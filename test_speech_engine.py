"""Meaningful regression checks; set CABIN_SPEECH_INTEGRATION=1 for model tests.

synthetic-ko-*.wav were generated locally with Microsoft Heami Desktop.
official-four-speakers-zh.wav is the unmodified public sherpa-onnx sample at
https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/0-four-speakers-zh.wav
These fixtures contain no recording of the app user. Profiles are temporary.
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from speech_engine import SAMPLE_RATE, SpeechEngine


ROOT = Path(__file__).parent
FIXTURES = ROOT / "tests" / "fixtures"


class SpeechBoundaryTests(unittest.TestCase):
    def fake_engine(self, words, duration=6, speech_ranges=None, turns=None):
        engine = SpeechEngine(ROOT / "models", ROOT / "tests" / "not-created-profiles.json")
        engine.ready = True
        engine._read_audio = Mock(return_value=np.zeros(int(SAMPLE_RATE * duration), dtype=np.float32))
        engine._speech_ranges = Mock(return_value=speech_ranges if speech_ranges is not None
                                    else [{"start": 0, "end": int(SAMPLE_RATE * duration)}])
        engine._turns = Mock(return_value=turns if turns is not None else [(0, duration, 0)])
        segment = SimpleNamespace(no_speech_prob=0.0, avg_logprob=0.0, compression_ratio=1.0,
                                  words=[SimpleNamespace(start=start, end=end, word=text)
                                         for start, end, text in words])
        engine._whisper = Mock()
        engine._whisper.transcribe.return_value = (iter([segment]), None)
        return engine

    def test_default_transcription_never_assigns_a_seat_from_legacy_profiles(self):
        engine = self.fake_engine([(0.1, 0.8, "커피 좋아요.")])
        engine._profiles = {"driver": {"embedding": [1.0], "speech_seconds": 10}}
        engine._match_speakers = Mock(return_value={0: ("driver", 0.99)})
        lines = engine.transcribe("not-opened.wav")
        engine._match_speakers.assert_not_called()
        self.assertEqual(lines[0]["seat"], "unknown")
        self.assertEqual(lines[0]["confidence"], 0.0)

    def test_short_reply_accepted_by_vad_reaches_transcription(self):
        engine = self.fake_engine([(0.05, 0.28, "싫어.")], duration=1,
                                  speech_ranges=[{"start": 640, "end": 4480}])
        lines = engine.transcribe("not-opened.wav")
        self.assertEqual([line["text"] for line in lines], ["싫어."])
        engine._whisper.transcribe.assert_called_once()

    def test_click_sized_audio_does_not_reach_transcription(self):
        engine = self.fake_engine([(0.05, 0.1, "환각")], duration=1,
                                  speech_ranges=[{"start": 800, "end": 2400}])
        self.assertEqual(engine.transcribe("not-opened.wav"), [])
        engine._whisper.transcribe.assert_not_called()

    def test_model_timestamps_stay_within_audio_and_never_shrink_a_line(self):
        engine = self.fake_engine([
            (-0.2, 0.4, " 처음"), (0.5, 2.5, " 긴말"),
            (1.5, 2.0, " 겹친말"), (2.8, 2.2, " 역전"),
            (2.5, 8.0, " 끝"), (7, 8, " 바깥"),
        ], duration=3)
        lines = engine.transcribe("not-opened.wav")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "처음 긴말 겹친말 끝")
        self.assertEqual(lines[0]["start"], 0)
        self.assertEqual(lines[0]["end"], 3)

    def test_overlapping_word_timing_does_not_move_line_end_backward(self):
        engine = self.fake_engine([(0.1, 2.5, "긴말"), (1.5, 2.0, " 겹친말")])
        lines = engine.transcribe("not-opened.wav")
        self.assertEqual(lines[0]["end"], 2.5)

    def test_overlap_is_preserved_in_output_without_guessing_a_seat(self):
        engine = self.fake_engine([(0.5, 1.0, "커피"), (1.5, 2.0, "동시 발언")],
                                  duration=3, turns=[(0, 3, 0), (1.4, 2.2, 1)])
        lines = engine.transcribe("not-opened.wav")
        self.assertEqual([line["overlap"] for line in lines], [False, True])
        self.assertIsNone(lines[1]["speaker_cluster"])
        self.assertEqual([line["seat"] for line in lines], ["unknown", "unknown"])

    def test_disabled_enrollment_keeps_anonymous_speaker_clusters(self):
        engine = SpeechEngine(ROOT / "models", ROOT / "tests" / "not-created-profiles.json")
        engine.ready = True
        engine._profiles = {"driver": {"embedding": [1.0], "speech_seconds": 10}}
        engine._read_audio = Mock(return_value=np.zeros(SAMPLE_RATE * 6, dtype=np.float32))
        engine._speech_ranges = Mock(return_value=[{"start": 0, "end": SAMPLE_RATE * 6}])
        engine._turns = Mock(return_value=[(0, 3, 0), (3, 6, 1)])
        engine._match_speakers = Mock(return_value={0: ("driver", 0.99), 1: ("passenger", 0.99)})
        segment = SimpleNamespace(
            no_speech_prob=0.0, avg_logprob=0.0, compression_ratio=1.0,
            words=[SimpleNamespace(start=0.4, end=1.0, word="커피 좋아요."),
                   SimpleNamespace(start=3.4, end=4.0, word="저는 산책이요.")],
        )
        engine._whisper = Mock()
        engine._whisper.transcribe.return_value = (iter([segment]), None)
        lines = engine.transcribe("not-opened.wav", allow_enrolled=False)
        engine._match_speakers.assert_not_called()
        self.assertEqual([line["seat"] for line in lines], ["unknown", "unknown"])
        self.assertEqual([line["speaker_cluster"] for line in lines], [0, 1])
        self.assertNotEqual(lines[0]["cluster_id"], lines[1]["cluster_id"])
        self.assertEqual([line["confidence"] for line in lines], [0.0, 0.0])
        self.assertIn("driver", engine._profiles)

    def test_words_on_turn_boundary_are_not_guessed(self):
        timeline = SpeechEngine._timeline([(0, 2, 0), (2, 4, 1)])
        self.assertEqual(SpeechEngine._speaker_at(0.2, 1.0, timeline), (0, False))
        self.assertEqual(SpeechEngine._speaker_at(2.2, 2.8, timeline), (1, False))
        self.assertEqual(SpeechEngine._speaker_at(1.9, 2.1, timeline), (None, False))

    def test_overlapping_speech_is_explicit(self):
        timeline = SpeechEngine._timeline([(0, 4, 0), (3, 5, 1)])
        self.assertEqual(SpeechEngine._speaker_at(3.2, 3.8, timeline), (None, True))

    def test_profiles_remain_responsive_during_inference(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            engine = SpeechEngine(ROOT / "models", Path(directory) / "profiles.json")
            acquired = threading.Event()
            release = threading.Event()

            def hold_inference():
                with engine._inference_lock:
                    acquired.set()
                    release.wait(3)

            worker = threading.Thread(target=hold_inference)
            worker.start()
            self.assertTrue(acquired.wait(1))
            try:
                before = time.monotonic()
                self.assertEqual(len(engine.profiles()), 4)
                self.assertLess(time.monotonic() - before, 0.15)
            finally:
                release.set()
                worker.join(1)


class SpeechVadTests(unittest.TestCase):
    """Real local VAD and synthetic speech; no Whisper load or microphone input."""

    def test_quiet_reply_is_not_erased_by_silence_padding_in_a_five_minute_file(self):
        engine = SpeechEngine(ROOT / 'models', ROOT / 'tests' / 'not-created-profiles.json')
        quiet = engine._read_audio(FIXTURES / 'synthetic-ko-utterance.wav') * 0.03
        standalone = engine._speech_ranges(quiet)
        self.assertTrue(standalone, 'The local VAD accepts this low-volume synthetic utterance')
        padded = np.zeros(300 * SAMPLE_RATE, dtype=np.float32)
        offset = 100 * SAMPLE_RATE
        padded[offset:offset + len(quiet)] = quiet
        # This is the real failure trigger: whole-file RMS is below the old gate.
        self.assertLess(float(np.sqrt(np.mean(np.square(padded, dtype=np.float64)))), 0.0005)
        ranges = engine._speech_ranges(padded)
        self.assertTrue(ranges, 'Adding silence cannot remove an otherwise recognized reply')
        self.assertTrue(all(item['start'] >= offset - SAMPLE_RATE // 10 for item in ranges))
        standalone_seconds = sum(item['end'] - item['start'] for item in standalone) / SAMPLE_RATE
        padded_seconds = sum(item['end'] - item['start'] for item in ranges) / SAMPLE_RATE
        self.assertEqual(len(ranges), len(standalone), 'Keep every synthetic utterance across the surrounding silence')
        self.assertGreaterEqual(padded_seconds, standalone_seconds * 0.9,
                                'VAD context can adjust boundaries but must retain the speech')

    def test_a_silent_five_minute_file_still_skips_vad_inference(self):
        engine = SpeechEngine(ROOT / 'models', ROOT / 'tests' / 'not-created-profiles.json')
        with patch('faster_whisper.vad.get_speech_timestamps') as detect:
            self.assertEqual(engine._speech_ranges(np.zeros(300 * SAMPLE_RATE, dtype=np.float32)), [])
            detect.assert_not_called()


@unittest.skipUnless(os.environ.get("CABIN_SPEECH_INTEGRATION") == "1", "Opt-in local model inference")
class LocalModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        cls.engine = SpeechEngine(ROOT / "models", Path(cls.temporary.name) / "profiles.json")
        cls.engine.load()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_silence_returns_no_transcript(self):
        path = Path(self.temporary.name) / "silence.wav"
        sf.write(path, np.zeros(SAMPLE_RATE * 5, dtype=np.float32), SAMPLE_RATE)
        self.assertEqual(self.engine.transcribe(path), [])

    def test_korean_transcription_works_without_voice_registration(self):
        self.assertFalse(any(item["ready"] for item in self.engine.profiles()))
        lines = self.engine.transcribe(FIXTURES / "synthetic-ko-utterance.wav")
        text = " ".join(line["text"] for line in lines)
        self.assertIn("커피", text)
        self.assertIn("매운", text)
        self.assertTrue(lines)
        self.assertTrue(all(line["seat"] == "unknown" for line in lines))
        self.assertTrue(all(0 <= line["start"] < line["end"] for line in lines))

    def test_korean_transcription_and_registered_voice(self):
        enrolled = self.engine.enroll("driver", FIXTURES / "synthetic-ko-enrollment.wav")
        self.assertTrue(enrolled["ready"])
        lines = self.engine.transcribe(FIXTURES / "synthetic-ko-utterance.wav", allow_enrolled=True)
        text = " ".join(line["text"] for line in lines)
        print("Synthetic Korean transcript:", text, flush=True)
        self.assertIn("커피", text)
        self.assertIn("매운", text)
        self.assertTrue(any(line["seat"] == "driver" for line in lines), lines)
        self.assertTrue(all(line["seat"] in {"driver", "unknown"} for line in lines))
        self.engine.delete_profile("driver")

    def test_public_multispeaker_sample_separates_voices(self):
        audio = self.engine._read_audio(FIXTURES / "official-four-speakers-zh.wav")
        turns = self.engine._turns(audio)
        speakers = {turn[2] for turn in turns}
        print("Public four-speaker sample clusters:", len(speakers), flush=True)
        self.assertEqual(len(speakers), 4)
        self.assertGreater(len(turns), 4)
        # Reference turns are published in the sherpa model documentation:
        # https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html
        timeline = self.engine._timeline(turns)
        probes = [(0.6, 1.0), (7.2, 7.7), (14.2, 14.6), (28.0, 28.5)]
        identities = [self.engine._speaker_at(a, b, timeline)[0] for a, b in probes]
        self.assertNotIn(None, identities)
        self.assertEqual(len(set(identities)), 4)
        for start, end, expected in [(23.0, 23.5, identities[0]),
                                     (48.5, 49.0, identities[2]),
                                     (53.0, 53.5, identities[0])]:
            self.assertEqual(self.engine._speaker_at(start, end, timeline), (expected, False))

    def test_registered_public_voice_matches_later_utterances(self):
        audio = self.engine._read_audio(FIXTURES / "official-four-speakers-zh.wav")
        try:
            # Real enrollment uses the two speakers' initial utterances only.
            for seat, start, end in [("driver", 0.31, 6.9), ("passenger", 7.0, 13.7)]:
                path = Path(self.temporary.name) / f"public-{seat}.wav"
                sf.write(path, audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)], SAMPLE_RATE)
                self.assertTrue(self.engine.enroll(seat, path)["ready"])
            # All probes are disjoint from enrollment. The fourth speaker was
            # never registered and must remain unknown, not take an empty seat.
            timeline = self.engine._timeline([
                (22.14, 24.83, 10), (52.53, 54.60, 10),
                (27.64, 29.47, 30), (30.00, 31.55, 30), (33.68, 37.93, 30),
            ])
            matches = self.engine._match_speakers(audio, timeline)
            print("Held-out public voice matches:", matches, flush=True)
            self.assertEqual(matches[10][0], "driver")
            self.assertEqual(matches[30][0], "unknown")
        finally:
            self.engine.delete_profile("driver")
            self.engine.delete_profile("passenger")


if __name__ == "__main__":
    unittest.main(verbosity=2)
