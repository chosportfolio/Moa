import unittest
import re
from unittest.mock import patch
from urllib.parse import parse_qs

import travel_discovery as discovery


class TravelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        discovery._CACHE.clear()
        self.origin = {'lat':37.39, 'lon':127.1, 'label':'출발 위치'}

    def fetch(self, elements):
        calls = []
        def run(url, data=None, timeout=None):
            calls.append((url, parse_qs(data.decode())['data'][0], timeout))
            return {'elements':elements}
        return run, calls

    def place(self, identifier, latitude=37.2, longitude=126.6, **tags):
        return {'type':'node','id':identifier,'lat':latitude,'lon':longitude,
                'tags':{'name':'공개 장소 '+str(identifier), **tags}}

    def test_beach_search_extends_beyond_local_radius_and_remains_unverified(self):
        beach = self.place(1, natural='beach')
        fetcher, calls = self.fetch([beach])
        data = discovery.discover_candidates(self.origin, 120, {'requested_topics':['해변']}, fetcher=fetcher)
        self.assertEqual([1], [p['id'] for p in data['elements']])
        self.assertEqual(260000, data['discovery']['radius_m'])
        self.assertTrue(data['discovery']['requires_route_check'])
        self.assertFalse(data['discovery']['complete_inventory'])
        self.assertEqual(1, len(calls))
        self.assertIn('(around:260000,', calls[0][1])
        self.assertNotIn('restaurant', calls[0][1])
        self.assertNotIn('판교', calls[0][1])

    def test_installed_public_index_avoids_unavailable_remote_query(self):
        indexed={'elements':[self.place(1,amenity='cafe')], 'discovery':{'mode':'local_public_index'}}
        with patch('poi_index.query_candidates',return_value=indexed) as local, \
             patch('recommend.get_json',side_effect=AssertionError('no remote request')):
            data=discovery.discover_candidates(self.origin,60,{'requested_topics':['카페']})
        local.assert_called_once_with(self.origin,130000,('cafe',),())
        self.assertEqual('local_public_index',data['discovery']['mode'])
        self.assertTrue(data['discovery']['requires_route_check'])

    def test_general_cafe_culture_and_park_are_not_limited_to_beach_logic(self):
        places = [self.place(1, amenity='cafe'), self.place(2, tourism='museum'),
                  self.place(3, leisure='park'), self.place(4, natural='beach')]
        fetcher, calls = self.fetch(places)
        data = discovery.discover_candidates(self.origin, 60,
            {'requested_topics':['카페','전시·문화','산책']}, fetcher=fetcher)
        self.assertEqual({1,2,3}, {p['id'] for p in data['elements']})
        self.assertIn('museum|gallery', calls[0][1])
        self.assertNotIn('natural', calls[0][1])

    def test_specific_menus_have_reserved_search_and_excluded_cuisine_is_omitted(self):
        fetcher, calls = self.fetch([self.place(1, amenity='restaurant', cuisine='pasta'),
                                     self.place(2, amenity='restaurant', cuisine='korean')])
        data = discovery.discover_candidates(self.origin, 90,
            {'requested_topics':['식사','파스타','한식'], 'requested_menu_topics':['파스타','한식'],
             'excluded_topics':['한식']}, fetcher=fetcher)
        self.assertEqual([1], [p['id'] for p in data['elements']])
        self.assertIn('파스타|스파게티', calls[0][1])
        self.assertNotIn('korean', calls[0][1])
        self.assertGreater(calls[0][1].count('out center tags'), 16)

    def test_korean_only_names_are_queried_and_preserved_for_all_groups(self):
        kinds = [('meal', {'amenity':'restaurant'}), ('cafe', {'amenity':'cafe'}),
                 ('bakery', {'shop':'bakery'}), ('park', {'leisure':'park'}),
                 ('culture', {'tourism':'museum'}), ('beach', {'natural':'beach'})]
        places = []
        for identifier, (group, tags) in enumerate(kinds, 1):
            place = self.place(identifier, **tags)
            place['tags'].pop('name')
            place['tags']['name:ko'] = '한글 이름 '+str(identifier)
            places.append(place)
            selectors = discovery._selectors(group)
            self.assertTrue(any('["name:ko"]' in value for value in selectors))
            self.assertTrue(any('["name"]' in value for value in selectors))
        fetcher, calls = self.fetch(places)
        result = discovery.discover_candidates(self.origin, 60, {}, fetcher=fetcher)
        self.assertEqual(set(range(1, 7)), {place['id'] for place in result['elements']})
        self.assertTrue(all('name:ko' in place['tags'] for place in result['elements']))
        quotas = [int(value) for value in re.findall(r'out center tags (\d+)', calls[0][1])]
        self.assertLessEqual(sum(quotas), discovery.MAX_ELEMENTS)

    def test_requested_dish_in_korean_alias_without_cuisine_is_found(self):
        place = self.place(1, amenity='restaurant')
        place['tags'].update({'name':'Coastal Noodles', 'name:ko':'바닷가 칼국수'})
        fetcher, calls = self.fetch([place])
        result = discovery.discover_candidates(self.origin, 120,
            {'requested_topics':['식사','칼국수'], 'requested_menu_topics':['칼국수']}, fetcher=fetcher)
        self.assertEqual([1], [place['id'] for place in result['elements']])
        self.assertIn('["name"~"칼국수"]', calls[0][1])
        self.assertIn('["name:ko"~"칼국수"]', calls[0][1])
        self.assertNotIn('cuisine', result['elements'][0]['tags'])

    def test_invalid_duplicate_outside_circle_and_wrong_categories_are_removed(self):
        place = self.place(1, amenity='cafe')
        fetcher, _ = self.fetch([place, place, self.place(2, 10, 10, amenity='cafe'),
                                self.place(True, amenity='cafe'), {'type':'node','id':3},
                                self.place(4, natural='beach')])
        data = discovery.discover_candidates(self.origin, 60, {'requested_topics':['카페']}, fetcher=fetcher)
        self.assertEqual([1], [p['id'] for p in data['elements']])

    def test_cached_result_is_independent_and_does_not_repeat_network_call(self):
        fetcher, calls = self.fetch([self.place(1, amenity='cafe')])
        profile = {'requested_topics':['카페']}
        first = discovery.discover_candidates(self.origin, 60, profile, fetcher=fetcher)
        first['elements'].clear()
        second = discovery.discover_candidates(self.origin, 60, profile, fetcher=fetcher)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(second['elements']))

    def test_origin_and_budget_errors_do_not_call_the_data_source(self):
        def fail(*args, **kwargs):
            self.fail('must not call network')
        for origin, limit in [(None,60), ({'lat':True,'lon':127},60),
                              (self.origin,0), (self.origin,float('nan')), (self.origin,True)]:
            with self.subTest(origin=origin, limit=limit), self.assertRaises(ValueError):
                discovery.discover_candidates(origin, limit, {}, fetcher=fail)

    def test_partial_query_is_not_cached_or_presented_as_success(self):
        def partial(*args, **kwargs):
            return {'elements':[self.place(1, amenity='cafe')], 'remark':'timeout'}
        with self.assertRaisesRegex(ValueError, '여러 지역'):
            discovery.discover_candidates(self.origin, 60, {}, fetcher=partial)
        self.assertFalse(discovery._CACHE)

    def test_inner_grid_and_adaptive_quotas_improve_reachable_candidate_coverage(self):
        groups = ('beach',)
        query = discovery.build_query((37.39,127.1),260000,groups,())
        quotas = [int(value) for value in re.findall(r'out center tags (\d+)', query)]
        self.assertEqual(21,len(quotas))  # nearby + 4 inner + 16 outer cells
        self.assertGreaterEqual(min(quotas[1:]),8)
        self.assertLessEqual(sum(quotas),discovery.MAX_ELEMENTS)
        broad = discovery._tiles(37.39,127.1,260000)
        inner = discovery._tiles(37.39,127.1,80000,divisions=2)
        self.assertLess(inner[0][2]-inner[0][0], broad[0][2]-broad[0][0])

    def test_all_topics_query_keeps_total_allowance_bounded(self):
        from recommend import MENU_SEARCH_TERMS
        groups = ('meal','cafe','bakery','park','culture','beach')
        query = discovery.build_query((37.39,127.1),260000,groups,tuple(MENU_SEARCH_TERMS))
        quotas = [int(value) for value in re.findall(r'out center tags (\d+)', query)]
        self.assertLessEqual(sum(quotas),discovery.MAX_ELEMENTS)
        self.assertGreaterEqual(min(quotas),2)

    def test_long_duration_matches_extractor_range_with_honest_radius_cap(self):
        self.assertEqual(1_560_000,discovery.search_radius(720))
        self.assertEqual(discovery.MAX_RADIUS_M,discovery.search_radius(1440))
        fetcher, _ = self.fetch([])
        result = discovery.discover_candidates(self.origin,1440,{'requested_topics':['해변']},fetcher=fetcher)
        self.assertTrue(result['discovery']['radius_capped'])
        self.assertIn('2,000km',result['search_note'])
        with self.assertRaises(ValueError):
            discovery.search_radius(1441)

    def test_rare_requested_dish_survives_dense_same_area(self):
        elements = [self.place(i, 37.4+i*.000001,127.1,amenity='restaurant',cuisine='pizza')
                    for i in range(1,800)]
        elements.append(self.place(1000,37.402,127.1,amenity='restaurant',cuisine='pasta'))
        fetcher, _ = self.fetch(elements)
        data = discovery.discover_candidates(self.origin,120,
            {'requested_topics':['파스타','피자'],'requested_menu_topics':['파스타','피자']}, fetcher=fetcher)
        self.assertTrue(any(place['id']==1000 for place in data['elements']))

    def test_geographic_bucket_prevents_single_dense_area_from_consuming_cap(self):
        elements = [self.place(i, 37.4+i*.000001,127.1,amenity='cafe') for i in range(1,800)]
        elements += [self.place(1000,37.0,126.3,amenity='cafe'), self.place(1001,37.8,128.0,amenity='cafe')]
        fetcher, _ = self.fetch(elements)
        data = discovery.discover_candidates(self.origin,120,{'requested_topics':['카페']},fetcher=fetcher)
        self.assertEqual(discovery.MAX_ELEMENTS, len(data['elements']))
        self.assertTrue({1000,1001} <= {p['id'] for p in data['elements']})


if __name__ == '__main__':
    unittest.main()
