"""Official speech-final validation and local HTTP persistence regressions."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

import native_transcripts as native
import server
import test_server as server_test_helpers


def final(**changes):
    event = {'conversation_id': 'test-conversation', 'session_id': 'native-session',
             'sequence': 1, 'text': ' 바다 보고 칼국수도 먹고 싶어. ',
             'start': 0, 'end': 3.5, 'source': 'pleos_on_device'}
    event.update(changes)
    return event


class NativeTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.state = {'conversation_id': 'test-conversation', 'segments': [
            {'id': 'before', 'seat': 'driver', 'text': '출발하자', 'start': 0, 'end': 7}],
            'saved_places': [{'id': 'memory'}]}

    def stage(self, state=None, **changes):
        return native.stage_final(self.state if state is None else state,
                                  native.validate_final(final(**changes)))

    def test_exact_text_unknown_seat_and_fixed_session_offset(self):
        before = copy.deepcopy(self.state)
        first = self.stage(seat='driver')
        self.assertEqual(self.state, before)
        segment = first['segments'][-1]
        self.assertEqual(segment['text'], final()['text'])
        self.assertEqual(segment['seat'], 'unknown')
        self.assertEqual((segment['start'], segment['end']), (7, 10.5))
        second = self.stage(first, sequence=2, start=4, end=6, text='나는 회도 좋아.')
        self.assertEqual((second['segments'][-1]['start'], second['segments'][-1]['end']), (11, 13))
        next_session = self.stage(second, session_id='next-session', start=1, end=2)
        self.assertEqual(next_session['segments'][-1]['start'], 14)
        self.assertEqual(next_session['saved_places'], before['saved_places'])

    def test_retry_is_noop_but_changed_text_or_timing_conflicts(self):
        first = self.stage()
        self.assertIsNone(self.stage(first))
        # Integer and equivalent decimal JSON times refer to the same final.
        self.assertIsNone(self.stage(first, start=0.0))
        for change in ({'text': '다른 내용'}, {'start': 1}, {'end': 4}):
            with self.subTest(change=change), self.assertRaises(native.NativeTranscriptConflict):
                self.stage(first, **change)

    def test_capture_clock_is_separate_from_conversation_offsets_and_pinned_per_session(self):
        first = self.stage(recording_started_at='2026-11-12T09:00:00.120+09:00', start=1.25)
        segment = first['segments'][-1]
        self.assertEqual(segment['start'], 8.25)
        self.assertEqual(segment['spoken_at'], '2026-11-12T00:00:01.370Z')
        self.assertEqual(segment['time_source'], 'capture')
        self.assertNotIn('spoken_at', first['segments'][0])
        self.assertEqual(first['_speech_streams']['native-session']['recording_started_at'],
                         '2026-11-12T00:00:00.120Z')
        second = self.stage(first, sequence=2, start=5, end=6)
        self.assertEqual(second['segments'][-1]['start'], 12)
        self.assertEqual(second['segments'][-1]['spoken_at'], '2026-11-12T00:00:05.120Z')
        next_session = self.stage(second, session_id='later-session', start=1, end=2,
                                  recording_started_at='2026-11-12T01:00:00Z')
        self.assertEqual(next_session['segments'][-1]['start'], 14)
        self.assertEqual(next_session['segments'][-1]['spoken_at'], '2026-11-12T01:00:01.000Z')

    def test_first_recognition_clock_takes_priority_after_initial_silence(self):
        clocks = {'recording_started_at': '2026-11-12T09:00:00+09:00',
                  'recognized_at': '2026-11-12T09:01:04.567+09:00'}
        first = self.stage(**clocks, start=0.5)
        segment = first['segments'][-1]
        self.assertEqual(segment['start'], 7.5)
        self.assertEqual(segment['spoken_at'], '2026-11-12T00:01:04.567Z')
        self.assertEqual(segment['time_source'], 'recognition')
        self.assertIsNone(self.stage(first, **clocks, start=0.5))
        self.assertIsNone(self.stage(first, start=0.5,
                                     recording_started_at='2026-11-12T00:00:00.000Z',
                                     recognized_at='2026-11-12T00:01:04.567Z'))
        before = copy.deepcopy(first)
        with self.assertRaises(native.NativeTranscriptConflict):
            self.stage(first, start=0.5, recording_started_at=clocks['recording_started_at'],
                       recognized_at='2026-11-12T00:01:05.567Z')
        self.assertEqual(first, before)
        without_origin = self.stage(recognized_at=clocks['recognized_at'])
        self.assertEqual(without_origin['segments'][-1]['spoken_at'], segment['spoken_at'])
        self.assertEqual(without_origin['segments'][-1]['time_source'], 'recognition')

    def test_recording_origin_conflicts_reject_replay_and_new_final_without_mutation(self):
        first = self.stage(recording_started_at='2026-11-12T00:00:00Z')
        before = copy.deepcopy(first)
        for sequence in (1, 2):
            with self.subTest(sequence=sequence), self.assertRaises(native.NativeTranscriptConflict):
                self.stage(first, sequence=sequence, recording_started_at='2026-11-12T00:00:01Z')
        self.assertEqual(first, before)

    def test_optional_clocks_require_timezone_and_normalize_to_utc_milliseconds(self):
        for field in ('recording_started_at', 'recognized_at'):
            with self.subTest(field=field):
                normalized = native.validate_final(final(**{field: '2026-11-12T09:00:00.123456+09:00'}))
                self.assertEqual(normalized[field], '2026-11-12T00:00:00.123Z')
            for invalid in ('2026-11-12T09:00:00', '2026-11-12', 'not-a-clock',
                            '2026-02-30T09:00:00Z', '2026-11-12T09:00:00+25:00', 123, True, {}):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    native.validate_final(final(**{field: invalid}))

    def test_legacy_finals_and_reloads_never_gain_an_invented_clock(self):
        for missing in (None, ''):
            state = self.stage(recording_started_at=missing, recognized_at=missing)
            reloaded = json.loads(json.dumps(state))
            self.assertIsNone(self.stage(reloaded, recording_started_at=missing, recognized_at=missing))
            self.assertTrue(all('spoken_at' not in segment for segment in reloaded['segments']))
            self.assertTrue(all('time_source' not in segment for segment in reloaded['segments']))
            later = self.stage(reloaded, sequence=2, recording_started_at='2026-11-12T00:00:00Z')
            self.assertTrue(all('spoken_at' not in segment for segment in later['segments'][:-1]))
            self.assertEqual(later['segments'][-1]['spoken_at'], '2026-11-12T00:00:00.000Z')

    def test_reset_and_session_reuse_cannot_append_an_old_final(self):
        first = self.stage()
        first['conversation_id'] = 'new-conversation'
        first['segments'] = []
        with self.assertRaises(native.NativeTranscriptConflict):
            self.stage(first)
        with self.assertRaises(native.NativeTranscriptConflict):
            self.stage(first, conversation_id='new-conversation', sequence=2)

    def test_demo_rejects_finals_even_if_they_are_duplicate(self):
        for key, value in (('demo', True), ('_demo_backup', {'segments': []})):
            state = self.stage()
            state[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.stage(state)

    def test_numeric_text_and_identifier_validation(self):
        invalid = [
            {'text': ''}, {'text': ' \n\t'}, {'text': '가' * 4001}, {'text': 123},
            {'sequence': -1}, {'sequence': 1001}, {'sequence': True}, {'sequence': 1.0},
            {'start': -0.1}, {'end': 185.01}, {'start': True}, {'end': '3'},
            {'start': float('nan')}, {'end': float('inf')}, {'end': 10**400},
            {'start': 4, 'end': 3}, {'start': None},
            {'source': 'browser'}, {'session_id': ''}, {'session_id': 's' * 161},
            {'session_id': 'bad\nidentifier'}, {'conversation_id': ''},
            {'conversation_id': None}, {'conversation_id': ' trailing '},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                native.validate_final(final(**changes))
        accepted = native.validate_final(final(sequence=1000, text='가' * 4000, start=180, end=185))
        self.assertEqual(accepted['end'], 185)
        self.assertEqual(native.validate_final(final(sequence=0))['sequence'], 0)

    def test_receipts_are_bounded_and_include_every_permitted_sequence(self):
        state = copy.deepcopy(self.state)
        for sequence in range(1001):
            state = self.stage(state, sequence=sequence)
        self.assertEqual(len(state['_speech_streams']['native-session']['receipts']), 1001)
        for index in range(64):
            state = self.stage(state, session_id='session-' + str(index))
        self.assertEqual(len(state['_speech_streams']), 64)
        self.assertNotIn('native-session', state['_speech_streams'])
        self.assertIn('session-63', state['_speech_streams'])


class NativeTranscriptHTTPTests(unittest.TestCase):
    # Reuse temporary files and a loopback server without inheriting its tests.
    connection = server_test_helpers.ServerStateTests.connection
    request = server_test_helpers.ServerStateTests.request

    def setUp(self):
        server_test_helpers.ServerStateTests.setUp(self)

    def tearDown(self):
        server_test_helpers.ServerStateTests.tearDown(self)

    def send_final(self, **changes):
        return self.request('POST', '/api/speech/final', final(**changes))

    def test_commit_preserves_other_state_clears_stale_recommendations_and_hides_receipts(self):
        with server.LOCK:
            candidate = copy.deepcopy(server.STATE)
            candidate.update(recommendations=[{'id': 'old'}], recommendation_summary={'old': True},
                             request_profile={'old': True}, coverage='old', lookup_source='old',
                             trip={'id': 'trip'}, saved_places=[{'id': 'saved'}],
                             _audio_streams={'old-audio': {'base': 0, 'receipts': {}}})
            server.save_state(candidate)
        with (patch.object(server.ENGINE, 'transcribe', side_effect=AssertionError('No audio transcription')),
              patch.object(server, 'recommend', side_effect=AssertionError('No place lookup'))):
            status, result = self.send_final(seat='rear_left')
        self.assertEqual(status, 200)
        self.assertTrue(result['committed'])
        self.assertNotIn('job_id', result)
        state = result['state']
        self.assertEqual(state['segments'][-1]['text'], final()['text'])
        self.assertEqual(state['segments'][-1]['seat'], 'unknown')
        self.assertEqual(state['segments'][-1]['start'], 2)
        self.assertEqual(state['trip'], {'id': 'trip'})
        self.assertEqual(state['saved_places'], [{'id': 'saved'}])
        self.assertEqual(state['region'], self.initial['region'])
        self.assertEqual(state['recommendations'], [])
        self.assertEqual(state['recommendation_summary'], {})
        self.assertEqual(state['request_profile'], {})
        self.assertEqual(state['coverage'], '')
        self.assertNotIn('_speech_streams', state)
        self.assertNotIn('_audio_streams', state)
        disk = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
        self.assertEqual(disk['segments'], state['segments'])
        self.assertIn('1', disk['_speech_streams']['native-session']['receipts'])
        self.assertIn('old-audio', disk['_audio_streams'])

    def test_retry_after_restart_preserves_revision_and_new_recommendations(self):
        self.assertEqual(self.send_final()[0], 200)
        with server.LOCK:
            candidate = copy.deepcopy(server.STATE)
            candidate.update(recommendations=[{'id': 'fresh'}], recommendation_summary={'fresh': True})
            server.save_state(candidate)
            defaults = copy.deepcopy(server.STATE)
            defaults['_speech_streams'] = {}
            loaded = server.load_state(server.STATE_FILE, defaults)
            server.STATE.clear()
            server.STATE.update(loaded)
            before = copy.deepcopy(server.STATE)
        status, result = self.send_final()
        self.assertEqual(status, 200)
        self.assertTrue(result['committed'])
        self.assertEqual(server.STATE, before)
        self.assertEqual(result['state']['recommendations'], [{'id': 'fresh'}])
        for changes in ({'text': '바꾼 발언'}, {'start': 1}, {'end': 4}):
            self.assertEqual(self.send_final(**changes)[0], 409)
        self.assertEqual(server.STATE, before)

    def test_recognition_clock_survives_disk_reload_retry_and_transcript_edit(self):
        clocks = {'recording_started_at': '2026-11-12T09:00:00+09:00',
                  'recognized_at': '2026-11-12T09:01:00.250+09:00'}
        status, first = self.send_final(**clocks)
        self.assertEqual(status, 200)
        recorded = first['state']['segments'][-1]
        self.assertEqual(recorded['spoken_at'], '2026-11-12T00:01:00.250Z')
        self.assertEqual(recorded['time_source'], 'recognition')
        self.assertNotIn('spoken_at', first['state']['segments'][0])
        persisted = json.loads(server.STATE_FILE.read_text(encoding='utf-8'))
        self.assertEqual(persisted['segments'][-1], recorded)
        self.assertEqual(persisted['_speech_streams']['native-session']['recording_started_at'],
                         '2026-11-12T00:00:00.000Z')
        with server.LOCK:
            defaults = copy.deepcopy(server.STATE)
            defaults.update(segments=[], _speech_streams={})
            loaded = server.load_state(server.STATE_FILE, defaults)
            server.STATE.clear()
            server.STATE.update(loaded)
            before = copy.deepcopy(server.STATE)
        self.assertEqual(self.send_final(**clocks)[0], 200)
        self.assertEqual(server.STATE, before)
        status, edited = self.request('PATCH', '/api/segments/' + recorded['id'], {'text': '수정한 발언'})
        self.assertEqual(status, 200)
        self.assertEqual(edited['segments'][-1]['spoken_at'], recorded['spoken_at'])
        self.assertEqual(edited['segments'][-1]['time_source'], 'recognition')
        self.assertNotIn('spoken_at', edited['segments'][0])

    def test_clock_validation_and_conflicts_do_not_mutate_http_state(self):
        before = copy.deepcopy(server.STATE)
        for field in ('recording_started_at', 'recognized_at'):
            self.assertEqual(self.send_final(**{field: '2026-11-12T09:00:00'})[0], 400)
        self.assertEqual(server.STATE, before)
        self.assertEqual(self.send_final(recording_started_at='2026-11-12T00:00:00Z')[0], 200)
        before = copy.deepcopy(server.STATE)
        for sequence in (1, 2):
            self.assertEqual(self.send_final(sequence=sequence,
                                             recording_started_at='2026-11-12T00:01:00Z')[0], 409)
        self.assertEqual(server.STATE, before)

    def test_atomic_write_failure_leaves_neither_segment_nor_receipt(self):
        before = copy.deepcopy(server.STATE)
        disk_before = server.STATE_FILE.read_bytes()
        with patch('server.os.replace', side_effect=OSError('fixture write failure')):
            self.assertEqual(self.send_final()[0], 500)
        self.assertEqual(server.STATE, before)
        self.assertEqual(server.STATE_FILE.read_bytes(), disk_before)
        self.assertFalse(server.STATE_FILE.with_suffix('.tmp').exists())
        self.assertEqual(self.send_final()[0], 200)
        self.assertEqual(len(server.STATE['segments']), 2)

    def test_active_job_allows_acknowledgement_but_cannot_append_new_final(self):
        self.assertEqual(self.send_final()[0], 200)
        with server.LOCK:
            server.ACTIVE_JOB = 'busy'
            server.JOBS['busy'] = {'id': 'busy', 'status': 'running'}
            before = copy.deepcopy(server.STATE)
        self.assertEqual(self.send_final()[0], 200)
        self.assertEqual(self.send_final(sequence=2, text='새 발언')[0], 400)
        self.assertEqual(server.STATE, before)

    def test_reset_invalidates_late_duplicate_and_reused_session(self):
        self.assertEqual(self.send_final()[0], 200)
        status, reset = self.request('POST', '/api/reset')
        self.assertEqual(status, 200)
        self.assertEqual(self.send_final()[0], 409)
        self.assertEqual(self.send_final(conversation_id=reset['conversation_id'], sequence=2)[0], 409)
        self.assertEqual(server.STATE['segments'], [])
        self.assertEqual(self.send_final(conversation_id=reset['conversation_id'], session_id='new-session')[0], 200)
        self.assertEqual(server.STATE['segments'][0]['start'], 0)

    def test_demo_and_invalid_payload_do_not_mutate_state(self):
        before = copy.deepcopy(server.STATE)
        for changes in ({'text': ''}, {'sequence': True}, {'end': float('nan')},
                        {'end': 186}, {'source': 'browser'}, {'conversation_id': ''}):
            with self.subTest(changes=changes):
                self.assertEqual(self.send_final(**changes)[0], 400)
        self.assertEqual(server.STATE, before)
        status, demo = self.request('POST', '/api/demo')
        self.assertEqual(status, 200)
        demo_before = copy.deepcopy(server.STATE)
        self.assertEqual(self.send_final(conversation_id=demo['conversation_id'])[0], 400)
        self.assertEqual(server.STATE, demo_before)

    def test_concurrent_duplicates_commit_once(self):
        responses = []
        threads = [threading.Thread(target=lambda: responses.append(self.send_final())) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual([status for status, _ in responses], [200] * 4)
        self.assertEqual(len(server.STATE['segments']), 2)
        self.assertEqual(server.STATE['state_revision'], 2)

    def test_slow_upload_cannot_append_after_reset(self):
        body_reading = threading.Event()
        original = server.Handler.json_body

        def read_body(handler):
            if handler.path == '/api/speech/final':
                body_reading.set()
            return original(handler)

        connection = self.connection()
        payload = json.dumps(final()).encode()
        try:
            with patch.object(server.Handler, 'json_body', read_body):
                connection.putrequest('POST', '/api/speech/final')
                connection.putheader('Content-Length', str(len(payload)))
                connection.endheaders()
                self.assertTrue(body_reading.wait(1))
                self.assertEqual(self.request('POST', '/api/reset')[0], 200)
                connection.send(payload)
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                response.read()
                self.assertEqual(server.STATE['segments'], [])
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
