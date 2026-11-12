"""Journey HTTP regressions use temporary state and synthetic POIs only."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch

import journey
import server
import test_server as server_tests


def sample_state():
    return {
        'segments': [{'id': 'fixture-line', 'seat': 'unknown', 'text': '조용한 카페가 좋아',
                      'start': 0, 'end': 2, 'confidence': None}],
        'recommendations': [{
            'id': 'node/test-only-1', 'name': '테스트 전용 가상 카페',
            'latitude': 37.39, 'longitude': 127.11, 'address': '테스트 주소',
            'reason_summary': '대화에서 조용한 카페를 원했어요.',
            'reasons': [{'seat': 'unknown', 'text': '카페 선호', 'quote': '카페가 좋아'}],
            'cautions': ['가상 테스트 장소'],
        }],
        'recommendation_summary': {
            'title': '함께 정한 조건', 'text': '조용한 카페를 원해요.',
            'bullets': ['카페'], 'evidence': [{'seat': 'unknown', 'text': '카페가 좋아', 'segment_id': 'fixture-line'}],
            'limitations': ['좌석 정보 없음'], 'generated_at': '2026-01-01T00:00:00Z', 'segment_count': 1,
        },
        'constraints': [], 'region': '테스트 지역', 'demo': False,
        'notice': '', 'coverage': '', 'lookup_source': '', 'trip': {}, 'saved_places': [],
    }


class JourneyHTTPTests(unittest.TestCase):
    # Reuse the isolated HTTP fixture, not its test methods. The real user's
    # data paths are replaced before any writes or requests occur.
    tearDown = server_tests.ServerStateTests.tearDown
    connection = server_tests.ServerStateTests.connection
    request = server_tests.ServerStateTests.request
    wait_job = server_tests.ServerStateTests.wait_job

    def setUp(self):
        server_tests.ServerStateTests.setUp(self)
        self.initial = sample_state()
        with server.LOCK:
            server.save_state(copy.deepcopy(self.initial))

    def select(self, place_id='node/test-only-1'):
        status, state = self.request('POST', '/api/trip/select', {'place_id': place_id})
        self.assertEqual(status, 200, state)
        return state['trip']['id']

    def navigate_and_arrive(self, identifier):
        for path, values in (
            ('start', {'provider': 'pleos'}),
            ('arrive', {'source': 'pleos_arrival'}),
        ):
            status, state = self.request('POST', '/api/trip/' + path, {'trip_id': identifier, **values})
            self.assertEqual(status, 200, state)
        return state

    def test_complete_native_flow_persists_snapshots_and_duplicate_events_once(self):
        identifier = self.select()
        self.assertEqual(self.select(), identifier)
        state = self.navigate_and_arrive(identifier)
        arrival = copy.deepcopy(state['trip'])
        self.assertEqual(arrival['arrival_source'], 'pleos_arrival')
        self.assertEqual(self.navigate_and_arrive(identifier)['trip'], arrival)
        status, state = self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': True})
        self.assertEqual(status, 200)
        self.assertEqual(state['trip']['status'], 'saved')
        record = state['saved_places'][0]
        self.assertEqual(record['summary'], self.initial['recommendation_summary'])
        self.assertEqual(record['place'], arrival['place'])
        self.assertEqual(record['id'], identifier)
        self.assertTrue(record['saved_at'])
        self.assertEqual(record['provider'], 'pleos')
        self.assertEqual(self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': True})[1]['saved_places'], [record])
        restored = server.load_state(server.STATE_FILE, self.initial)
        self.assertEqual(restored['saved_places'], [record])
        self.assertEqual(restored['trip'], state['trip'])

    def test_declining_keeps_only_minimal_marker_and_no_saved_record(self):
        identifier = self.select()
        self.navigate_and_arrive(identifier)
        status, result = self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': False})
        self.assertEqual(status, 200)
        self.assertEqual(result['saved_places'], [])
        self.assertEqual(set(result['trip']), {'id', 'status', 'decision_at'})
        self.assertEqual(result['trip']['status'], 'dismissed')
        self.assertEqual(result['segments'], self.initial['segments'])
        marker = result['trip']
        self.assertEqual(self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': False})[1]['trip'], marker)
        self.assertEqual(self.navigate_and_arrive(identifier)['trip'], marker)
        self.assertEqual(self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': True})[0], 409)
        self.assertEqual(json.loads(server.STATE_FILE.read_text(encoding='utf-8'))['trip'], marker)

    def test_manual_arrival_from_selection_is_explicit_and_does_not_invent_navigation(self):
        identifier = self.select()
        self.assertEqual(self.request('POST', '/api/trip/arrive', {'trip_id': identifier, 'source': 'pleos_arrival'})[0], 409)
        status, state = self.request('POST', '/api/trip/arrive', {'trip_id': identifier, 'source': 'manual_confirmation'})
        self.assertEqual(status, 200)
        trip = state['trip']
        self.assertEqual(trip['status'], 'arrived')
        self.assertEqual(trip['arrival_source'], 'manual_confirmation')
        self.assertIsNone(trip['started_at'])
        self.assertIsNone(trip['provider'])
        # A late native event must not change the recorded provenance.
        self.assertEqual(self.request('POST', '/api/trip/arrive', {'trip_id': identifier, 'source': 'pleos_arrival'})[1]['trip'], trip)

    def test_invalid_order_stale_ids_and_sources_never_mutate_state(self):
        identifier = self.select()
        before = copy.deepcopy(server.STATE)
        cases = [
            ('decision', {'trip_id': identifier, 'save': True}, 409),
            ('decision', {'trip_id': identifier, 'save': 'false'}, 400),
            ('start', {'trip_id': 'stale-id', 'provider': 'pleos'}, 409),
            ('start', {'trip_id': identifier, 'provider': ''}, 400),
            ('arrive', {'trip_id': identifier, 'source': 'timer'}, 400),
            ('arrive', {'trip_id': identifier, 'source': ['pleos_arrival']}, 400),
            ('arrive', {'trip_id': 'stale-id', 'source': 'manual_confirmation'}, 409),
        ]
        for path, body, expected in cases:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.request('POST', '/api/trip/' + path, body)[0], expected)
                self.assertEqual(server.STATE, before)

    def test_recommendation_replacement_and_reset_preserve_selected_and_saved_snapshots(self):
        identifier = self.select()
        selected = copy.deepcopy(server.STATE['trip'])
        status, state = self.request('PATCH', '/api/segments/fixture-line', {'text': '이제 식당이 좋아'})
        self.assertEqual(status, 200)
        self.assertEqual(state['recommendation_summary'], {})
        self.assertEqual(state['trip'], selected)
        replacement = {'recommendation_summary': {'text': '새 대화 요약'}, 'recommendations': [], 'region': '다른 지역'}
        with patch('server.recommend', return_value=replacement):
            status, result = self.request('POST', '/api/recommend', {'region': '다른 지역'})
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
        self.assertEqual(server.STATE['trip'], selected)
        self.navigate_and_arrive(identifier)
        self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': True})
        saved = copy.deepcopy(server.STATE['saved_places'])
        status, state = self.request('POST', '/api/reset')
        self.assertEqual(status, 200)
        self.assertEqual(state['recommendation_summary'], {})
        self.assertEqual(state['saved_places'], saved)
        self.assertEqual(saved[0]['region'], '테스트 지역')
        self.assertEqual(saved[0]['summary'], self.initial['recommendation_summary'])

    def test_native_arrival_can_commit_while_an_unrelated_job_is_running(self):
        identifier = self.select()
        with server.LOCK:
            server.JOBS['busy'] = {'id': 'busy', 'status': 'running'}
            server.ACTIVE_JOB = 'busy'
        self.assertEqual(self.navigate_and_arrive(identifier)['trip']['status'], 'arrived')

    def test_disk_failure_does_not_publish_save_and_retry_is_safe(self):
        identifier = self.select()
        self.navigate_and_arrive(identifier)
        before = copy.deepcopy(server.STATE)
        disk_before = server.STATE_FILE.read_bytes()
        with patch('server.os.replace', side_effect=OSError('synthetic test write failure')):
            self.assertEqual(self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': True})[0], 500)
        self.assertEqual(server.STATE, before)
        self.assertEqual(server.STATE_FILE.read_bytes(), disk_before)
        self.assertFalse(server.STATE_FILE.with_suffix('.tmp').exists())
        self.assertEqual(self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': True})[0], 200)
        self.assertEqual(len(server.STATE['saved_places']), 1)

    def test_simultaneous_native_ui_save_requests_produce_one_record(self):
        identifier = self.select()
        self.navigate_and_arrive(identifier)
        with ThreadPoolExecutor(max_workers=2) as callers:
            responses = list(callers.map(lambda _: self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': True}), range(2)))
        self.assertEqual([item[0] for item in responses], [200, 200])
        self.assertEqual(len(server.STATE['saved_places']), 1)

    def test_older_state_loads_empty_journey_fields_without_rewriting_file(self):
        legacy = {key: value for key, value in self.initial.items() if key not in ('trip', 'saved_places', 'recommendation_summary')}
        legacy_path = self.data/'legacy.json'
        legacy_path.write_text(json.dumps(legacy), encoding='utf-8')
        before = legacy_path.read_bytes()
        defaults = {**self.initial, 'trip': {}, 'saved_places': [], 'recommendation_summary': {}}
        loaded = server.load_state(legacy_path, defaults)
        self.assertEqual(loaded['trip'], {})
        self.assertEqual(loaded['saved_places'], [])
        self.assertEqual(loaded['recommendation_summary'], {})
        self.assertEqual(legacy_path.read_bytes(), before)

    def test_cancellation_releases_trip_and_late_events_cannot_reactivate_it(self):
        for source in ('pleos_cancelled', 'user_cancelled'):
            with self.subTest(source=source):
                identifier = self.select()
                if source == 'pleos_cancelled':
                    self.assertEqual(self.request('POST', '/api/trip/start', {'trip_id': identifier, 'provider': 'pleos'})[0], 200)
                status, result = self.request('POST', '/api/trip/cancel', {'trip_id': identifier, 'source': source})
                self.assertEqual(status, 200)
                marker = result['trip']
                self.assertEqual(set(marker), {'id', 'status', 'cancelled_at', 'cancellation_source'})
                self.assertEqual(marker['status'], 'cancelled')
                self.assertEqual(marker['cancellation_source'], source)
                self.assertEqual(result['saved_places'], [])
                self.assertEqual(self.request('POST', '/api/trip/cancel', {'trip_id': identifier, 'source': source})[1]['trip'], marker)
                self.assertEqual(self.navigate_and_arrive(identifier)['trip'], marker)
                self.assertEqual(self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': True})[0], 409)
                self.assertEqual(server.load_state(server.STATE_FILE, self.initial)['trip'], marker)
        replacement = self.select()
        self.assertNotEqual(replacement, identifier)
        self.assertEqual(server.STATE['trip']['status'], 'selected')
        self.assertEqual(self.request('POST', '/api/trip/cancel', {'trip_id': identifier, 'source': 'pleos_cancelled'})[0], 409)
        self.assertEqual(server.STATE['trip']['id'], replacement)

    def test_late_cancellation_preserves_arrival_prompt_saved_or_declined_decision(self):
        for save in (True, False):
            with self.subTest(save=save):
                identifier = self.select()
                self.navigate_and_arrive(identifier)
                before = copy.deepcopy(server.STATE)
                self.assertEqual(self.request('POST', '/api/trip/cancel', {'trip_id': identifier, 'source': 'pleos_cancelled'})[0], 200)
                self.assertEqual(server.STATE, before)
                self.assertEqual(self.request('POST', '/api/trip/decision', {'trip_id': identifier, 'save': save})[0], 200)
                before = copy.deepcopy(server.STATE)
                self.assertEqual(self.request('POST', '/api/trip/cancel', {'trip_id': identifier, 'source': 'user_cancelled'})[0], 200)
                self.assertEqual(server.STATE, before)

    def test_cancellation_rejects_unknown_source_without_changing_selected_trip(self):
        identifier = self.select()
        before = copy.deepcopy(server.STATE)
        self.assertEqual(self.request('POST', '/api/trip/cancel', {'trip_id': identifier, 'source': 'timeout'})[0], 400)
        self.assertEqual(server.STATE, before)


class JourneyValidationTests(unittest.TestCase):
    def test_selection_rejects_missing_or_invalid_coordinates_without_mutation(self):
        for bad in (None, float('nan'), float('inf'), 91, True, '37.39'):
            state = sample_state()
            state['recommendations'][0]['latitude'] = bad
            with self.subTest(latitude=bad), self.assertRaises(ValueError):
                journey.select_place(state, 'node/test-only-1')
            self.assertEqual(state['trip'], {})
        with self.assertRaises(ValueError):
            journey.select_place(sample_state(), 'not-a-recommendation')

    def test_selection_does_not_alias_mutable_conversation_or_place(self):
        original = sample_state()
        updated = journey.select_place(original, 'node/test-only-1')
        updated['recommendations'][0]['reasons'].clear()
        updated['recommendation_summary']['evidence'].clear()
        self.assertTrue(updated['trip']['place']['reasons'])
        self.assertTrue(updated['trip']['summary']['evidence'])
        self.assertTrue(original['recommendations'][0]['reasons'])

    def test_active_navigation_cannot_be_silently_replaced(self):
        state = sample_state()
        second = {**state['recommendations'][0], 'id': 'node/test-only-2'}
        state['recommendations'].append(second)
        state = journey.select_place(state, 'node/test-only-1')
        identifier = state['trip']['id']
        state = journey.start_trip(state, identifier, 'pleos')
        with self.assertRaises(journey.JourneyConflict):
            journey.select_place(state, second['id'])
        self.assertEqual(state['trip']['id'], identifier)


if __name__ == '__main__':
    unittest.main()
