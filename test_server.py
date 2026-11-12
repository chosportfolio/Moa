"""HTTP state/race regressions using temporary data and no speech or POI service."""
import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import server


class FakeEngine:
    ready = True
    message = '준비 완료'

    def profiles(self):
        return []

    def transcribe(self, path, allow_enrolled=True):
        return []


class ServerStateTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(__file__).resolve().parent/'tests'
        self.test_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=self.test_root)
        self.data = Path(self.directory.name)
        self.initial = {
            'segments': [{'id': 'utterance', 'seat': 'driver', 'text': '카페 좋아',
                          'start': 0, 'end': 2, 'confidence': None}],
            'recommendations': [], 'constraints': [], 'region': '판교역',
            'demo': False, 'notice': '', 'coverage': '', 'lookup_source': '',
            'conversation_id': 'test-conversation',
            'search_mode': 'conversation', 'search_location': {}, 'search_intent': {},
        }
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.location_clock = 100.0
        self.location_time = datetime(2026, 11, 10, tzinfo=timezone.utc)
        self.host_position = server.HostPosition(utc_now=lambda: self.location_time,
                                                monotonic=lambda: self.location_clock)
        # Synthetic fixture only; never use the live bridge's location or store.
        self.location_fix = {'latitude': 37.1234567, 'longitude': 127.7654321,
                             'accuracy_m': 145.5, 'source': 'WiFi',
                             'updated_at': self.location_time.isoformat()}
        self.patches = patch.multiple(server, DATA=self.data, STATE_FILE=self.data/'meeting.json',
                                      ENGINE=FakeEngine(), STATE=copy.deepcopy(self.initial),
                                      JOBS={}, ACTIVE_JOB=None, WORKER=self.worker, STREAMS={}, STREAM_CHUNKS={},
                                      HOST_POSITION=self.host_position, ALLOW_HOST_LOCATION=True,
                                      VEHICLE_POSITION=server.VehiclePosition(utc_now=lambda: self.location_time,
                                                                              monotonic=lambda: self.location_clock))
        self.patches.start()
        with server.LOCK:
            server.save_state()
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.worker.shutdown(wait=True)
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)
        self.patches.stop()
        self.assertTrue(self.data.resolve().is_relative_to(self.test_root.resolve()))
        self.directory.cleanup()

    def connection(self):
        return http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)

    def request(self, method, path, value=None, raw=None, headers=None):
        connection = self.connection()
        try:
            payload = raw if raw is not None else json.dumps(value or {}).encode()
            connection.request(method, path, body=payload if method != 'GET' else None, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def wait_job(self, identifier):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _, job = self.request('GET', '/api/jobs/' + identifier)
            if job['status'] in ('complete', 'error'):
                return job
            time.sleep(0.01)
        self.fail('Background job did not finish')

    def recommendation(self, *args):
        return {'constraints': [], 'recommendations': [], 'region': '판교역',
                'notice': '공개 장소 결과', 'coverage': '중심에서 동서·남북 800m',
                'lookup_source': 'OpenStreetMap · Map API'}

    def test_recommendation_metadata_is_persisted_and_cleared_on_edit(self):
        with patch('server.recommend', side_effect=self.recommendation):
            status, result = self.request('POST', '/api/recommend', {'region': '판교역'})
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
        _, state = self.request('GET', '/api/state')
        persisted = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
        for key in ('coverage', 'lookup_source'):
            self.assertEqual(state[key], persisted[key])
            self.assertIn(state[key], state['notice'])
        status, edited = self.request('PATCH', '/api/segments/utterance', {'text': '한식 좋아'})
        self.assertEqual(status, 200)
        self.assertEqual(edited['coverage'], '')
        self.assertEqual(edited['lookup_source'], '')
        self.assertNotIn('800m', edited['notice'])

    def test_reset_and_demo_invalidate_old_recommendation_metadata(self):
        for path in ('/api/reset', '/api/demo'):
            with self.subTest(path=path):
                with server.LOCK:
                    server.STATE.update(coverage='old', lookup_source='old', notice='old')
                status, state = self.request('POST', path)
                self.assertEqual(status, 200)
                self.assertEqual(state['coverage'], '')
                self.assertEqual(state['lookup_source'], '')

    def test_delayed_edit_cannot_pass_a_job_started_during_body_upload(self):
        body_reading = threading.Event()
        job_started = threading.Event()
        finish_job = threading.Event()
        original_json_body = server.Handler.json_body

        def read_body(handler):
            if handler.path == '/api/segments':
                body_reading.set()
            return original_json_body(handler)

        def recommend_slowly(*args):
            job_started.set()
            finish_job.wait(3)
            return self.recommendation()

        connection = self.connection()
        payload = json.dumps({'seat': 'passenger', 'text': '밥 먹자'}).encode()
        try:
            with patch.object(server.Handler, 'json_body', read_body), patch('server.recommend', side_effect=recommend_slowly):
                connection.putrequest('POST', '/api/segments')
                connection.putheader('Content-Length', str(len(payload)))
                connection.endheaders()
                self.assertTrue(body_reading.wait(1))
                status, job = self.request('POST', '/api/recommend', {'region': '판교역'})
                self.assertEqual(status, 202)
                self.assertTrue(job_started.wait(1))
                connection.send(payload)
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                self.assertEqual(len(server.STATE['segments']), 1)
                self.assertEqual(self.request('POST', '/api/reset')[0], 400)
                self.assertEqual(self.request('POST', '/api/demo')[0], 400)
                self.assertEqual(self.request('PATCH', '/api/segments/utterance', {'text': '바꿈'})[0], 400)
                finish_job.set()
                self.assertEqual(self.wait_job(job['job_id'])['status'], 'complete')
        finally:
            finish_job.set()
            connection.close()

    def test_silent_recording_preserves_transcript_and_removes_temporary_audio(self):
        with server.LOCK:
            server.STATE['demo'] = True
            server.save_state()
        before = copy.deepcopy(server.STATE)
        status, result = self.request('POST', '/api/transcribe', raw=b'fake audio')
        self.assertEqual(status, 202)
        job = self.wait_job(result['job_id'])
        self.assertEqual(job['status'], 'error')
        self.assertIn('기존 대화록은 유지', job['message'])
        self.assertEqual(server.STATE, before)
        self.assertFalse(list(self.data.glob('*.audio')))

    def test_new_transcription_appends_and_invalidates_previous_recommendations(self):
        with server.LOCK:
            server.STATE.update(coverage='old', lookup_source='old', notice='old',
                                recommendations=[{'name': 'old'}])
        new_segment = {'id': 'new', 'seat': 'passenger', 'text': '밥 먹자', 'start': 1, 'end': 2}
        with patch.object(server.ENGINE, 'transcribe', return_value=[new_segment]):
            status, result = self.request('POST', '/api/transcribe', raw=b'fake audio')
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
        _, state = self.request('GET', '/api/state')
        self.assertEqual(len(state['segments']), 2)
        self.assertEqual(state['segments'][1]['start'], 3)
        self.assertEqual(state['recommendations'], [])
        self.assertEqual(state['coverage'], '')
        self.assertEqual(state['lookup_source'], '')
        self.assertEqual(json.loads(server.STATE_FILE.read_text(encoding='utf-8'))['segments'], state['segments'])

    def test_failed_disk_commit_preserves_memory_and_saved_transcript(self):
        before = copy.deepcopy(server.STATE)
        saved = server.STATE_FILE.read_bytes()
        with patch('server.os.replace', side_effect=OSError('test disk error')):
            status, result = self.request('PATCH', '/api/segments/utterance', {'text': '다른 말'})
        self.assertEqual(status, 500)
        self.assertIn('기존 대화록은 유지', result['error'])
        self.assertEqual(server.STATE, before)
        self.assertEqual(server.STATE_FILE.read_bytes(), saved)

    def test_revision_changes_only_after_atomic_commit_without_mutating_candidate(self):
        before = copy.deepcopy(server.STATE)
        candidate = copy.deepcopy(before)
        candidate['region'] = '부산'
        proposed = copy.deepcopy(candidate)
        saved = server.STATE_FILE.read_bytes()
        with server.LOCK, patch('server.os.replace', side_effect=OSError('fixture commit failure')):
            with self.assertRaises(OSError):
                server.save_state(candidate)
        self.assertEqual(server.STATE, before)
        self.assertEqual(candidate, proposed, 'Even a failed save cannot modify the caller candidate')
        self.assertEqual(server.STATE_FILE.read_bytes(), saved)

        original_replace = server.os.replace
        def verify_then_commit(source, destination):
            self.assertEqual(server.STATE, before, 'Revision is not published before the disk replacement')
            self.assertEqual(candidate, proposed)
            return original_replace(source, destination)
        with server.LOCK, patch('server.os.replace', side_effect=verify_then_commit):
            server.save_state(candidate)
        self.assertEqual(candidate, proposed)
        self.assertEqual(server.STATE['state_revision'], before['state_revision'] + 1)
        self.assertEqual(json.loads(server.STATE_FILE.read_text(encoding='utf-8')), server.STATE)
        reloaded = server.load_state(server.STATE_FILE, {**self.initial, 'state_revision': 0})
        self.assertEqual(reloaded['state_revision'], server.STATE['state_revision'])
        with server.LOCK:
            server.STATE.clear()
            server.STATE.update(reloaded)
            server.save_state()
        self.assertEqual(server.STATE['state_revision'], before['state_revision'] + 2, 'Revision survives a state reload')

    def test_model_and_host_metadata_can_change_without_advancing_saved_revision(self):
        revision = server.STATE['state_revision']
        with patch.object(server.ENGINE, 'ready', False):
            _, loading = self.request('GET', '/api/state')
        self.assertEqual(self.request('POST', '/api/host-location', self.location_fix)[0], 200)
        _, ready = self.request('GET', '/api/state')
        self.assertEqual(loading['state_revision'], revision)
        self.assertEqual(ready['state_revision'], revision)
        self.assertFalse(loading['models']['ready'])
        self.assertTrue(ready['models']['ready'])
        self.assertTrue(ready['location']['available'])

    def test_vehicle_location_is_used_ahead_of_the_development_adapter(self):
        self.request('POST', '/api/host-location', self.location_fix)
        fix = {**self.location_fix, 'latitude':36.2345678, 'longitude':128.2345678,
               'source':'vehicle', 'confidence':0.75}
        fix.pop('accuracy_m')
        persisted = server.STATE_FILE.read_bytes()
        status, received = self.request('POST', '/api/vehicle-location', fix)
        self.assertEqual(status, 200)
        self.assertTrue(received['available'])
        self.assertIsNone(received['accuracy_m'], 'Confidence must never be converted to metres')
        self.assertEqual(server.STATE_FILE.read_bytes(), persisted)
        with patch('server.recommend', return_value=self.recommendation()) as recommend:
            status, response = self.request('POST', '/api/recommend', {'mode':'current'})
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(response['job_id'])['status'], 'complete')
            position = recommend.call_args.kwargs['location']
            self.assertEqual(position['latitude'], fix['latitude'])
            self.assertEqual(position['source'], 'vehicle')
        state = self.request('GET', '/api/state')[1]
        self.assertNotIn('host_location', state)
        for encoded in (json.dumps(state), server.STATE_FILE.read_text(encoding='utf-8')):
            self.assertNotIn(str(fix['latitude']), encoded)
            self.assertNotIn(str(fix['longitude']), encoded)

    def test_normal_mode_never_accepts_or_uses_pc_position(self):
        self.host_position.update(self.location_fix)
        with patch.object(server, 'ALLOW_HOST_LOCATION', False):
            self.assertEqual(self.request('POST', '/api/host-location', self.location_fix)[0], 403)
            state = self.request('GET', '/api/state')[1]
            self.assertFalse(state['location']['available'])
            self.assertFalse(state['development']['host_location_enabled'])
            with patch('server.recommend') as recommend:
                self.assertEqual(self.request('POST', '/api/recommend', {'mode':'current'})[0], 400)
                recommend.assert_not_called()

    def test_vehicle_permission_loss_immediately_invalidates_current_position(self):
        fix = {**self.location_fix, 'source':'vehicle'}
        with patch.object(server, 'ALLOW_HOST_LOCATION', False):
            self.assertEqual(self.request('POST', '/api/vehicle-location', fix)[0], 200)
            status, result = self.request('POST', '/api/vehicle-location',
                                          {'available':False, 'status':'permission_required'})
            self.assertEqual(status, 200)
            self.assertFalse(result['available'])
            self.assertIn('허용', result['message'])
            self.assertFalse(self.request('GET', '/api/state')[1]['location']['available'])
            self.assertEqual(self.request('POST', '/api/recommend', {'mode':'current'})[0], 400)

    def test_non_object_json_is_rejected(self):
        self.assertEqual(self.request('POST', '/api/recommend', raw=b'[]')[0], 400)

    def test_region_change_clears_old_results_but_preserves_trip_and_saved_places(self):
        with server.LOCK:
            server.STATE.update(recommendations=[{'name': '판교 장소'}],
                                recommendation_summary={'text': '이전 요약'},
                                search_mode='current', search_location={'source': 'WiFi'},
                                trip={'id': 'existing-trip', 'region': '판교역', 'status': 'navigating'},
                                saved_places=[{'id': 'visit', 'summary': '저장된 요약'}])
            before = copy.deepcopy(server.STATE)
        status, result = self.request('POST', '/api/region', {'region': ' 충주 '})
        self.assertEqual(status, 200)
        self.assertEqual(result['region'], '충주')
        self.assertEqual(result['search_mode'], 'manual')
        self.assertEqual(result['search_location'], {})
        self.assertEqual(result['recommendations'], [])
        self.assertEqual(result['recommendation_summary'], {})
        for field in ('segments', 'trip', 'saved_places'):
            self.assertEqual(result[field], before[field])
        self.assertEqual(json.loads(server.STATE_FILE.read_text(encoding='utf-8'))['region'], '충주')

    def test_fresh_host_location_is_used_for_search_and_coordinates_remain_in_memory(self):
        with server.LOCK:
            server.STATE.update(search_mode='manual', region='충주',
                                recommendations=[{'name': '수동 검색 결과'}])
            server.save_state()
            before = copy.deepcopy(server.STATE)
            persisted_before = server.STATE_FILE.read_bytes()
        status, public_location = self.request('POST', '/api/host-location', self.location_fix)
        self.assertEqual(status, 200)
        self.assertTrue(public_location['available'])
        self.assertEqual(server.STATE, before, 'A location update must not change a manually chosen region')
        self.assertEqual(server.STATE_FILE.read_bytes(), persisted_before, 'Receiving an exact fix must not persist it')

        status, changed = self.request('POST', '/api/search-mode', {'mode': 'current'})
        self.assertEqual(status, 200)
        self.assertEqual(changed['recommendations'], [])
        current_result = {**self.recommendation(), 'region': '현재 위치 주변',
                          'recommendations': [{'name': '현재 위치 근처 카페'}]}
        with patch('server.recommend', return_value=current_result) as recommend:
            status, result = self.request('POST', '/api/recommend', {'mode': 'current', 'region': '충주'})
            self.assertEqual(status, 202)
            job = self.wait_job(result['job_id'])
            self.assertEqual(job['status'], 'complete')
            recommend.assert_called_once()
            args, kwargs = recommend.call_args
            self.assertEqual(args[0], self.initial['segments'])
            self.assertEqual(args[1], '현재 위치 주변')
            self.assertEqual(kwargs['location'], self.location_fix)

        _, snapshot = self.request('GET', '/api/state')
        saved = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
        self.assertEqual(snapshot['search_mode'], 'current')
        self.assertEqual(snapshot['region'], '현재 위치 주변')
        self.assertEqual(snapshot['search_location'], {key: self.location_fix[key]
                         for key in ('accuracy_m', 'source', 'updated_at')})
        self.assertTrue(snapshot['location']['available'])
        self.assertNotIn('location', saved, 'Live host metadata is produced for the response only')
        for value in (public_location, snapshot, saved, job):
            with self.subTest(surface=type(value).__name__):
                encoded = json.dumps(value)
                self.assertNotIn(str(self.location_fix['latitude']), encoded)
                self.assertNotIn(str(self.location_fix['longitude']), encoded)
                self.assertNotIn('latitude', encoded)
                self.assertNotIn('longitude', encoded)

    def test_current_search_without_fresh_location_never_falls_back_to_manual_city(self):
        with server.LOCK:
            server.STATE.update(search_mode='manual', region='충주',
                                recommendations=[{'name': '기존 충주 결과'}])
            server.save_state()
        for condition in ('missing', 'expired'):
            with self.subTest(condition=condition):
                if condition == 'expired':
                    self.assertEqual(self.request('POST', '/api/host-location', self.location_fix)[0], 200)
                    self.location_clock += server.HostPosition.MAX_AGE + 1
                before = copy.deepcopy(server.STATE)
                persisted = server.STATE_FILE.read_bytes()
                with patch('server.recommend') as recommend:
                    status, result = self.request('POST', '/api/recommend', {'mode': 'current', 'region': '충주'})
                    self.assertEqual(status, 400)
                    self.assertIn('현재 위치', result['error'])
                    recommend.assert_not_called()
                self.assertEqual(server.JOBS, {})
                self.assertEqual(server.STATE, before)
                self.assertEqual(server.STATE_FILE.read_bytes(), persisted)
                self.assertFalse(self.request('GET', '/api/state')[1]['location']['available'])

    def test_current_mode_clears_old_results_and_preserves_transcript_trip_and_saved_places(self):
        with server.LOCK:
            server.STATE.update(search_mode='manual', region='충주',
                                recommendations=[{'name': '이전 장소'}],
                                recommendation_summary={'text': '이전 추천 이유'},
                                coverage='이전 검색 범위', lookup_source='이전 검색 제공자',
                                search_location={'source': 'WiFi'},
                                trip={'id': 'active-trip', 'status': 'navigating'},
                                saved_places=[{'id': 'saved-visit', 'summary': '저장한 이유'}])
            before = copy.deepcopy(server.STATE)
        status, result = self.request('POST', '/api/search-mode', {'mode': 'current'})
        self.assertEqual(status, 200)
        self.assertEqual(result['search_mode'], 'current')
        self.assertEqual(result['region'], '현재 위치 주변')
        for key, empty in (('recommendations', []), ('recommendation_summary', {}),
                           ('coverage', ''), ('lookup_source', ''), ('search_location', {})):
            self.assertEqual(result[key], empty)
        for key in ('segments', 'trip', 'saved_places', 'conversation_id'):
            self.assertEqual(result[key], before[key])
        saved = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
        self.assertEqual(saved['search_mode'], 'current')
        self.assertEqual(saved['segments'], before['segments'])
        self.assertFalse(result['location']['available'], 'Selecting current mode must not invent a fix')

    def test_conversation_destination_uses_busan_without_pc_location_or_fixed_region(self):
        with server.LOCK:
            server.STATE['segments'][0]['text'] = '부산 바다 보러 가자. 사람 붐비는 곳은 싫어.'
            server.STATE['region'] = '충주'
            original_segments = copy.deepcopy(server.STATE['segments'])
        original_resolve = server.resolve_search_intent
        original_queue = server.queue_job

        def resolve_locked(segments):
            self.assertTrue(server.LOCK._is_owned(), 'Intent resolution must share the reservation lock')
            return original_resolve(segments)

        def reserve_locked(*args):
            self.assertTrue(server.LOCK._is_owned(), 'Reserve the job without releasing the intent/snapshot lock')
            return original_queue(*args)

        with patch('server.resolve_search_intent', side_effect=resolve_locked), \
             patch('server.queue_job', side_effect=reserve_locked), \
             patch.object(server.HOST_POSITION, 'for_search', side_effect=AssertionError('Explicit destination must not use PC location')) as position, \
             patch('server.recommend', side_effect=self.recommendation) as recommend:
            # The default request follows the conversation, not the old region field.
            status, result = self.request('POST', '/api/recommend', {})
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
            position.assert_not_called()
            self.assertEqual(recommend.call_args.args[0], original_segments, 'Keep all speakers/preferences for recommendation')
            self.assertEqual(recommend.call_args.args[1], '부산')
            self.assertIsNone(recommend.call_args.kwargs.get('location'))
        _, snapshot = self.request('GET', '/api/state')
        self.assertEqual(snapshot['region'], '부산')
        self.assertEqual(snapshot['search_mode'], 'conversation')
        self.assertEqual(snapshot['search_location'], {})
        self.assertEqual(snapshot['search_intent']['area_source'], 'conversation')
        self.assertEqual(snapshot['search_intent']['evidence'][0]['text'], original_segments[0]['text'])
        persisted = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
        self.assertEqual(persisted['search_intent'], snapshot['search_intent'])
        self.assertEqual(self.request('PATCH', '/api/segments/utterance', {'text': '조용한 곳 좋아'})[0], 200)
        self.assertEqual(server.STATE['search_intent'], {}, 'Transcript edits invalidate the old area interpretation')

    def test_conversation_nearby_and_unspecified_area_use_fresh_pc_location(self):
        self.assertEqual(self.request('POST', '/api/host-location', self.location_fix)[0], 200)
        for text, source in [('근처 카페 가자', 'nearby'), ('조용하고 주차되는 카페가 좋아', 'unspecified')]:
            with self.subTest(source=source):
                with server.LOCK:
                    server.STATE['segments'][0]['text'] = text
                with patch('server.recommend', return_value=self.recommendation()) as recommend:
                    status, result = self.request('POST', '/api/recommend', {'mode': 'conversation', 'region': '충주'})
                    self.assertEqual(status, 202)
                    self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
                    self.assertEqual(recommend.call_args.args[1], '현재 위치 주변')
                    self.assertEqual(recommend.call_args.kwargs['location'], self.location_fix)
                    self.assertEqual(recommend.call_args.args[0][0]['text'], text)
                self.assertEqual(server.STATE['search_mode'], 'conversation')
                self.assertEqual(server.STATE['search_intent']['area_source'], source)
                self.assertEqual(server.STATE['search_location']['source'], 'WiFi')
                encoded = json.dumps(self.request('GET', '/api/state')[1]) + server.STATE_FILE.read_text(encoding='utf-8')
                self.assertNotIn(str(self.location_fix['latitude']), encoded)
                self.assertNotIn(str(self.location_fix['longitude']), encoded)

    def test_conversation_area_conflict_returns_choices_without_state_or_job_mutation(self):
        with server.LOCK:
            server.STATE['segments'] = [
                {**self.initial['segments'][0], 'text': '부산 바다 보러 가자'},
                {'id': 'other', 'seat': 'passenger', 'text': '충주에서 밥 먹자', 'start': 3, 'end': 5},
            ]
            server.STATE['recommendations'] = [{'name': '이전 결과'}]
            server.save_state()
            before = copy.deepcopy(server.STATE)
            saved = server.STATE_FILE.read_bytes()
        with patch('server.recommend') as recommend, patch.object(server.HOST_POSITION, 'for_search') as position:
            status, result = self.request('POST', '/api/recommend', {'mode': 'conversation'})
            self.assertEqual(status, 409)
            self.assertEqual(set(result['area_choices']), {'부산', '충주'})
            self.assertTrue(result['search_intent']['needs_area_choice'])
            self.assertEqual({entry['seat'] for entry in result['search_intent']['evidence']}, {'driver', 'passenger'})
            recommend.assert_not_called()
            position.assert_not_called()
        self.assertEqual(server.STATE, before)
        self.assertEqual(server.JOBS, {})
        self.assertEqual(server.STATE_FILE.read_bytes(), saved)

    def area_choice_conflict(self, text='부산 가자. 강릉 가자.'):
        # Reset only this fixture's temporary state between parameterized flows.
        # Independent, unreconciled proposals still require a choice. An
        # explicit "부산이나 강릉" list now correctly searches both regions.
        with server.LOCK:
            state = copy.deepcopy(self.initial)
            state['segments'][0]['text'] = text
            state['region'] = ''
            server.save_state(state)
        # If intent behavior regresses, fail without starting a real POI job.
        with patch('server.queue_job', return_value='unexpected-conflict-job') as queue:
            status, conflict = self.request('POST', '/api/recommend', {'mode': 'conversation'})
            queue.assert_not_called()
        self.assertEqual(status, 409)
        self.assertTrue(conflict['needs_area_choice'])
        self.assertRegex(conflict['area_choice_key'], r'^[0-9a-f]{64}$')
        return conflict

    def test_explicit_region_alternatives_without_time_budget_search_together(self):
        with server.LOCK:
            server.STATE['segments'][0]['text'] = '부산이나 강릉 바다 보러 가자.'
            server.save_state()
        with patch('server.recommend_regions', return_value=self.recommendation()) as regions, \
             patch('server.recommend') as single_region, \
             patch('server.current_position') as position:
            status, response = self.request('POST', '/api/recommend', {'mode': 'conversation'})
            self.assertEqual(status, 202, response)
            self.assertFalse(response['state']['search_intent']['needs_area_choice'])
            self.assertTrue(response['state']['search_intent']['multi_region'])
            self.assertEqual(self.wait_job(response['job_id'])['status'], 'complete')
            regions.assert_called_once()
            self.assertEqual(regions.call_args.args[1], ['부산', '강릉'])
            single_region.assert_not_called()
            position.assert_not_called()
        self.assertEqual(server.STATE['search_intent']['search_regions'], ['부산', '강릉'])
        self.assertEqual(server.STATE['region'], '부산 · 강릉')
        self.assertNotIn('area_choice', server.STATE['search_intent'])

    def assert_area_choice_rejected_without_mutation(self, choice, expected_status=409):
        with server.LOCK:
            before = copy.deepcopy(server.STATE)
            before_jobs = copy.deepcopy(server.JOBS)
            active = server.ACTIVE_JOB
            saved = server.STATE_FILE.read_bytes()
        with patch('server.recommend') as recommend, patch('server.queue_job') as queue:
            status, result = self.request('POST', '/api/recommend',
                                          {'mode': 'conversation', 'area_choice': choice})
            self.assertEqual(status, expected_status)
            self.assertIn('error', result)
            recommend.assert_not_called()
            queue.assert_not_called()
        self.assertEqual(server.STATE, before)
        self.assertEqual(server.JOBS, before_jobs)
        self.assertEqual(server.ACTIVE_JOB, active)
        self.assertEqual(server.STATE_FILE.read_bytes(), saved)

    def test_area_choice_is_conversation_scoped_and_reused_for_unchanged_dialogue(self):
        conflict = self.area_choice_conflict()
        self.assertEqual(set(conflict['area_choices']), {'부산', '강릉'})
        choice = {'region': '부산', 'key': conflict['area_choice_key']}
        with patch('server.recommend', return_value=self.recommendation()) as recommend, \
             patch.object(server.HOST_POSITION, 'for_search') as position:
            for options in ({'mode': 'conversation', 'area_choice': choice}, {'mode': 'conversation'}):
                status, result = self.request('POST', '/api/recommend', options)
                self.assertEqual(status, 202)
                self.assertEqual(result['state']['search_mode'], 'conversation')
                self.assertEqual(result['state']['search_intent']['area_choice'], choice)
                self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
                self.assertEqual(server.STATE['region'], '부산')
                self.assertEqual(server.STATE['search_mode'], 'conversation')
                self.assertFalse(server.STATE['search_intent']['needs_area_choice'])
            self.assertEqual([call.args[1] for call in recommend.call_args_list], ['부산', '부산'])
            position.assert_not_called()
        persisted = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
        self.assertEqual(persisted['search_intent']['area_choice'], choice)
        self.assertEqual(persisted['search_mode'], 'conversation')

    def test_area_choice_is_cleared_by_append_or_edit_and_follows_new_destination(self):
        self.assertEqual(self.request('POST', '/api/host-location', self.location_fix)[0], 200)
        for operation in ('append', 'edit'):
            for text, region in [('아니다 강릉 가자', '강릉'), ('아니다 근처 카페 가자', '현재 위치 주변')]:
                with self.subTest(operation=operation, region=region):
                    conflict = self.area_choice_conflict()
                    choice = {'region': '부산', 'key': conflict['area_choice_key']}
                    with patch('server.recommend', return_value=self.recommendation()) as recommend:
                        status, result = self.request('POST', '/api/recommend',
                                                      {'mode': 'conversation', 'area_choice': choice})
                        self.assertEqual(status, 202)
                        self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
                        if operation == 'append':
                            status, changed = self.request('POST', '/api/segments', {'seat': 'driver', 'text': text})
                        else:
                            status, changed = self.request('PATCH', '/api/segments/utterance', {'text': text})
                        self.assertEqual(status, 200)
                        self.assertEqual(changed['search_mode'], 'conversation')
                        self.assertEqual(changed['search_intent'], {})
                        status, result = self.request('POST', '/api/recommend', {'mode': 'conversation'})
                        self.assertEqual(status, 202)
                        self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
                        self.assertEqual(recommend.call_args.args[1], region)
                        self.assertEqual(recommend.call_args.kwargs.get('location'),
                                         self.location_fix if region == '현재 위치 주변' else None)
                    self.assertNotIn('area_choice', server.STATE['search_intent'])
                    self.assertEqual(server.STATE['search_mode'], 'conversation')

    def test_area_choice_from_old_dialog_is_rejected_after_basis_or_dialogue_changes(self):
        changes = [
            ('manual in another tab', 'POST', '/api/region', {'region': '서울'}),
            ('current in another tab', 'POST', '/api/search-mode', {'mode': 'current'}),
            ('new conversation', 'POST', '/api/reset', {}),
            ('edited utterance', 'PATCH', '/api/segments/utterance', {'text': '강릉 가자'}),
            ('new utterance', 'POST', '/api/segments', {'seat': 'driver', 'text': '아니다 강릉 가자'}),
        ]
        for label, method, path, payload in changes:
            with self.subTest(change=label):
                conflict = self.area_choice_conflict()
                choice = {'region': '부산', 'key': conflict['area_choice_key']}
                self.assertEqual(self.request(method, path, payload)[0], 200)
                self.assert_area_choice_rejected_without_mutation(choice)
                if label == 'manual in another tab':
                    self.assertEqual(server.STATE['search_mode'], 'manual')
                    self.assertEqual(server.STATE['region'], '서울')

    def test_area_choice_custom_region_resolves_conflicts_with_or_without_candidates(self):
        for text, expected_candidates in [('부산 가자. 강릉 가자.', {'부산', '강릉'}), ('부산은 싫어', set())]:
            with self.subTest(text=text):
                conflict = self.area_choice_conflict(text)
                self.assertEqual(set(conflict['area_choices']), expected_candidates)
                choice = {'region': '  서울  ', 'key': conflict['area_choice_key'], 'custom': True}
                with patch('server.recommend', return_value=self.recommendation()) as recommend:
                    status, result = self.request('POST', '/api/recommend',
                                                  {'mode': 'conversation', 'area_choice': choice})
                    self.assertEqual(status, 202)
                    self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
                    self.assertEqual(recommend.call_args.args[1], '서울')
                self.assertEqual(server.STATE['search_mode'], 'conversation')
                accepted = server.STATE['search_intent']['area_choice']
                self.assertEqual(accepted, {**choice, 'region': '서울'})
                self.assertFalse(server.STATE['search_intent']['needs_area_choice'])

    def test_area_choice_revalidates_candidates_and_custom_region_input(self):
        conflict = self.area_choice_conflict()
        key = conflict['area_choice_key']
        for choice, expected in [
            ({'region': '서울', 'key': key}, 409),
            ({'region': '서울', 'key': key, 'custom': 'true'}, 409),
            ({'region': '서울', 'key': 'old-key', 'custom': True}, 409),
            ({'region': '   ', 'key': key, 'custom': True}, 400),
            ({'region': '서울\n강남', 'key': key, 'custom': True}, 400),
            ({'region': 123, 'key': key, 'custom': True}, 400),
        ]:
            with self.subTest(choice=choice):
                self.assert_area_choice_rejected_without_mutation(choice, expected)
        # A remembered decision is also untrusted persisted input, not a bypass.
        with server.LOCK:
            updated = copy.deepcopy(server.STATE)
            updated['search_intent'] = {'area_choice': {'region': '서울', 'key': key}}
            server.save_state(updated)
            before = copy.deepcopy(server.STATE)
            saved = server.STATE_FILE.read_bytes()
        with patch('server.recommend') as recommend, patch('server.queue_job') as queue:
            status, result = self.request('POST', '/api/recommend', {'mode': 'conversation'})
            self.assertEqual(status, 409)
            self.assertEqual(set(result['area_choices']), {'부산', '강릉'})
            recommend.assert_not_called()
            queue.assert_not_called()
        self.assertEqual(server.STATE, before)
        self.assertEqual(server.STATE_FILE.read_bytes(), saved)

    def test_conversation_without_destination_requires_fresh_location(self):
        for condition in ('missing', 'expired'):
            with self.subTest(condition=condition):
                if condition == 'expired':
                    self.assertEqual(self.request('POST', '/api/host-location', self.location_fix)[0], 200)
                    self.location_clock += server.HostPosition.MAX_AGE + 1
                before = copy.deepcopy(server.STATE)
                with patch('server.recommend') as recommend:
                    status, result = self.request('POST', '/api/recommend', {'mode': 'conversation', 'region': '충주'})
                    self.assertEqual(status, 400)
                    self.assertIn('현재 위치', result['error'])
                    recommend.assert_not_called()
                self.assertEqual(server.STATE, before)
                self.assertEqual(server.JOBS, {})

    def test_excluded_only_area_requests_choice_even_without_candidate_regions(self):
        with server.LOCK:
            server.STATE['segments'][0]['text'] = '충주 말고 어디든'
            before = copy.deepcopy(server.STATE)
        with patch('server.recommend') as recommend, patch.object(server.HOST_POSITION, 'for_search') as position:
            status, result = self.request('POST', '/api/recommend', {'mode': 'conversation'})
            self.assertEqual(status, 409)
            self.assertTrue(result['needs_area_choice'])
            self.assertEqual(result['area_choices'], [])
            recommend.assert_not_called()
            position.assert_not_called()
        self.assertEqual(server.STATE, before)
        self.assertEqual(server.JOBS, {})

    def test_explicit_current_and_manual_modes_still_override_conversation_area(self):
        self.assertEqual(self.request('POST', '/api/host-location', self.location_fix)[0], 200)
        with server.LOCK:
            server.STATE['segments'][0]['text'] = '부산 바다 보러 가자'
        for mode, region in [('current', '현재 위치 주변'), ('manual', '충주')]:
            with self.subTest(mode=mode), patch('server.recommend', return_value=self.recommendation()) as recommend:
                status, result = self.request('POST', '/api/recommend', {'mode': mode, 'region': '충주'})
                self.assertEqual(status, 202)
                self.assertEqual(self.wait_job(result['job_id'])['status'], 'complete')
                self.assertEqual(recommend.call_args.args[1], region)
                self.assertEqual(recommend.call_args.kwargs.get('location'), self.location_fix if mode == 'current' else None)
                self.assertEqual(server.STATE['search_mode'], mode)
                self.assertEqual(server.STATE['search_intent'], {})

    def test_conversation_mode_and_reset_remove_fixed_area_but_preserve_saved_visits(self):
        with server.LOCK:
            server.STATE.update(search_mode='manual', region='충주', recommendations=[{'name': '이전 장소'}],
                                search_intent={'region': '충주'}, trip={'id': 'trip', 'status': 'saved'},
                                saved_places=[{'id': 'saved', 'summary': '함께 정한 이유'}])
            before = copy.deepcopy(server.STATE)
        status, changed = self.request('POST', '/api/search-mode', {'mode': 'conversation'})
        self.assertEqual(status, 200)
        self.assertEqual(changed['search_mode'], 'conversation')
        self.assertEqual(changed['region'], '')
        self.assertEqual(changed['recommendations'], [])
        self.assertEqual(changed['search_intent'], {})
        for field in ('segments', 'trip', 'saved_places', 'conversation_id'):
            self.assertEqual(changed[field], before[field])
        self.assertEqual(self.request('POST', '/api/region', {'region': '충주'})[0], 200)
        status, reset = self.request('POST', '/api/reset')
        self.assertEqual(status, 200)
        self.assertEqual(reset['search_mode'], 'conversation')
        self.assertEqual(reset['region'], '')
        self.assertEqual(reset['saved_places'], before['saved_places'])
        status, demo = self.request('POST', '/api/demo')
        self.assertEqual(status, 200)
        self.assertEqual(demo['search_mode'], 'conversation')
        self.assertEqual(demo['region'], '')
        self.assertIn(server.resolve_search_intent(demo['segments'])['area_source'], ('nearby', 'conversation'))
        self.assertEqual(demo['search_intent'], {})

    def test_random_sample_restores_real_conversation_after_reload_without_exposing_backup(self):
        with server.LOCK:
            server.STATE.update(saved_places=[{'id': 'real-saved'}],
                                recommendations=[{'id': 'real-recommendation'}],
                                recommendation_summary={'text': '실제 대화 요약'},
                                trip={'id': 'finished', 'status': 'saved'},
                                search_mode='manual', region='대전')
            original = copy.deepcopy(server.STATE)
        status, first = self.request('POST', '/api/demo')
        self.assertEqual(status, 200)
        self.assertTrue(first['demo_session'])
        self.assertNotIn('_demo_backup', first)
        self.assertEqual({row['seat'] for row in first['segments']}, {'driver', 'passenger', 'rear1', 'rear2'})
        self.assertTrue(all(row['seat_source'] == 'sample' for row in first['segments']))
        self.assertEqual(first['recommendations'], [])
        self.assertEqual(first['saved_places'], original['saved_places'])
        self.assertIsNone(first['job'])
        prior_id = first['demo_scenario']['id']
        for _ in range(8):
            status, sample = self.request('POST', '/api/demo')
            self.assertEqual(status, 200)
            self.assertNotEqual(sample['demo_scenario']['id'], prior_id)
            prior_id = sample['demo_scenario']['id']
        # Edit the sample and create a sample memory without changing the backup.
        self.request('PATCH', '/api/segments/' + sample['segments'][0]['id'], {'text': '샘플 수정'})
        with server.LOCK:
            server.STATE['saved_places'].append({'id': 'sample-saved', 'demo': True})
            server.save_state()
            loaded = server.load_state(server.STATE_FILE, {**self.initial, '_demo_backup': {},
                                       'demo_scenario': {}, 'saved_places': [], 'state_revision': 0})
            server.STATE.clear(); server.STATE.update(loaded)
        status, restored = self.request('POST', '/api/demo/exit')
        self.assertEqual(status, 200)
        self.assertFalse(restored['demo_session'])
        self.assertFalse(restored['demo'])
        for key in ('segments', 'recommendations', 'recommendation_summary', 'trip', 'region', 'search_mode'):
            self.assertEqual(restored[key], original[key])
        self.assertNotEqual(restored['conversation_id'], original['conversation_id'])
        self.assertNotEqual(restored['conversation_id'], sample['conversation_id'])
        self.assertEqual([item['id'] for item in restored['saved_places']], ['real-saved', 'sample-saved'])
        self.assertEqual(server.STATE['_demo_backup'], {})

    def test_sample_session_blocks_raw_recording_and_rejects_late_sample_audio(self):
        _, sample = self.request('POST', '/api/demo')
        before = copy.deepcopy(server.STATE)
        with patch.object(server.ENGINE, 'transcribe') as transcribe:
            self.assertEqual(self.request('POST', '/api/transcribe', raw=b'new audio')[0], 400)
            transcribe.assert_not_called()
        self.assertEqual(server.STATE, before)
        self.request('POST', '/api/demo/exit')
        status, _ = self.request('POST', '/api/transcribe', raw=b'late sample audio',
                                 headers={'X-Conversation-Id': sample['conversation_id']})
        self.assertEqual(status, 409)
        self.assertEqual(server.STATE['segments'], self.initial['segments'])
        self.assertFalse(list(self.data.glob('*.audio')))

    def test_new_conversation_from_sample_clears_backup_but_keeps_saved_visits(self):
        with server.LOCK:
            server.STATE['saved_places'] = [{'id': 'kept'}]
            server.STATE['_place_ratings'] = {'visit:p': {'rating': 4}}
        _, sample = self.request('POST', '/api/demo')
        status, fresh = self.request('POST', '/api/reset')
        self.assertEqual(status, 200)
        self.assertFalse(fresh['demo_session'])
        self.assertFalse(fresh['demo'])
        self.assertEqual(fresh['segments'], [])
        self.assertEqual(fresh['demo_scenario'], {})
        self.assertEqual(fresh['saved_places'], [{'id': 'kept'}])
        self.assertEqual(server.STATE['_place_ratings'], {'visit:p': {'rating': 4}})
        self.assertNotEqual(sample['conversation_id'], fresh['conversation_id'])
        self.assertEqual(self.request('POST', '/api/transcribe', raw=b'late', headers={'X-Conversation-Id':sample['conversation_id']})[0], 409)

    def test_rating_api_persists_and_only_exposes_visited_place_feedback(self):
        with server.LOCK:
            server.STATE['saved_places'] = [{'id':'visit-one','place':{'id':'node/1'},'arrived_at':'2026-11-12','demo':False}]
        status, rated = self.request('POST', '/api/ratings', {'visit_id':'visit-one','place_id':'node/1','rating':5,'note':'다시 가고 싶어'})
        self.assertEqual(status, 200)
        self.assertNotIn('_place_ratings', rated)
        self.assertEqual(rated['saved_places'][0]['place']['my_rating']['rating'], 5)
        self.assertEqual(json.loads(server.STATE_FILE.read_text(encoding='utf-8'))['_place_ratings']['visit:node/1']['note'], '다시 가고 싶어')
        before=copy.deepcopy(server.STATE)
        self.assertEqual(self.request('POST','/api/ratings',{'visit_id':'visit-one','place_id':'node/1','rating':True})[0],400)
        self.assertEqual(server.STATE,before)

    def test_demo_switch_cannot_discard_active_trip(self):
        for status in ('navigating', 'arrived'):
            with self.subTest(status=status):
                with server.LOCK:
                    server.STATE['trip'] = {'id': 'active', 'status': status}
                    before = copy.deepcopy(server.STATE)
                self.assertEqual(self.request('POST', '/api/demo')[0], 400)
                self.assertEqual(server.STATE, before)
        with server.LOCK:
            server.STATE['trip'] = {}
        self.request('POST', '/api/demo')
        with server.LOCK:
            server.STATE['trip'] = {'id': 'sample-active', 'status': 'navigating'}
            before = copy.deepcopy(server.STATE)
        self.assertEqual(self.request('POST', '/api/demo/exit')[0], 400)
        self.assertEqual(server.STATE, before)

    def test_selected_place_does_not_lock_reversible_samples(self):
        with server.LOCK:
            server.STATE['trip'] = {'id': 'chosen-only', 'status': 'selected'}
        status, result = self.request('POST', '/api/demo')
        self.assertEqual(status, 200)
        self.assertFalse(result['trip'])
        status, restored = self.request('POST', '/api/demo/exit')
        self.assertEqual(status, 200)
        self.assertEqual(restored['trip']['id'], 'chosen-only')

    def test_reset_discards_an_unstarted_selection(self):
        with server.LOCK:
            server.STATE['trip'] = {'id': 'chosen-only', 'status': 'selected'}
        status, result = self.request('POST', '/api/reset')
        self.assertEqual(status, 200)
        self.assertFalse(result['trip'])

    def test_load_repairs_only_explicitly_empty_orphan_selections_without_rewriting(self):
        orphan = {'id': 'old-choice', 'status': 'selected', 'place': {'id': 'old-place', 'name': '예전 식당'},
                  'selected_at': '2026-11-01T00:00:00+00:00', 'started_at': None, 'provider': None}
        saved = {**copy.deepcopy(self.initial), 'segments': [], 'recommendations': [], 'trip': orphan,
                 'saved_places': [{'id': 'keep-visit', 'place': {'name': '저장한 장소'}}],
                 '_demo_backup': {'segments': [], 'recommendations': [], 'trip': copy.deepcopy(orphan),
                                  'recommendation_summary': {'text': '예전 이유'}}}
        path = self.data/'legacy-orphan.json'
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        original_bytes = path.read_bytes()
        defaults = {**copy.deepcopy(self.initial), 'trip': {}, 'saved_places': [], '_demo_backup': {}}
        loaded = server.load_state(path, defaults)
        self.assertEqual(loaded['trip'], {})
        self.assertEqual(loaded['_demo_backup']['trip'], {})
        self.assertEqual(loaded['saved_places'], saved['saved_places'])
        self.assertEqual(loaded['_demo_backup']['recommendation_summary'], saved['_demo_backup']['recommendation_summary'])
        self.assertEqual(path.read_bytes(), original_bytes, 'Load migration must not rewrite user storage')
        self.assertEqual(defaults['trip'], {}, 'Defaults remain untouched')

    def test_load_preserves_active_completed_and_supported_selected_trips(self):
        for status in ('navigating', 'arrived', 'saved', 'dismissed', 'cancelled'):
            for location in ('current', 'backup'):
                with self.subTest(status=status, location=location):
                    trip = {'id': 'keep', 'status': status, 'place': {'name': '유지할 장소'}}
                    container = {'segments': [], 'recommendations': [], 'trip': trip}
                    saved = container if location == 'current' else {'_demo_backup': container}
                    path = self.data/'preserve-trip.json'
                    path.write_text(json.dumps(saved), encoding='utf-8')
                    loaded = server.load_state(path, {'segments': [], 'recommendations': [], 'trip': {}, '_demo_backup': {}})
                    actual = loaded if location == 'current' else loaded['_demo_backup']
                    self.assertEqual(actual['trip'], trip)
        selected = {'id': 'valid-choice', 'status': 'selected', 'place': {'name': '현재 식당'}}
        for context in (
                {'segments': [{'id': 'speech', 'text': '밥 먹자'}], 'recommendations': []},
                {'segments': [], 'recommendations': [{'id': 'place'}]},
                {'recommendations': []}, {'segments': []}, {}):
            for location in ('current', 'backup'):
                with self.subTest(context=context, location=location):
                    container = {**context, 'trip': selected}
                    saved = container if location == 'current' else {'_demo_backup': container}
                    path = self.data/'preserve-selected.json'
                    path.write_text(json.dumps(saved), encoding='utf-8')
                    loaded = server.load_state(path, {'segments': [], 'recommendations': [], 'trip': {}, '_demo_backup': {}})
                    actual = loaded if location == 'current' else loaded['_demo_backup']
                    self.assertEqual(actual['trip'], selected)

    def test_inconsistent_selected_with_guidance_or_save_evidence_is_preserved(self):
        for key in ('started_at', 'arrived_at', 'provider', 'arrival_source', 'decision_at', 'saved_at'):
            with self.subTest(key=key):
                selected = {'id': 'keep-evidence', 'status': 'selected', key: 'existing-value'}
                candidate = {**copy.deepcopy(server.STATE), 'segments': [], 'recommendations': [], 'trip': selected,
                             '_demo_backup': {'segments': [], 'recommendations': [], 'trip': copy.deepcopy(selected)}}
                with server.LOCK:
                    server.save_state(candidate)
                self.assertEqual(server.STATE['trip'], selected)
                self.assertEqual(server.STATE['_demo_backup']['trip'], selected)

    def test_orphan_cleanup_is_atomic_and_cannot_mutate_the_callers_candidate(self):
        candidate = {**copy.deepcopy(server.STATE), 'segments': [], 'recommendations': [],
                     'trip': {'id': 'orphan', 'status': 'selected'}, 'saved_places': [{'id': 'saved'}],
                     '_demo_backup': {'segments': [], 'recommendations': [],
                                      'trip': {'id': 'backup-orphan', 'status': 'selected'}}}
        proposed = copy.deepcopy(candidate)
        before = copy.deepcopy(server.STATE)
        disk_before = server.STATE_FILE.read_bytes()
        with server.LOCK, patch('server.os.replace', side_effect=OSError('fixture disk error')):
            with self.assertRaises(OSError):
                server.save_state(candidate)
        self.assertEqual(server.STATE, before)
        self.assertEqual(candidate, proposed)
        self.assertEqual(server.STATE_FILE.read_bytes(), disk_before)
        with server.LOCK:
            server.save_state(candidate)
        self.assertEqual(candidate, proposed)
        self.assertEqual(server.STATE['trip'], {})
        self.assertEqual(server.STATE['_demo_backup']['trip'], {})
        self.assertEqual(server.STATE['saved_places'], [{'id': 'saved'}])
        self.assertEqual(json.loads(server.STATE_FILE.read_text(encoding='utf-8')), server.STATE)

    def test_leaving_legacy_demo_cannot_restore_an_orphan_selection(self):
        with server.LOCK:
            server.STATE.update(demo=True, trip={}, saved_places=[{'id': 'keep'}],
                _demo_backup={'segments': [], 'recommendations': [], 'demo': False,
                              'trip': {'id': 'orphan-backup', 'status': 'selected'}})
        status, restored = self.request('POST', '/api/demo/exit')
        self.assertEqual(status, 200)
        self.assertEqual(restored['trip'], {})
        self.assertEqual(restored['segments'], [])
        self.assertEqual(restored['saved_places'], [{'id': 'keep'}])
        self.assertFalse(restored['demo_session'])

    def test_failed_search_in_new_region_does_not_present_old_city_results(self):
        with server.LOCK:
            server.STATE['recommendations'] = [{'name': '판교 장소'}]
        with patch('server.recommend', side_effect=ValueError('장소 서버 연결 실패')):
            status, result = self.request('POST', '/api/recommend', {'region': '충주'})
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(result['job_id'])['status'], 'error')
        self.assertEqual(server.STATE['region'], '충주')
        self.assertEqual(server.STATE['recommendations'], [])
        self.assertEqual(server.STATE['segments'], self.initial['segments'])

    def test_local_summary_is_visible_before_place_lookup_and_survives_lookup_failure(self):
        with server.LOCK:
            server.STATE['segments'][0]['text'] = '부산 바다 보고 싶어. 조용한 곳 좋아.'
            server.STATE.update(region='충주', recommendations=[{'name': '이전 지역의 장소'}],
                                recommendation_summary={'text': '이전 요약'},
                                saved_places=[{'id': 'saved', 'summary': '보관한 이유'}])
            before = copy.deepcopy(server.STATE)
        release = threading.Event()
        def failing_lookup(*args, **kwargs):
            release.wait(3)
            raise ValueError('외부 장소 검색 연결 실패')
        with patch('server.recommend', side_effect=failing_lookup):
            try:
                status, result = self.request('POST', '/api/recommend', {'mode': 'conversation'})
                self.assertEqual(status, 202)
                draft = result['state']
                summary = draft['recommendation_summary']
                self.assertEqual(summary['title'], '대화에서 나온 조건')
                self.assertTrue(summary['text'])
                self.assertEqual(summary['segment_count'], 1)
                self.assertEqual(summary['evidence'][0]['text'], before['segments'][0]['text'])
                self.assertEqual(draft['recommendations'], [])
                self.assertEqual(draft['region'], '부산')
                self.assertEqual(draft['search_intent']['region'], '부산')
                self.assertGreater(draft['state_revision'], before['state_revision'])
                self.assertEqual(draft['segments'], before['segments'])
                _, while_running = self.request('GET', '/api/state')
                self.assertEqual(while_running['recommendation_summary'], summary)
                self.assertEqual(json.loads(server.STATE_FILE.read_text(encoding='utf-8'))['recommendation_summary'], summary)
            finally:
                release.set()
            self.assertEqual(self.wait_job(result['job_id'])['status'], 'error')
        self.assertEqual(server.STATE['recommendation_summary'], summary)
        self.assertEqual(server.STATE['recommendations'], [])
        self.assertEqual(server.STATE['saved_places'], before['saved_places'])
        self.assertEqual(self.request('PATCH', '/api/segments/utterance', {'text': '한식 좋아'})[0], 200)
        self.assertEqual(server.STATE['recommendation_summary'], {})

    def test_invalid_region_is_rejected_without_job_or_state_mutation(self):
        before = copy.deepcopy(server.STATE)
        for path in ('/api/region', '/api/recommend'):
            for region in (None, {}, '', ' ', 'x' * 71, '충주\n서울'):
                with self.subTest(path=path, region=region):
                    self.assertEqual(self.request('POST', path, {'region': region})[0], 400)
                    self.assertEqual(server.STATE, before)
                    self.assertEqual(server.JOBS, {})

    def test_stream_chunks_keep_timing_do_not_guess_seats_and_are_idempotent(self):
        def recognized(*args, **kwargs):
            self.assertFalse(kwargs['allow_enrolled'])
            return [{'id':str(time.monotonic()),'seat':'driver','text':'카페 좋아', 'start':0.2,'end':1.2}]
        with patch.object(server.ENGINE, 'transcribe', side_effect=recognized) as transcribe:
            for sequence, started in [(0, 1), (1, 7)]:
                headers={'X-Stream-Id':'test-stream','X-Sequence':str(sequence),'X-Start-Seconds':str(started)}
                status, response = self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
                self.assertEqual(status,202)
                self.assertEqual(self.wait_job(response['job_id'])['status'],'complete')
                duplicate_status, duplicate = self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
                self.assertEqual(duplicate_status, 200)
                self.assertTrue(duplicate['committed'])
                self.assertTrue(duplicate['duplicate'])
            self.assertEqual(transcribe.call_count,2)
        utterances=server.STATE['segments'][1:]
        self.assertEqual([s['start'] for s in utterances],[3.2,9.2])
        self.assertTrue(all(s['seat']=='unknown' and s['seat_source']=='unavailable' for s in utterances))
        self.assertTrue(all('spoken_at' not in s and 'time_source' not in s for s in server.STATE['segments']))

    def test_stream_capture_clock_uses_chunk_and_segment_offsets_after_restart(self):
        headers = {'X-Stream-Id': 'clock-stream', 'X-Sequence': '0', 'X-Start-Seconds': '1',
                   'X-Recording-Started-At': '2026-11-12T09:00:00.120+09:00'}

        def recognized(*args, **kwargs):
            return [{'id': str(time.monotonic()), 'seat': 'driver', 'text': '커피 좋아',
                     'start': 0.2, 'end': 1.2}]

        with patch.object(server.ENGINE, 'transcribe', side_effect=recognized) as transcribe:
            status, first = self.request('POST', '/api/transcribe?stream=1', raw=b'audio', headers=headers)
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(first['job_id'])['status'], 'complete')
            persisted = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
            self.assertEqual(persisted['segments'][-1]['spoken_at'], '2026-11-12T00:00:01.320Z')
            self.assertEqual(persisted['_audio_streams']['clock-stream']['recording_started_at'],
                             '2026-11-12T00:00:00.120Z')
            with server.LOCK:
                defaults = copy.deepcopy(server.STATE)
                defaults.update(segments=[], _audio_streams={})
                loaded = server.load_state(server.STATE_FILE, defaults)
                server.STATE.clear()
                server.STATE.update(loaded)
                server.JOBS.clear()
                server.STREAMS.clear()
                server.STREAM_CHUNKS.clear()
                server.ACTIVE_JOB = None
                before = copy.deepcopy(server.STATE)
            headers['X-Recording-Started-At'] = '2026-11-12T00:00:00.120Z'
            status, duplicate = self.request('POST', '/api/transcribe?stream=1', raw=b'audio', headers=headers)
            self.assertEqual(status, 200)
            self.assertTrue(duplicate['committed'])
            self.assertEqual(server.STATE, before)
            headers.pop('X-Recording-Started-At')
            headers.update({'X-Sequence': '1', 'X-Start-Seconds': '7'})
            status, second = self.request('POST', '/api/transcribe?stream=1', raw=b'next audio', headers=headers)
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(second['job_id'])['status'], 'complete')
            self.assertEqual(transcribe.call_count, 2)
        utterances = server.STATE['segments'][1:]
        self.assertEqual([segment['start'] for segment in utterances], [3.2, 9.2])
        self.assertEqual([segment['spoken_at'] for segment in utterances],
                         ['2026-11-12T00:00:01.320Z', '2026-11-12T00:00:07.320Z'])
        self.assertTrue(all(segment['time_source'] == 'capture' for segment in utterances))
        status, visible = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(visible['segments'], server.STATE['segments'])
        self.assertNotIn('spoken_at', visible['segments'][0])
        self.assertNotIn('_audio_streams', visible)

    def test_whole_audio_capture_clock_uses_engine_offsets_without_conversation_base(self):
        def recognized(*args, **kwargs):
            return [{'id': 'first-clock', 'seat': 'unknown', 'text': '커피 좋아', 'start': 0.25, 'end': 0.9},
                    {'id': 'second-clock', 'seat': 'unknown', 'text': '밥도 먹자', 'start': 1.5, 'end': 2}]

        with patch.object(server.ENGINE, 'transcribe', side_effect=recognized):
            status, response = self.request('POST', '/api/transcribe', raw=b'audio', headers={
                'X-Recording-Started-At': '2026-11-12T09:00:00+09:00', 'X-Start-Seconds': '60'})
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(response['job_id'])['status'], 'complete')
        utterances = server.STATE['segments'][1:]
        self.assertEqual([segment['start'] for segment in utterances], [2.25, 3.5])
        self.assertEqual([segment['spoken_at'] for segment in utterances],
                         ['2026-11-12T00:00:00.250Z', '2026-11-12T00:00:01.500Z'])
        self.assertTrue(all(segment['time_source'] == 'capture' for segment in utterances))
        self.assertNotIn('spoken_at', server.STATE['segments'][0])

    def test_invalid_or_conflicting_capture_clock_rejects_without_mutation(self):
        before = copy.deepcopy(server.STATE)
        with patch.object(server.ENGINE, 'transcribe', return_value=[]) as transcribe:
            for path in ('/api/transcribe', '/api/transcribe?stream=1'):
                for invalid in ('2026-11-12T09:00:00', 'not-a-clock', '2026-02-30T09:00:00Z'):
                    with self.subTest(path=path, invalid=invalid):
                        headers = {'X-Stream-Id': 'invalid-clock', 'X-Sequence': '0',
                                   'X-Recording-Started-At': invalid}
                        self.assertEqual(self.request('POST', path, raw=b'audio', headers=headers)[0], 400)
            transcribe.assert_not_called()
            self.assertEqual(server.STATE, before)
            self.assertFalse(list(self.data.glob('*.audio')))
            headers = {'X-Stream-Id': 'pinned-clock', 'X-Sequence': '0',
                       'X-Recording-Started-At': '2026-11-12T00:00:00Z'}
            _, first = self.request('POST', '/api/transcribe?stream=1', raw=b'audio', headers=headers)
            self.assertEqual(self.wait_job(first['job_id'])['status'], 'complete')
            before = copy.deepcopy(server.STATE)
            headers['X-Recording-Started-At'] = '2026-11-12T00:00:01Z'
            for sequence in ('0', '1'):
                headers['X-Sequence'] = sequence
                self.assertEqual(self.request('POST', '/api/transcribe?stream=1', raw=b'audio', headers=headers)[0], 400)
            self.assertEqual(transcribe.call_count, 1)
            self.assertEqual(server.STATE, before)

    def test_legacy_audio_retry_with_added_clock_does_not_retime_saved_segments(self):
        headers = {'X-Stream-Id': 'legacy-clock', 'X-Sequence': '0', 'X-Start-Seconds': '0'}
        with patch.object(server.ENGINE, 'transcribe', return_value=[{
                'id': 'legacy-recording', 'seat': 'unknown', 'text': '한식 좋아', 'start': 0, 'end': 1}]):
            _, first = self.request('POST', '/api/transcribe?stream=1', raw=b'audio', headers=headers)
            self.assertEqual(self.wait_job(first['job_id'])['status'], 'complete')
        before = copy.deepcopy(server.STATE)
        headers['X-Recording-Started-At'] = '2026-11-12T00:00:00Z'
        with patch.object(server.ENGINE, 'transcribe') as transcribe:
            status, duplicate = self.request('POST', '/api/transcribe?stream=1', raw=b'audio', headers=headers)
            self.assertEqual(status, 200)
            self.assertTrue(duplicate['committed'])
            transcribe.assert_not_called()
        self.assertEqual(server.STATE, before)
        self.assertTrue(all('spoken_at' not in segment for segment in server.STATE['segments']))

    def test_pending_audio_duplicate_cannot_change_its_capture_origin(self):
        headers = {'X-Stream-Id': 'pending-clock', 'X-Sequence': '0',
                   'X-Recording-Started-At': '2026-11-12T09:00:00+09:00'}
        started = threading.Event()
        release = threading.Event()

        def recognized(*args, **kwargs):
            started.set()
            release.wait(3)
            return [{'id': 'pending-recording', 'seat': 'unknown', 'text': '한식 좋아',
                     'start': 0.25, 'end': 1}]

        with patch.object(server.ENGINE, 'transcribe', side_effect=recognized) as transcribe:
            try:
                status, first = self.request('POST', '/api/transcribe?stream=1', raw=b'audio', headers=headers)
                self.assertEqual(status, 202)
                self.assertTrue(started.wait(1))
                headers['X-Recording-Started-At'] = '2026-11-12T00:00:00.000Z'
                status, duplicate = self.request('POST', '/api/transcribe?stream=1', raw=b'audio', headers=headers)
                self.assertEqual(status, 202)
                self.assertEqual(duplicate['job_id'], first['job_id'])
                headers['X-Recording-Started-At'] = '2026-11-12T00:00:01Z'
                self.assertEqual(self.request('POST', '/api/transcribe?stream=1', raw=b'audio', headers=headers)[0], 400)
            finally:
                release.set()
            self.assertEqual(self.wait_job(first['job_id'])['status'], 'complete')
            self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(server.STATE['segments'][-1]['spoken_at'], '2026-11-12T00:00:00.250Z')

    def test_sample_clocks_survive_disk_reload_and_exit_restores_legacy_rows(self):
        status, sample = self.request('POST', '/api/demo')
        self.assertEqual(status, 200)
        sample_rows = copy.deepcopy(sample['segments'])
        self.assertTrue(all(segment['spoken_at'].endswith('Z') and segment['time_source'] == 'sample'
                            for segment in sample_rows))
        with server.LOCK:
            defaults = copy.deepcopy(server.STATE)
            defaults.update(segments=[], _demo_backup={})
            loaded = server.load_state(server.STATE_FILE, defaults)
            server.STATE.clear()
            server.STATE.update(loaded)
        self.assertEqual(self.request('GET', '/api/state')[1]['segments'], sample_rows)
        self.assertEqual(json.loads(server.STATE_FILE.read_text(encoding='utf-8'))['segments'], sample_rows)
        status, restored = self.request('POST', '/api/demo/exit')
        self.assertEqual(status, 200)
        self.assertEqual(restored['segments'], self.initial['segments'])
        self.assertNotIn('spoken_at', restored['segments'][0])

    def test_export_uses_saved_local_date_and_clock_and_labels_unknown_times(self):
        spoken_at = '2026-11-12T03:24:56.789Z'
        with server.LOCK:
            candidate = copy.deepcopy(server.STATE)
            candidate['segments'] = [
                {'id': 'clocked', 'seat': 'driver', 'text': '시각이 있는 발언',
                 'start': 0, 'end': 2, 'spoken_at': spoken_at},
                {'id': 'legacy', 'seat': 'passenger', 'text': '예전 발언', 'start': 2, 'end': 4},
                {'id': 'invalid-clock', 'seat': 'unknown', 'text': '잘못된 시각 발언',
                 'start': 4, 'end': 5, 'spoken_at': 'not-a-clock'},
                {'id': 'naive-clock', 'seat': 'unknown', 'text': '시간대 없는 발언',
                 'start': 5, 'end': 6, 'spoken_at': '2026-11-12T03:24:56'},
            ]
            server.save_state(candidate)
            before = copy.deepcopy(server.STATE)
        connection = self.connection()
        try:
            connection.request('GET', '/api/export')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn('text/markdown', response.getheader('Content-Type'))
            exported = response.read().decode('utf-8')
        finally:
            connection.close()
        expected_clock = datetime.fromisoformat(spoken_at.replace('Z', '+00:00')).astimezone().strftime(
            '%Y-%m-%d %H:%M:%S %z')
        utterance_lines = [line for line in exported.splitlines() if line.startswith('- ')]
        self.assertEqual(len(utterance_lines), 4)
        self.assertTrue(utterance_lines[0].startswith('- 기록 시각 ' + expected_clock + ' **'))
        self.assertTrue(all(line.startswith('- 시각 미상 **') for line in utterance_lines[1:]))
        self.assertNotIn('- 00:00', exported)
        self.assertEqual(server.STATE, before)

    def test_silent_stream_does_not_replace_sample_or_raise_error(self):
        before=copy.deepcopy(server.STATE)
        status,response=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers={
            'X-Stream-Id':'silent-stream','X-Sequence':'0','X-Start-Seconds':'0'})
        self.assertEqual(status,202)
        self.assertEqual(self.wait_job(response['job_id'])['status'],'complete')
        self.assertEqual({key:value for key,value in server.STATE.items() if key not in ('_audio_streams','state_revision')},
                         {key:value for key,value in before.items() if key != 'state_revision'})
        self.assertEqual(server.STATE['state_revision'], before['state_revision'] + 1)
        self.assertTrue(server.STATE['_audio_streams']['silent-stream']['receipts'])

    def test_stream_receipt_survives_restart_and_preserves_session_time_base(self):
        headers={'X-Stream-Id':'restart-stream','X-Sequence':'0','X-Start-Seconds':'1'}
        def recognized(*args, **kwargs):
            return [{'id':str(time.monotonic()),'seat':'unknown','text':'커피 좋아', 'start':0.2,'end':1.2}]
        with patch.object(server.ENGINE, 'transcribe', side_effect=recognized) as transcribe:
            _, first=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
            self.assertEqual(self.wait_job(first['job_id'])['status'],'complete')
            persisted=json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
            with server.LOCK:
                server.STATE.clear(); server.STATE.update(persisted)
                server.JOBS.clear(); server.STREAMS.clear(); server.STREAM_CHUNKS.clear(); server.ACTIVE_JOB=None
            with patch.object(server.ENGINE,'ready',False):
                status,duplicate=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
            self.assertEqual(status,200)
            self.assertTrue(duplicate['committed'])
            headers.update({'X-Sequence':'1','X-Start-Seconds':'7'})
            _, second=self.request('POST','/api/transcribe?stream=1',raw=b'next audio',headers=headers)
            self.assertEqual(self.wait_job(second['job_id'])['status'],'complete')
            self.assertEqual(transcribe.call_count,2)
        self.assertEqual([s['start'] for s in server.STATE['segments'][1:]],[3.2,9.2])
        self.assertNotIn('_audio_streams',self.request('GET','/api/state')[1])

    def test_stream_disk_failure_can_retry_without_duplicate_transcript(self):
        headers={'X-Stream-Id':'retry-stream','X-Sequence':'0','X-Start-Seconds':'0'}
        def recognized(*args, **kwargs):
            return [{'id':str(time.monotonic()),'seat':'unknown','text':'한식 좋아','start':0,'end':1}]
        with patch.object(server.ENGINE,'transcribe',side_effect=recognized) as transcribe:
            with patch('server.os.replace',side_effect=OSError('test disk error')):
                _,first=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
                self.assertEqual(self.wait_job(first['job_id'])['status'],'error')
            self.assertEqual(server.STATE['segments'],self.initial['segments'])
            _,retry=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
            self.assertNotEqual(first['job_id'],retry['job_id'])
            self.assertEqual(self.wait_job(retry['job_id'])['status'],'complete')
            self.assertEqual(transcribe.call_count,2)
        self.assertEqual(len(server.STATE['segments']),2)
        self.assertFalse(list(self.data.glob('*.audio')))

    def test_changed_audio_cannot_reuse_completed_or_pending_sequence(self):
        headers={'X-Stream-Id':'hash-stream','X-Sequence':'0','X-Start-Seconds':'0'}
        release=threading.Event()
        def recognized(*args, **kwargs):
            release.wait(2)
            return []
        with patch.object(server.ENGINE,'transcribe',side_effect=recognized) as transcribe:
            try:
                _,first=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
                self.assertEqual(self.request('POST','/api/transcribe?stream=1',raw=b'changed',headers=headers)[0],400)
            finally:
                release.set()
            self.assertEqual(self.wait_job(first['job_id'])['status'],'complete')
            self.assertEqual(self.request('POST','/api/transcribe?stream=1',raw=b'changed',headers=headers)[0],400)
            self.assertEqual(transcribe.call_count,1)

    def test_reset_does_not_resurrect_old_audio_session(self):
        headers={'X-Stream-Id':'finished-stream','X-Sequence':'0','X-Start-Seconds':'0'}
        _,first=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
        self.assertEqual(self.wait_job(first['job_id'])['status'],'complete')
        self.assertEqual(self.request('POST','/api/reset')[0],200)
        self.assertTrue(self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)[1]['committed'])
        headers['X-Sequence']='1'
        self.assertEqual(self.request('POST','/api/transcribe?stream=1',raw=b'old next audio',headers=headers)[0],400)
        self.assertEqual(server.STATE['segments'],[])

    def test_concurrent_duplicate_is_reserved_only_once_after_body_write(self):
        headers={'X-Stream-Id':'concurrent-stream','X-Sequence':'0','X-Start-Seconds':'0'}
        reached_write=threading.Event(); release_write=threading.Event()
        original_write=Path.write_bytes
        delayed_result=[]
        # The request is handled on a server thread, so tag just its first
        # audio write rather than relying on client thread identity.
        first_write=True
        def gate_write(path,data):
            nonlocal first_write
            if path.suffix=='.audio' and first_write:
                first_write=False
                reached_write.set(); release_write.wait(3)
            return original_write(path,data)
        def delayed_request():
            delayed_result.append(self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers))
        with patch.object(Path,'write_bytes',gate_write), patch.object(server.ENGINE,'transcribe',return_value=[]) as transcribe:
            request_thread=threading.Thread(target=delayed_request)
            request_thread.start()
            try:
                self.assertTrue(reached_write.wait(1))
                _,first=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
                self.assertEqual(self.wait_job(first['job_id'])['status'],'complete')
            finally:
                release_write.set(); request_thread.join(3)
            self.assertFalse(request_thread.is_alive())
            self.assertEqual(delayed_result[0][0],200)
            self.assertTrue(delayed_result[0][1]['committed'])
            self.assertEqual(transcribe.call_count,1)
        self.assertFalse(list(self.data.glob('*.audio')))

    def test_partial_audio_file_is_removed_when_disk_write_fails(self):
        original_write=Path.write_bytes
        def partial_write(path,data):
            if path.suffix=='.audio':
                original_write(path,data[:2])
                raise OSError('test disk full')
            return original_write(path,data)
        with patch.object(Path,'write_bytes',partial_write):
            self.assertEqual(self.request('POST','/api/transcribe',raw=b'audio')[0],500)
        self.assertFalse(list(self.data.glob('*.audio')))
        self.assertEqual(server.STATE['segments'],self.initial['segments'])

    def test_legacy_conversation_identity_is_migrated_once_and_persisted(self):
        with server.LOCK:
            server.STATE.pop('conversation_id')
        _, first = self.request('GET','/api/state')
        persisted = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
        self.assertTrue(first['conversation_id'])
        self.assertEqual(first['conversation_id'],persisted['conversation_id'])
        self.assertEqual(self.request('GET','/api/state')[1]['conversation_id'],first['conversation_id'])
        self.assertEqual(first['segments'],self.initial['segments'])

    def test_reset_and_demo_reject_first_delayed_chunk_from_previous_conversation(self):
        for path in ('/api/reset','/api/demo'):
            previous = server.STATE['conversation_id']
            _, changed = self.request('POST',path)
            self.assertNotEqual(changed['conversation_id'],previous)
            before = copy.deepcopy(server.STATE)
            with patch.object(server.ENGINE,'transcribe') as transcribe:
                for stream in (False,True):
                    headers={'X-Conversation-Id':previous,'X-Stream-Id':'never-seen','X-Sequence':'0'}
                    status, result = self.request('POST','/api/transcribe'+('?stream=1' if stream else ''),raw=b'audio',headers=headers)
                    self.assertEqual(status,409)
                    self.assertIn('이전 대화',result['error'])
                transcribe.assert_not_called()
            self.assertEqual(server.STATE,before)
            self.assertFalse(list(self.data.glob('*.audio')))

    def test_acknowledged_old_conversation_duplicate_can_still_release_native_audio(self):
        headers={'X-Conversation-Id':server.STATE['conversation_id'],'X-Stream-Id':'acked','X-Sequence':'0'}
        _, first=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
        self.assertEqual(self.wait_job(first['job_id'])['status'],'complete')
        self.request('POST','/api/reset')
        status,result=self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)
        self.assertEqual(status,200)
        self.assertTrue(result['committed'])
        self.assertEqual(server.STATE['segments'],[])

    def test_reset_during_first_chunk_file_write_cannot_join_new_conversation(self):
        reached=threading.Event(); release=threading.Event(); responses=[]
        original_write=Path.write_bytes
        def gated_write(path,data):
            if path.suffix=='.audio':
                reached.set(); release.wait(3)
            return original_write(path,data)
        headers={'X-Conversation-Id':server.STATE['conversation_id'],'X-Stream-Id':'late-first','X-Sequence':'0'}
        with patch.object(Path,'write_bytes',gated_write), patch.object(server.ENGINE,'transcribe') as transcribe:
            thread=threading.Thread(target=lambda:responses.append(self.request('POST','/api/transcribe?stream=1',raw=b'audio',headers=headers)))
            thread.start()
            try:
                self.assertTrue(reached.wait(1))
                self.assertEqual(self.request('POST','/api/reset')[0],200)
            finally:
                release.set(); thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(responses[0][0],409)
            transcribe.assert_not_called()
        self.assertEqual(server.STATE['segments'],[])
        self.assertFalse(list(self.data.glob('*.audio')))

    def test_legacy_upload_also_cannot_cross_reset_during_body_transfer(self):
        reading=threading.Event(); original_body=server.Handler.body
        def observed_body(handler,*args,**kwargs):
            if handler.path=='/api/transcribe':reading.set()
            return original_body(handler,*args,**kwargs)
        connection=self.connection()
        try:
            with patch.object(server.Handler,'body',observed_body):
                connection.putrequest('POST','/api/transcribe')
                connection.putheader('Content-Length','5'); connection.endheaders()
                self.assertTrue(reading.wait(1))
                self.assertEqual(self.request('POST','/api/reset')[0],200)
                connection.send(b'audio')
                response=connection.getresponse()
                self.assertEqual(response.status,409); response.read()
        finally:connection.close()
        self.assertEqual(server.STATE['segments'],[])


if __name__ == '__main__':
    unittest.main()
