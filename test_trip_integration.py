"""Recording-to-recommendation HTTP coverage with no external services.

Only the public POI, geocoder, GPS and road-time boundaries are substituted.
Speech-final persistence, dialogue parsing, scoring, time screening and final
selection use their production code, with unknown speakers as on the device.
"""
from collections import OrderedDict
import copy
import json
import unittest
from unittest.mock import patch

import routing_duration
import server
import test_server as server_test_helpers
import trip_recommendation


ORIGIN = {'lat': 37.394, 'lon': 127.111}
CURRENT_POSITION = {
    'latitude': 37.5, 'longitude': 127.0, 'accuracy_m': 10,
    'source': 'test-vehicle', 'updated_at': '2026-11-12T00:00:00+00:00',
}


class TripHTTPIntegrationTests(unittest.TestCase):
    connection = server_test_helpers.ServerStateTests.connection
    request = server_test_helpers.ServerStateTests.request
    wait_job = server_test_helpers.ServerStateTests.wait_job

    def setUp(self):
        server_test_helpers.ServerStateTests.setUp(self)
        self.elements = []
        self.minutes = {}
        self.discovery_calls = []
        self.route_calls = []
        self.fixture_patches = [
            patch.object(server, 'ORIGIN_CACHE', {}),
            patch.object(server, 'nominatim_search', return_value=[dict(ORIGIN)]),
            patch.object(server, 'current_position', return_value=dict(CURRENT_POSITION)),
            patch.object(trip_recommendation, 'discover_candidates', side_effect=self.discover),
            patch.object(routing_duration, '_ROUTE_CACHE', OrderedDict()),
            patch.object(routing_duration, '_fetch_table', side_effect=self.routes),
        ]
        mocks = [patcher.start() for patcher in self.fixture_patches]
        self.geocode, self.current = mocks[1:3]
        self.prepare_recording()

    def tearDown(self):
        try:
            # Finish jobs while all service substitutes are still installed.
            server_test_helpers.ServerStateTests.tearDown(self)
        finally:
            for patcher in reversed(self.fixture_patches):
                patcher.stop()

    def prepare_recording(self):
        with server.LOCK:
            server.STATE.update(segments=[], recommendations=[], demo=False,
                                _demo_backup={}, _speech_streams={}, search_mode='conversation',
                                search_intent={}, saved_places=[{'id': 'saved-before', 'name': '기존 기록'}])
            server.save_state()

    def add_place(self, identifier, kind, *, minutes=20, lat=None, lon=None):
        labels = {'beach': '시험 해변', 'cafe': '시험 카페', 'gallery': '시험 미술관',
                  'restaurant': '시험 한식당'}
        tags = {'name': f'{labels[kind]} {identifier}'}
        if kind == 'beach':
            tags['natural'] = 'beach'
        elif kind == 'gallery':
            tags['tourism'] = 'gallery'
        else:
            tags['amenity'] = kind
            if kind == 'restaurant':
                tags['cuisine'] = 'korean'
        point = (round(lat if lat is not None else 37.394 + identifier * .002, 6),
                 round(lon if lon is not None else 127.111, 6))
        self.elements.append({'type': 'node', 'id': identifier,
                              'lat': point[0], 'lon': point[1], 'tags': tags,
                              'coastal_distance_m': 50 if kind == 'beach' else None,
                              'coastal_basis': 'OpenStreetMap natural=coastline' if kind == 'beach' else None})
        self.minutes[point] = minutes
        return f'node/{identifier}'

    def discover(self, origin, maximum, profile):
        self.discovery_calls.append((copy.deepcopy(origin), maximum, copy.deepcopy(profile)))
        return {'elements': copy.deepcopy(self.elements), 'center': dict(ORIGIN),
                'fetched_at': '2026-11-12T00:00:00+00:00',
                'coverage': '테스트용 여러 지역 후보', 'lookup_source': 'fixture-public-places',
                'discovery': {'mode': 'fixture-broad-search'}}

    def routes(self, origin, destinations):
        self.route_calls.append((origin, list(destinations)))
        return tuple(routing_duration._Estimate(seconds=self.minutes[point]*60,
                                                distance_m=self.minutes[point]*500,
                                                reason='ok')
                     if self.minutes[point] is not None else
                     routing_duration._Estimate(reason='unreachable')
                     for point in destinations)

    def record(self, *texts):
        _, before = self.request('GET', '/api/state')
        for index, text in enumerate(texts):
            event = {'conversation_id': before['conversation_id'], 'session_id': 'native-trip-test',
                     'sequence': index, 'text': text, 'start': index*4, 'end': index*4+3,
                     'source': 'pleos_on_device',
                     'recording_started_at': '2026-11-12T00:00:00.000Z'}
            status, response = self.request('POST', '/api/speech/final', event)
            self.assertEqual(status, 200, response)
            self.assertTrue(response['committed'])
        _, state = self.request('GET', '/api/state')
        self.assertEqual([row['text'] for row in state['segments']], list(texts))
        self.assertTrue(all(row['seat'] == 'unknown' for row in state['segments']))
        self.assertTrue(all(row['source'] == 'pleos_on_device' for row in state['segments']))
        return state

    def recommend(self):
        status, response = self.request('POST', '/api/recommend', {})
        self.assertEqual(status, 202, response)
        job = self.wait_job(response['job_id'])
        self.assertEqual(job['status'], 'complete', job)
        status, state = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertTrue(all(place['travel_constraint']['within_limit'] is True
                            for place in state['recommendations']))
        return state

    def test_recorded_explicit_departure_works_without_gps_and_keeps_original_data(self):
        beach = self.add_place(1, 'beach', minutes=80)
        self.add_place(2, 'beach', minutes=140)
        self.add_place(3, 'cafe', minutes=15)
        self.current.side_effect = server.LocationUnavailable('테스트 GPS 없음')
        recorded = self.record('판교에서 차로 두 시간 이내에 바다를 가고 싶어.')
        result = self.recommend()
        self.assertEqual([p['id'] for p in result['recommendations']], [beach])
        self.assertEqual(result['segments'], recorded['segments'])
        self.assertEqual(result['conversation_id'], recorded['conversation_id'])
        self.assertEqual(result['saved_places'], recorded['saved_places'])
        self.assertEqual(result['search_intent']['origin_region'], '판교')
        self.assertEqual(result['search_intent']['search_regions'], [])
        self.assertEqual(self.discovery_calls[0][1], 120)
        self.assertEqual(self.discovery_calls[0][0]['lat'], ORIGIN['lat'])
        self.assertEqual(self.geocode.call_args.args[0]['q'], '판교')
        self.current.assert_not_called()
        persisted = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
        self.assertEqual(persisted['segments'], recorded['segments'])
        self.assertEqual(persisted['_speech_streams'], server.STATE['_speech_streams'])
        self.assertEqual(persisted['saved_places'], recorded['saved_places'])

    def test_recorded_request_without_departure_uses_current_vehicle_position(self):
        cafe = self.add_place(1, 'cafe', minutes=25)
        self.add_place(2, 'cafe', minutes=40)
        self.add_place(3, 'beach', minutes=10)
        self.record('차로 30분 이내에 갈 카페 찾아줘. 커피 마시고 싶어.')
        result = self.recommend()
        self.assertEqual([p['id'] for p in result['recommendations']], [cafe])
        origin = self.discovery_calls[0][0]
        self.assertEqual((origin['lat'], origin['lon']),
                         (CURRENT_POSITION['latitude'], CURRENT_POSITION['longitude']))
        self.assertEqual(origin['label'], '차량 현재 위치')
        self.geocode.assert_not_called()
        self.current.assert_called_once_with()
        self.assertEqual(result['search_location']['source'], CURRENT_POSITION['source'])

    def test_valid_candidates_after_first_twelve_are_routed_before_selection(self):
        for identifier in range(1, 14):
            self.add_place(identifier, 'cafe', minutes=60)
        valid = {self.add_place(14, 'cafe', minutes=20), self.add_place(15, 'cafe', minutes=25)}
        self.record('판교에서 차로 30분 이내에 갈 카페 찾아줘.')
        result = self.recommend()
        self.assertEqual({p['id'] for p in result['recommendations']}, valid)
        report = result['request_profile']['search_report']
        self.assertEqual(report['candidate_count'], 15)
        self.assertEqual(report['route_candidate_count'], 15)
        self.assertEqual(report['route_confirmed_count'], 15)
        self.assertEqual(report['over_limit_count'], 13)
        self.assertEqual(report['within_limit_count'], 2)
        self.assertEqual(sum(len(points) for _, points in self.route_calls), 15)

    def test_the_same_recording_pipeline_handles_exhibitions_and_food(self):
        gallery = self.add_place(1, 'gallery', minutes=25)
        restaurant = self.add_place(2, 'restaurant', minutes=25)
        self.add_place(3, 'beach', minutes=25)
        self.add_place(4, 'cafe', minutes=25)
        for text, expected in (
                ('서울에서 출발해서 차로 40분 이내에 전시 보러 가자.', gallery),
                ('서울에서 출발해서 차로 40분 이내에 한식 먹으러 가자.', restaurant)):
            with self.subTest(text=text):
                self.prepare_recording()
                self.record(text)
                result = self.recommend()
                self.assertEqual([p['id'] for p in result['recommendations']], [expected])
                self.assertTrue(result['recommendations'][0]['evidence'])
                self.assertTrue(any(e['quote'] == text for e in result['recommendations'][0]['evidence']))

    def test_sample_and_recording_share_selection_and_sample_exit_restores_recording(self):
        text = '판교에서 차로 두 시간 이내에 바다를 가고 싶어.'
        self.add_place(1, 'beach', minutes=80)
        self.add_place(2, 'beach', minutes=110, lat=37.6, lon=126.5)
        self.add_place(3, 'beach', minutes=130)
        self.current.side_effect = server.LocationUnavailable('테스트 GPS 없음')
        self.record(text)
        real = self.recommend()
        scenario = {'id': 'fixture-departure', 'title': '테스트용 가상 대화',
                    'dialogue': (('driver', text),)}
        with patch('demo_conversations.random_scenario', return_value=scenario):
            status, sample = self.request('POST', '/api/demo', {})
        self.assertEqual(status, 200, sample)
        self.assertTrue(sample['demo'])
        sample = self.recommend()
        self.assertEqual([p['id'] for p in sample['recommendations']],
                         [p['id'] for p in real['recommendations']])
        self.assertEqual(sample['request_profile']['search_report'],
                         real['request_profile']['search_report'])
        self.assertEqual(len(self.discovery_calls), 2)
        self.current.assert_not_called()
        status, restored = self.request('POST', '/api/demo/exit', {})
        self.assertEqual(status, 200, restored)
        self.assertFalse(restored['demo'])
        self.assertEqual(restored['segments'], real['segments'])
        self.assertEqual(restored['recommendations'], real['recommendations'])
        self.assertEqual(restored['saved_places'], real['saved_places'])


if __name__ == '__main__':
    unittest.main()
