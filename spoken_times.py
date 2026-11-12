"""Capture-clock timestamps, independent of upload and transcription delays."""
from datetime import datetime, timedelta, timezone
import math
import re


_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$')


def normalize_recording_started_at(value):
    """Return canonical UTC ISO8601, or None for a legacy missing clock."""
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError('녹음 시작 시각 정보가 올바르지 않아요.')
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.utcoffset() is None:
            raise ValueError()
        return instant.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        raise ValueError('녹음 시작 시각 정보가 올바르지 않아요.') from None


def at_offset(recording_started_at, seconds):
    """Use the capture origin; never substitute receive time for missing data."""
    if recording_started_at is None:
        return None
    try:
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError()
        origin = datetime.fromisoformat(recording_started_at.replace('Z', '+00:00'))
        return (origin + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat(
            timespec='milliseconds').replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError):
        raise ValueError('발언 시각 정보가 올바르지 않아요.') from None


def session_recording_origin(session, supplied):
    """Keep an already persisted session clock stable across chunks/retries."""
    previous = session.get('recording_started_at') if session else None
    if previous and supplied and previous != supplied:
        raise ValueError('같은 녹음 세션에 다른 시작 시각이 전달됐어요.')
    return previous or supplied
