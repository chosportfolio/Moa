"""Origin separation and recommendation-order regressions, with no live services.

HTTP tests reuse the server test harness's temporary state directory and fake
position sources. All geocoding, routing and place lookups are mocked.
"""
import copy
import tempfile
import json
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import personal_ratings
import server
import test_server as server_test_helpers


CITY_POINT = {'lat': 37.5663, 'lon': 126.9779}
FAKE_POSITION = {'latitude': 36.1, 'longitude': 127.1, 'accuracy_m': 10,
                 'source': 'test-fixture', 'updated_at': '2026-11-12T00:00:00+00:00'}


class RouteOriginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent/'tests')
        self.addCleanup(self.directory.cleanup)
        patches = [patch.object(server, 'ORIGIN_CACHE', {}),
                   patch.object(server, 'nominatim_search', return_value=[CITY_POINT]),
                   patch.object(server, 'current_position', return_value=FAKE_POSITION),
                   patch.object(server, 'DATA', Path(self.directory.name))]
        self.mocks = [p.start() for p in patches]
        for p in reversed(patches):
            self.addCleanup(p.stop)
        self.geocode, self.current = self.mocks[1:3]

    def test_only_exact_public_region_cache_can_supply_origin(self):
        folder=server.DATA/'places'
        folder.mkdir()
        cache=folder/(hashlib.sha256('서울'.encode()).hexdigest()+'.json')
        cache.write_text(json.dumps({'region':'서울','center':CITY_POINT}),encoding='utf-8')
        result=server.route_origin({'origin_region':'서울','region':'부산'})
        self.assertEqual(result['lat'],CITY_POINT['lat'])
        self.geocode.assert_not_called()
        server.ORIGIN_CACHE.clear()
        cache.write_text(json.dumps({'region':'부산','center':CITY_POINT}),encoding='utf-8')
        server.route_origin({'origin_region':'서울'})
        self.geocode.assert_called_once()

    def test_destination_search_region_is_never_geocoded_as_origin(self):
        result = server.route_origin({'region': '부산', 'candidate_regions': ['부산']})
        self.assertEqual((result['lat'], result['lon']), (36.1, 127.1))
        self.assertEqual(result['label'], '차량 현재 위치')
        self.geocode.assert_not_called()
        self.current.assert_called_once_with()

    def test_explicit_departure_city_has_priority_over_current_and_destination(self):
        result = server.route_origin({'origin_region': '서울', 'region': '부산'}, FAKE_POSITION)
        self.assertEqual((result['lat'], result['lon']), (37.5663, 126.9779))
        self.assertEqual(result['label'], '서울 도시 기준점')
        self.assertEqual(self.geocode.call_args.args[0]['q'], '서울')
        self.current.assert_not_called()

    def test_captured_current_position_is_used_without_fetching_another_fix(self):
        result = server.route_origin({'region': '부산'}, FAKE_POSITION)
        self.assertEqual(result['lat'], FAKE_POSITION['latitude'])
        self.current.assert_not_called()
        self.geocode.assert_not_called()

    def test_demo_without_explicit_origin_never_borrows_real_position(self):
        self.assertIsNone(server.route_origin({'region': '부산'}, FAKE_POSITION, demo=True))
        self.assertIsNone(server.route_origin({'region': '부산'}, demo=True))
        self.current.assert_not_called()
        self.geocode.assert_not_called()

    def test_demo_can_use_explicit_city_reference_point(self):
        result = server.route_origin({'origin_region': '서울', 'region': '부산'}, demo=True)
        self.assertEqual(result['label'], '서울 도시 기준점')
        self.current.assert_not_called()
        self.geocode.assert_called_once()

    def test_conflicting_origins_remain_unknown_even_with_current_fix(self):
        intent = {'needs_origin_choice': True, 'origin_candidates': ['서울', '부산'], 'region': '강릉'}
        self.assertIsNone(server.route_origin(intent, FAKE_POSITION))
        self.geocode.assert_not_called()
        self.current.assert_not_called()

    def test_geocode_failure_or_invalid_point_does_not_fallback_to_real_location(self):
        for response in ([], [{'lat': 'nan', 'lon': 127}], [{'lat': 91, 'lon': 127}], [None]):
            self.geocode.return_value = response
            self.assertIsNone(server.route_origin({'origin_region': '서울'}, FAKE_POSITION))
        self.geocode.side_effect = TimeoutError('fixture timeout')
        self.assertIsNone(server.route_origin({'origin_region': '서울'}, FAKE_POSITION))
        self.assertEqual(server.ORIGIN_CACHE, {})
        self.current.assert_not_called()

    def test_origin_cache_is_bounded_and_return_values_are_independent(self):
        server.ORIGIN_CACHE.update({f'fixture-{i}': dict(CITY_POINT, label='fixture') for i in range(64)})
        first = server.route_origin({'origin_region': '서울'})
        first['lat'] = 0
        second = server.route_origin({'origin_region': '서울'})
        self.assertEqual(second['lat'], CITY_POINT['lat'])
        self.assertEqual(len(server.ORIGIN_CACHE), 64)
        self.assertNotIn('fixture-0', server.ORIGIN_CACHE)
        self.geocode.assert_called_once()

    def test_unavailable_current_position_stays_unknown(self):
        self.current.side_effect = server.LocationUnavailable('test position unavailable')
        self.assertIsNone(server.route_origin({'region': '부산'}))
        self.geocode.assert_not_called()


