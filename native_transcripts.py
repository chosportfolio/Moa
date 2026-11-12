"""Validate and stage official on-device speech finals without speech inference.

The caller must hold its state lock and atomically persist the returned candidate.
Receipts live with the transcript, so retries remain idempotent after a restart.
"""
import copy
import hashlib
import json
import math
import uuid

from spoken_times import normalize_recording_started_at, at_offset, session_recording_origin


MAX_SESSIONS = 64
MAX_SEQUENCE = 1000
MAX_SECONDS = 185  # Three-minute capture plus the recognizer's final callback.
SOURCE = 'pleos_on_device'


class NativeTranscriptConflict(ValueError):
    """A final belongs to another conversation or conflicts with a saved final."""


def validate_final(payload):
    if not isinstance(payload, dict):
        raise ValueError('음성 인식 결과 형식이 올바르지 않아요.')
    event = {}
    for key in ('conversation_id', 'session_id'):
        value = payload.get(key)
        if (not isinstance(value, str) or not value or len(value) > 160
                or value != value.strip() or any(ord(char) < 32 for char in value)):
            raise ValueError('대화와 음성 인식 세션 정보를 확인해 주세요.')
        event[key] = value
    sequence = payload.get('sequence')
    if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
        raise ValueError('음성 인식 발언 번호가 올바르지 않아요.')
    text = payload.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('음성 인식 내용은 비어 있지 않은 4,000자 이내의 글이어야 해요.')
    for key in ('start', 'end'):
        value = payload.get(key)
        # Check bounds before converting very large JSON integers to float.
        if (type(value) not in (int, float) or not 0 <= value <= MAX_SECONDS
                or not math.isfinite(value)):
            raise ValueError('음성 인식 발언 시간이 올바르지 않아요.')
        event[key] = float(value)
    if event['end'] < event['start']:
        raise ValueError('발언 종료 시간이 시작 시간보다 빨라요.')
    if payload.get('source') != SOURCE:
        raise ValueError('지원하지 않는 음성 인식 입력이에요.')
    # Keep the recognizer's text byte-for-byte at the string level. Do not infer
    # seat identity, correct words, or trim the utterance after recognition.
    event.update(sequence=sequence, text=text, source=SOURCE)
    origin = normalize_recording_started_at(payload.get('recording_started_at'))
    if origin is not None:
        event['recording_started_at'] = origin
    try:
        recognized_at = normalize_recording_started_at(payload.get('recognized_at'))
    except ValueError:
        raise ValueError('발언 인식 시각 정보가 올바르지 않아요.') from None
    if recognized_at is not None:
        event['recognized_at'] = recognized_at
    return event


def stage_final(state, event):
    """Return a new state, or None for an exact, already committed retry.

    ``event`` must come from validate_final. Session timestamps are relative to
    capture start; the first commit fixes the session's offset permanently.
    """
    if event['conversation_id'] != state.get('conversation_id'):
        raise NativeTranscriptConflict('이전 대화에서 인식한 발언이에요. 현재 대화에는 추가하지 않았어요.')
    if state.get('demo') or state.get('_demo_backup'):
        raise ValueError('샘플 종료를 누른 뒤 실제 대화를 시작해 주세요.')
    digest = hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    sessions = state.get('_speech_streams', {})
    previous = sessions.get(event['session_id'])
    try:
        recording_origin = session_recording_origin(previous, event.get('recording_started_at'))
    except ValueError as error:
        raise NativeTranscriptConflict(str(error)) from None
    if previous is not None:
        if previous.get('conversation_id') != event['conversation_id']:
            raise NativeTranscriptConflict('이미 마친 대화의 음성 인식 세션이에요. 새 대화를 시작해 주세요.')
        receipt = previous.get('receipts', {}).get(str(event['sequence']))
        if receipt is not None:
            if receipt != digest:
                raise NativeTranscriptConflict('같은 발언 번호에 다른 인식 결과가 전달됐어요. 대화록을 확인해 주세요.')
            return None

    candidate = copy.deepcopy(state)
    sessions = candidate.setdefault('_speech_streams', {})
    if previous is None:
        ends = (segment.get('end', 0) for segment in candidate.get('segments', []))
        base = max((end for end in ends if type(end) in (int, float)
                    and math.isfinite(end) and end >= 0), default=0)
        sessions[event['session_id']] = {'conversation_id': event['conversation_id'],
                                        'base': base, 'receipts': {}}
    session = sessions[event['session_id']]
    if recording_origin is not None:
        session['recording_started_at'] = recording_origin
    # Sequence bounds give each session at most 1,001 durable receipts.
    session['receipts'][str(event['sequence'])] = digest
    while len(sessions) > MAX_SESSIONS:
        sessions.pop(next(iter(sessions)))
    segment = {
        'id': uuid.uuid4().hex, 'seat': 'unknown', 'text': event['text'],
        'start': session['base'] + event['start'], 'end': session['base'] + event['end'],
        'confidence': None, 'source': SOURCE,
        'session_id': event['session_id'], 'sequence': event['sequence'],
    }
    spoken_at = event.get('recognized_at') or at_offset(recording_origin, event['start'])
    if spoken_at is not None:
        segment['spoken_at'] = spoken_at
        segment['time_source'] = 'recognition' if event.get('recognized_at') else 'capture'
    candidate.setdefault('segments', []).append(segment)
    return candidate
