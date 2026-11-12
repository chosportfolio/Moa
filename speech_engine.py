"""Local Korean transcription with anonymous speaker and overlap metadata.

Legacy enrolled-speaker matching is available only through an explicit opt-in.
The cabin conversation path never infers a seat from a voice.
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


SEAT_LABELS = {
    "driver": "운전자",
    "passenger": "동승자",
    "rear1": "뒷좌석 1",
    "rear2": "뒷좌석 2",
}
SAMPLE_RATE = 16000
MAX_SECONDS = 300
MATCH_THRESHOLD = 0.60
MATCH_MARGIN = 0.08


class SpeechEngine:
    def __init__(self, model_dir: Path, profile_path: Path):
        self.model_dir = Path(model_dir)
        self.profile_path = Path(profile_path)
        self.ready = False
        self.message = "음성 모델 준비 중"
        self._inference_lock = threading.Lock()
        self._profile_lock = threading.RLock()
        self._whisper = None
        self._extractor = None
        self._diarizer = None
        self._profiles = self._read_profiles()

    def _read_profiles(self):
        if not self.profile_path.exists():
            return {}
        try:
            document = json.loads(self.profile_path.read_text(encoding="utf-8"))
            profiles = {}
            for seat, item in document.get("profiles", {}).items():
                if seat not in SEAT_LABELS or not isinstance(item, dict):
                    continue
                embedding = np.asarray(item.get("embedding", []), dtype=np.float32)
                if embedding.ndim != 1 or embedding.size == 0:
                    continue
                if not np.all(np.isfinite(embedding)) or np.linalg.norm(embedding) == 0:
                    continue
                profiles[seat] = item
            return profiles
        except (OSError, ValueError, TypeError, AttributeError):
            self.message = "저장된 목소리 정보를 읽을 수 없어 다시 등록해야 해요."
            return {}

    def _save_profiles(self, profiles):
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.profile_path.with_name(self.profile_path.name + ".tmp")
        payload = {"version": 1, "profiles": profiles}
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.profile_path)
        finally:
            temporary.unlink(missing_ok=True)
        self._profiles = profiles

    def load(self):
        with self._inference_lock:
            if self.ready:
                return
            try:
                import sherpa_onnx
                from faster_whisper import WhisperModel

                embedding_path = self.model_dir / "nemo_en_titanet_small.onnx"
                segmentation_path = self.model_dir / "segmentation.onnx"
                whisper_path = self.model_dir / "whisper-small"
                required = [embedding_path, segmentation_path, whisper_path / "model.bin"]
                if not all(path.is_file() for path in required):
                    raise RuntimeError("음성 모델이 없어요. download_models.py를 먼저 실행해 주세요.")
                self.message = "한국어 음성 인식 모델을 불러오는 중"
                self._whisper = WhisperModel(
                    str(whisper_path), device="cpu", compute_type="int8",
                    cpu_threads=4, num_workers=1, local_files_only=True,
                )
                self.message = "목소리 구분 모델을 불러오는 중"
                embedding_config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(embedding_path), num_threads=4, provider="cpu", debug=False,
                )
                if not embedding_config.validate():
                    raise RuntimeError("목소리 구분 모델 파일을 확인해 주세요.")
                self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(embedding_config)
                diarization_config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                            model=str(segmentation_path), window_shift_ratio=0.1,
                        ),
                        num_threads=4, provider="cpu", debug=False,
                    ),
                    embedding=embedding_config,
                    clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.8),
                    min_duration_on=0.25,
                    min_duration_off=0.3,
                )
                if not diarization_config.validate():
                    raise RuntimeError("대화 구간 분석 모델 파일을 확인해 주세요.")
                self._diarizer = sherpa_onnx.OfflineSpeakerDiarization(diarization_config)
                if self._diarizer.sample_rate != SAMPLE_RATE:
                    raise RuntimeError("대화 구간 모델의 샘플레이트가 16 kHz와 달라요.")
                self.ready = True
                self.message = "음성 인식 준비 완료"
            except Exception as error:
                self.ready = False
                self.message = "음성 모델을 준비하지 못했어요. " + str(error)
                raise

    def profiles(self):
        with self._profile_lock:
            return [
                {
                    "seat": seat,
                    "label": label,
                    "ready": seat in self._profiles,
                    "speech_seconds": self._profiles.get(seat, {}).get("speech_seconds", 0),
                }
                for seat, label in SEAT_LABELS.items()
            ]

    def _require_ready(self):
        if not self.ready:
            raise RuntimeError(self.message)

    def _read_audio(self, audio_path):
        from faster_whisper.audio import decode_audio

        try:
            audio = np.ascontiguousarray(decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE))
        except Exception as error:
            raise ValueError("녹음 파일을 읽을 수 없어요. WAV, MP3 또는 WebM 파일을 확인해 주세요.") from error
        if audio.size == 0 or audio.ndim != 1 or not np.all(np.isfinite(audio)):
            raise ValueError("녹음 파일에 유효한 음성이 없어요.")
        if len(audio) > MAX_SECONDS * SAMPLE_RATE:
            raise ValueError("한 번에 5분 이내의 녹음을 사용해 주세요.")
        return audio.astype(np.float32, copy=False)

    def _speech_ranges(self, audio):
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        # A short, quiet reply must not disappear just because an uploaded file
        # contains minutes of silence. Measure local energy before running VAD.
        frame_samples = SAMPLE_RATE // 10
        if not any(np.mean(np.square(audio[offset:offset + frame_samples], dtype=np.float64)) >= 0.0005 ** 2
                   for offset in range(0, len(audio), frame_samples)):
            return []
        return get_speech_timestamps(
            audio,
            VadOptions(min_speech_duration_ms=200, min_silence_duration_ms=250, speech_pad_ms=0),
            sampling_rate=SAMPLE_RATE,
        )

    def _embedding(self, audio):
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=np.ascontiguousarray(audio))
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            raise ValueError("목소리를 구분하기에 발언이 너무 짧아요.")
        embedding = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(embedding))
        if norm <= 0 or not math.isfinite(norm):
            raise ValueError("목소리 특징을 읽지 못했어요. 조용한 곳에서 다시 녹음해 주세요.")
        return embedding / norm

    def _turns(self, audio):
        duration = len(audio) / SAMPLE_RATE
        result = self._diarizer.process(audio).sort_by_start_time()
        return [
            (max(0.0, float(turn.start)), min(duration, float(turn.end)), int(turn.speaker))
            for turn in result
            if math.isfinite(turn.start) and math.isfinite(turn.end)
            and turn.end > turn.start and turn.start < duration
        ]

    @staticmethod
    def _timeline(turns):
        """Produce non-overlapping intervals that retain simultaneous speakers."""
        boundaries = sorted({boundary for start, end, _ in turns for boundary in (start, end)})
        timeline = []
        for start, end in zip(boundaries, boundaries[1:]):
            midpoint = (start + end) / 2
            speakers = frozenset(speaker for a, b, speaker in turns if a <= midpoint < b)
            if not speakers:
                continue
            if timeline and timeline[-1][2] == speakers and abs(timeline[-1][1] - start) < 0.001:
                timeline[-1] = (timeline[-1][0], end, speakers)
            else:
                timeline.append((start, end, speakers))
        return timeline

    def enroll(self, seat, audio_path):
        if seat not in SEAT_LABELS:
            raise ValueError("등록할 좌석을 선택해 주세요.")
        with self._inference_lock:
            self._require_ready()
            audio = self._read_audio(audio_path)
            ranges = self._speech_ranges(audio)
            voiced_seconds = sum(item["end"] - item["start"] for item in ranges) / SAMPLE_RATE
            if not 5.0 <= voiced_seconds <= 20.5:
                raise ValueError(
                    f"실제 말한 부분이 {voiced_seconds:.1f}초예요. 한 사람만 5~20초 동안 이야기해 주세요."
                )
            timeline = self._timeline(self._turns(audio))
            speaker_lengths = {}
            for start, end, speakers in timeline:
                for speaker in speakers:
                    speaker_lengths[speaker] = speaker_lengths.get(speaker, 0) + end - start
            substantial = [length for length in speaker_lengths.values() if length >= 1.0]
            if len(substantial) > 1:
                raise ValueError("등록 음성에서 여러 목소리가 감지됐어요. 해당 좌석의 한 사람만 다시 말해 주세요.")
            samples = np.concatenate([audio[item["start"]:item["end"]] for item in ranges])
            chunks = [samples[offset:offset + 8 * SAMPLE_RATE] for offset in range(0, len(samples), 8 * SAMPLE_RATE)]
            embeddings = [self._embedding(chunk) for chunk in chunks if len(chunk) >= 3 * SAMPLE_RATE]
            embedding = np.mean(embeddings, axis=0)
            embedding /= np.linalg.norm(embedding)
            profile = {
                "label": SEAT_LABELS[seat],
                "embedding": embedding.tolist(),
                "speech_seconds": round(voiced_seconds, 2),
                "enrolled_at": datetime.now(timezone.utc).isoformat(),
            }
            with self._profile_lock:
                updated = dict(self._profiles)
                updated[seat] = profile
                self._save_profiles(updated)
            return {"seat": seat, "label": SEAT_LABELS[seat], "ready": True, "speech_seconds": profile["speech_seconds"]}

    def delete_profile(self, seat):
        if seat not in SEAT_LABELS:
            raise ValueError("삭제할 좌석을 확인해 주세요.")
        with self._profile_lock:
            updated = dict(self._profiles)
            updated.pop(seat, None)
            self._save_profiles(updated)

    def _match_speakers(self, audio, timeline):
        clusters = sorted({speaker for _, _, speakers in timeline for speaker in speakers})
        matched = {speaker: ("unknown", 0.0) for speaker in clusters}
        with self._profile_lock:
            profiles = dict(self._profiles)
        if not profiles:
            return matched
        for speaker in clusters:
            clean = [(start, end) for start, end, speakers in timeline if speakers == frozenset((speaker,))]
            # Joining clean spans preserves short turns without mixing other speakers.
            clean.sort(key=lambda interval: interval[1] - interval[0], reverse=True)
            chunks = []
            collected = 0
            for start, end in clean:
                chunk = audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
                room = 20 * SAMPLE_RATE - collected
                if room <= 0:
                    break
                chunks.append(chunk[:room])
                collected += len(chunks[-1])
            if collected < 3 * SAMPLE_RATE:
                continue
            embedding = self._embedding(np.concatenate(chunks))
            scores = []
            for seat, profile in profiles.items():
                enrolled = np.asarray(profile["embedding"], dtype=np.float32)
                if enrolled.shape != embedding.shape:
                    continue
                score = float(np.dot(embedding, enrolled) / np.linalg.norm(enrolled))
                scores.append((score, seat))
            scores.sort(reverse=True)
            if not scores:
                continue
            best, seat = scores[0]
            second = scores[1][0] if len(scores) > 1 else -1.0
            if best >= MATCH_THRESHOLD and best - second >= MATCH_MARGIN:
                matched[speaker] = (seat, round(max(0.0, min(1.0, best)), 4))
        return matched

    @staticmethod
    def _speaker_at(start, end, timeline):
        end = max(end, start + 0.01)
        duration = end - start
        covered = {}
        simultaneous = 0.0
        for begin, finish, speakers in timeline:
            overlap = max(0.0, min(end, finish) - max(start, begin))
            if not overlap:
                continue
            if len(speakers) > 1:
                simultaneous += overlap
            for speaker in speakers:
                covered[speaker] = covered.get(speaker, 0.0) + overlap
        if simultaneous >= min(0.06, duration * 0.2):
            return None, True
        scores = sorted(covered.items(), key=lambda item: item[1], reverse=True)
        if not scores or scores[0][1] / duration < 0.5:
            return None, False
        if len(scores) > 1 and scores[1][1] / duration > 0.2:
            return None, False
        return scores[0][0], False

    def transcribe(self, audio_path, allow_enrolled=False):
        with self._inference_lock:
            self._require_ready()
            audio = self._read_audio(audio_path)
            duration = len(audio) / SAMPLE_RATE
            speech_ranges = self._speech_ranges(audio)
            voiced_seconds = sum(item["end"] - item["start"] for item in speech_ranges) / SAMPLE_RATE
            # Match the VAD's 200 ms speech floor so brief replies are not
            # discarded after the detector has already accepted them.
            if voiced_seconds < 0.2:
                return []
            timeline = self._timeline(self._turns(audio))
            identities = self._match_speakers(audio, timeline) if allow_enrolled else {}
            segments, _ = self._whisper.transcribe(
                audio, language="ko", beam_size=5, temperature=0,
                word_timestamps=True, condition_on_previous_text=False,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
                no_speech_threshold=0.6, log_prob_threshold=-1.0,
                hallucination_silence_threshold=1.0,
            )
            batch_id = uuid.uuid4().hex[:12]
            transcript = []
            for segment in segments:
                if segment.no_speech_prob > 0.65 and segment.avg_logprob < -0.8:
                    continue
                if segment.compression_ratio > 3.0 and segment.avg_logprob < -1.0:
                    continue
                words = segment.words
                if not words:
                    words = [segment]
                for word in words:
                    text = getattr(word, "word", getattr(word, "text", ""))
                    if not text.strip() or not math.isfinite(word.start) or not math.isfinite(word.end):
                        continue
                    start = max(0.0, min(duration, float(word.start)))
                    end = max(0.0, min(duration, float(word.end)))
                    if end <= start:
                        continue
                    if not any(item["end"] / SAMPLE_RATE > start and item["start"] / SAMPLE_RATE < end for item in speech_ranges):
                        continue
                    speaker, overlap = self._speaker_at(start, end, timeline)
                    seat, confidence = identities.get(speaker, ("unknown", 0.0))
                    cluster_id = f"{batch_id}:{speaker}" if speaker is not None else None
                    previous = transcript[-1] if transcript else None
                    if (previous and previous["seat"] == seat and previous["cluster_id"] == cluster_id
                            and previous["overlap"] == overlap and start - previous["end"] < 1.5
                            and end - previous["start"] <= 25):
                        previous["text"] += text
                        previous["end"] = max(previous["end"], round(end, 3))
                    else:
                        transcript.append({
                            "id": str(uuid.uuid4()), "seat": seat, "text": text,
                            "start": round(start, 3), "end": round(end, 3),
                            "confidence": confidence, "cluster_id": cluster_id,
                            "speaker_cluster": speaker, "overlap": overlap,
                        })
            for line in transcript:
                line["text"] = line["text"].strip()
            return transcript