class RoutePersonalizationTests(unittest.TestCase):
    def place(self, identifier, within, score=5, group='음식점', seats=None):
        return {'id': identifier, 'score': score, 'group': group,
                'matched_seats': seats or ['driver'],
                'travel_constraint': {'within_limit': within, 'verified': within is not None}}

    def test_rating_cannot_promote_over_limit_or_unknown_above_verified_within_limit(self):
        places = [self.place('viable', True), self.place('unknown', None), self.place('too-far', False)]
        ratings = {'visit:viable': {'rating': 1}, 'visit:unknown': {'rating': 4}, 'visit:too-far': {'rating': 5}}
        result = personal_ratings.personalize(places, ratings)
        self.assertEqual([p['id'] for p in result], ['viable', 'unknown', 'too-far'])
        self.assertNotIn('my_rating', places[0])

    def test_ratings_break_ties_only_within_same_route_tier_relevance_and_coverage(self):
        places = [self.place('first', True), self.place('park', True, group='산책'),
                  self.place('second', True), self.place('other-seat', True, seats=['passenger']),
                  self.place('different-score', True, score=4)]
        ratings = {'visit:first': {'rating': 1}, 'visit:second': {'rating': 5},
                   'visit:different-score': {'rating': 5}}
        result = personal_ratings.personalize(places, ratings)
        self.assertEqual([p['id'] for p in result], ['second', 'park', 'first', 'other-seat', 'different-score'])

    def test_sample_ratings_do_not_affect_real_recommendation_order(self):
        places = [self.place('first', True), self.place('second', True)]
        ratings = {'sample:first': {'rating': 1}, 'sample:second': {'rating': 5}}
        real = personal_ratings.personalize(places, ratings, demo=False)
        sample = personal_ratings.personalize(places, ratings, demo=True)
        self.assertEqual([p['id'] for p in real], ['first', 'second'])
        self.assertEqual([p['id'] for p in sample], ['second', 'first'])


