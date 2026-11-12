"""Scenario-level regressions; fixtures are synthetic POIs, never real ratings."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import trip_recommendation as trip
from recommend import choose_places


ORIGIN = {'lat': 37.4, 'lon': 127.1, 'label': '예시 출발 위치'}


def poi(i, category='beach', lat=None):
    tags = {'name': f'테스트 장소 {i}'}
    tags.update({'beach': {'natural': 'beach'}, 'cafe': {'amenity': 'cafe'},
                 'park': {'leisure': 'park'}, 'culture': {'tourism': 'museum'},
                 'meal': {'amenity': 'restaurant', 'cuisine': 'pasta'}}[category])
    return {'type': 'node', 'id': i, 'lat': lat or 37.4+i*.01,
            'lon': 127.1+i*.01, 'tags': tags,
            'coastal_distance_m': 50 if category == 'beach' else None,
            'coastal_basis': 'OpenStreetMap natural=coastline' if category == 'beach' else None}


class TripRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent / 'tests')
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        self.received = []

    def data(self, elements):
        return {'elements': elements, 'center': ORIGIN, 'fetched_at': 1,
                'coverage': '테스트 넓은 검색', 'lookup_source': 'fixture',
                'discovery': {'mode': 'travel_envelope'}}

    def route(self, places, origin, minutes):
        self.received.extend(copy.deepcopy(places))
        result = copy.deepcopy(places)
        for place in result:
            place['travel_constraint'] = {'within_limit': True, 'verified': True,
                'duration_minutes': 25, 'max_minutes': minutes, 'status': 'ok', 'live_traffic': False}
        return result

    def run_case(self, text, elements, router=None, seat='unknown'):
        with patch.object(trip, 'discover_candidates', return_value=self.data(elements)) as discovery, \
             patch.object(trip, 'enrich_candidate_travel_times', side_effect=router or self.route):
            result = trip.recommend_trip([{'id': 'real-final', 'seat': seat, 'text': text}],
                                         [], self.cache, ORIGIN, 120)
        return result, discovery

    def test_real_unknown_speaker_and_sample_seats_use_same_discovery(self):
        text = '차로 두 시간 이내에 바다 가고 싶어.'
        actual, one = self.run_case(text, [poi(1), poi(2, lat=38.1)])
        sample, two = self.run_case(text, [poi(1), poi(2, lat=38.1)], seat='driver')
        self.assertEqual([p['id'] for p in actual['recommendations']], [p['id'] for p in sample['recommendations']])
        self.assertEqual(one.call_args.args[0], ORIGIN)
        self.assertEqual(two.call_args.args[2]['travel_max_minutes'], 120)

    def test_every_supported_kind_uses_broad_discovery(self):
        for topic, category in [('커피 마시고 싶어', 'cafe'), ('파스타 먹고 싶어', 'meal'),
                                ('공원 산책 가자', 'park'), ('박물관 가자', 'culture')]:
            with self.subTest(category=category):
                result, discovery = self.run_case('차로 두 시간 이내에 '+topic, [poi(1, category)])
                self.assertEqual(len(result['recommendations']), 1)
                discovery.assert_called_once()

    def test_route_filter_runs_before_twelve_place_selection(self):
        def routes(places, origin, minutes):
            result = self.route(places, origin, minutes)
            for place in result:
                value = int(place['id'].split('/')[1])
                place['travel_constraint']['within_limit'] = value > 12
            return result
        result, _ = self.run_case('바다 가자. 차로 두 시간 이내로.', [poi(i) for i in range(1, 31)], routes)
        self.assertEqual(len(self.received), 30)
        self.assertEqual(len(result['recommendations']), 12)
        self.assertTrue(all(int(p['id'].split('/')[1]) > 12 for p in result['recommendations']))
        self.assertEqual(result['request_profile']['search_report']['over_limit_count'], 12)

    def test_unknown_routes_and_over_limit_never_fill_empty_results(self):
        def routes(places, origin, minutes):
            result = self.route(places, origin, minutes)
            for i, place in enumerate(result):
                place['travel_constraint'].update(within_limit=False if i else None, verified=bool(i), status='ok' if i else 'timeout')
            return result
        result, _ = self.run_case('바다 가자.', [poi(1), poi(2)], routes)
        self.assertEqual(result['recommendations'], [])
        self.assertIn('경로 시간을 확인하지 못한', result['notice'])

    def test_missing_origin_must_not_call_places_or_fabricate_location(self):
        with patch.object(trip, 'discover_candidates') as fetch:
            with self.assertRaisesRegex(ValueError, '출발 위치'):
                trip.recommend_trip([{'text': '바다 가자'}], [], self.cache, None, 120)
            fetch.assert_not_called()

    def test_explicit_alternative_regions_keep_their_scope(self):
        def lookup(region, cache, **kwargs):
            return self.data([poi(1 if region == '화성' else 2)])
        with patch('recommend.lookup_places', side_effect=lookup) as lookup_mock, \
             patch.object(trip, 'discover_region_candidates', return_value=None), \
             patch.object(trip, 'discover_candidates') as discovery, \
             patch.object(trip, 'enrich_candidate_travel_times', side_effect=self.route):
            result = trip.recommend_trip([{'id': 'one', 'text': '바다 가자', 'seat': 'unknown'}],
                                         ['화성', '인천'], self.cache, ORIGIN, 120)
        self.assertEqual([call.args[0] for call in lookup_mock.call_args_list], ['화성', '인천'])
        discovery.assert_not_called()
        self.assertEqual(len(result['recommendations']), 2)

    def test_final_choice_contains_geographically_different_options(self):
        nearby = [{'id': str(i), 'name': str(i), 'score': 10, 'group': '음식점',
                   'latitude': 37.4, 'longitude': 127.1, 'matched_topics': ['파스타'],
                   'matched_seats': ['unknown']} for i in range(15)]
        far = dict(nearby[0], id='other-area', name='other-area', latitude=37.9)
        result = choose_places(nearby + [far], geographic_diversity=True)
        self.assertEqual(result[1]['id'], 'other-area')

    def test_food_ranking_uses_real_visit_feedback_without_sample_rating_leak(self):
        elements = [poi(1, 'meal'), poi(2, 'meal')]
        ratings = {'visit:node/2': {'rating': 5}, 'sample:node/1': {'rating': 5}}
        with patch.object(trip, 'discover_candidates', return_value=self.data(elements)), \
             patch.object(trip, 'enrich_candidate_travel_times', side_effect=self.route):
            result = trip.recommend_trip([{'id': 'spoken', 'seat': 'unknown',
                'text': '파스타 먹고 싶어. 차로 두 시간 이내로 가자.'}],
                [], self.cache, ORIGIN, 120, ratings=ratings)
        self.assertEqual(result['recommendations'][0]['id'], 'node/2')
        winner = result['recommendations'][0]
        self.assertTrue(any(f['key'] == 'personal_visit_rating' for f in winner['quality_factors']))
        other = result['recommendations'][1]
        self.assertFalse(any(f['key'] == 'personal_visit_rating' for f in other['quality_factors']))

    def test_coffee_and_bakery_choices_do_not_degenerate_to_one_activity(self):
        candidates=[]
        for i in range(30):
            activity='카페' if i < 15 else '빵·디저트'
            candidates.append({'id':str(i),'name':str(i),'group':'카페·디저트',
                'selection_activity':activity,'matched_topics':[activity],
                'score':10 if i < 15 else 20,'quality_score':3 if i < 15 else 10,
                'latitude':37+i*.01,'longitude':127+i*.1})
        selected=choose_places(candidates,geographic_diversity=True)
        self.assertEqual(6,sum(p['selection_activity']=='카페' for p in selected))
        self.assertEqual(6,sum(p['selection_activity']=='빵·디저트' for p in selected))


if __name__ == '__main__':
    unittest.main()