class RouteHTTPIntegrationTests(unittest.TestCase):
    # Reuse only harness methods, not its test methods/discovery class.
    connection = server_test_helpers.ServerStateTests.connection
    request = server_test_helpers.ServerStateTests.request
    wait_job = server_test_helpers.ServerStateTests.wait_job

    def setUp(self):
        server_test_helpers.ServerStateTests.setUp(self)
        self.route_calls = []
        self.places = [
            {'id': 'too-far', 'score': 5, 'group': '음식점', 'matched_seats': ['driver']},
            {'id': 'unknown', 'score': 5, 'group': '음식점', 'matched_seats': ['driver']},
            {'id': 'viable', 'score': 5, 'group': '음식점', 'matched_seats': ['driver']},
        ]
        patches = [patch.object(server, 'ORIGIN_CACHE', {}),
                   patch.object(server, 'nominatim_search', return_value=[CITY_POINT]),
                   patch.object(server, 'current_position', return_value=FAKE_POSITION),
                   patch.object(server, 'recommend', side_effect=self.fake_recommend),
                   patch.object(server, 'enrich_travel_times', side_effect=self.fake_enrich)]
        self.integration_mocks = [p.start() for p in patches]
        self.integration_patches = patches

    def tearDown(self):
        # Wait for the temporary worker before restoring mocks or real state.
        try:
            server_test_helpers.ServerStateTests.tearDown(self)
        finally:
            for patcher in reversed(self.integration_patches):
                patcher.stop()

    def fake_recommend(self, *args, **kwargs):
        return {'constraints': [], 'recommendations': copy.deepcopy(self.places),
                'request_profile': {'travel_max_minutes': 120}, 'notice': '',
                'coverage': 'fixture', 'lookup_source': 'mocked'}

    def fake_enrich(self, places, origin, max_minutes):
        self.route_calls.append((copy.deepcopy(origin), max_minutes))
        result = copy.deepcopy(places)
        for place in result:
            within = {'viable': True, 'unknown': None, 'too-far': False}[place['id']] if origin else None
            place['travel_constraint'] = {'within_limit': within, 'verified': within is not None,
                                          'max_minutes': max_minutes, 'origin_label': (origin or {}).get('label', '')}
        return result

    def prepare(self, texts, demo=False):
        with server.LOCK:
            server.STATE.update(segments=[{'id': f's{i}', 'seat': 'driver' if i == 0 else 'passenger',
                                           'text': text} for i, text in enumerate(texts)],
                                demo=demo, search_mode='conversation',
                                _place_ratings={'visit:viable': {'rating': 1},
                                                'visit:unknown': {'rating': 4},
                                                'visit:too-far': {'rating': 5}})
            server.save_state()

    def recommend_and_wait(self, options=None):
        status, response = self.request('POST', '/api/recommend', options or {})
        self.assertEqual(status, 202, response)
        job = self.wait_job(response['job_id'])
        self.assertEqual(job['status'], 'complete', job)
        return self.request('GET', '/api/state')[1]

    def test_explicit_origin_is_separate_from_destination_and_route_tier_survives_personalization(self):
        self.prepare(['서울에서 출발하자.', '부산으로 가자.'])
        state = self.recommend_and_wait()
        self.assertEqual(state['region'], '부산')
        self.assertEqual(self.route_calls, [(dict(CITY_POINT, label='서울 도시 기준점'), 120)])
        self.assertEqual([p['id'] for p in state['recommendations']], ['viable', 'unknown', 'too-far'])
        self.integration_mocks[2].assert_not_called()

    def test_destination_only_uses_current_origin_not_destination_center(self):
        self.prepare(['부산으로 가자.'])
        state = self.recommend_and_wait()
        self.assertEqual(state['region'], '부산')
        self.assertEqual(self.route_calls[0][0]['lat'], FAKE_POSITION['latitude'])
        self.integration_mocks[1].assert_not_called()

    def test_sample_destination_without_departure_city_has_no_live_origin(self):
        self.prepare(['부산으로 가자.'], demo=True)
        self.recommend_and_wait()
        self.assertEqual(self.route_calls, [(None, 120)])
        self.integration_mocks[1].assert_not_called()
        self.integration_mocks[2].assert_not_called()

    def test_manual_destination_preserves_departure_from_conversation(self):
        self.prepare(['서울에서 출발하자.', '바다를 보고 싶어.'])
        state = self.recommend_and_wait({'region': '강릉'})
        self.assertEqual(state['region'], '강릉')
        self.assertEqual(self.route_calls[0][0]['label'], '서울 도시 기준점')
        self.integration_mocks[2].assert_not_called()

    def test_conflicting_departures_do_not_choose_one_or_use_current_position(self):
        self.prepare(['서울에서 출발하자.', '부산에서 출발하자.', '강릉으로 가자.'])
        state = self.recommend_and_wait()
        self.assertTrue(state['search_intent']['needs_origin_choice'])
        self.assertEqual(self.route_calls, [(None, 120)])
        self.integration_mocks[1].assert_not_called()
        self.integration_mocks[2].assert_not_called()


if __name__ == '__main__':
    unittest.main()
