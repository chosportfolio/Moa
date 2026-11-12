"""Pure, local journey transitions. Callers persist the returned state atomically.

Navigation/arrival events come from the native client; this module never infers
arrival from a timer, a map link, or coordinates. Source is recorded as reported.
"""
import copy
from datetime import datetime, timezone
import math
import re
import uuid


ARRIVAL_SOURCES = frozenset({'pleos_arrival', 'manual_confirmation'})
CANCELLATION_SOURCES = frozenset({'pleos_cancelled', 'user_cancelled'})


class JourneyConflict(ValueError):
    """A stale client or out-of-order event must not alter the current trip."""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _current(state, trip_id):
    if not isinstance(trip_id, str) or not trip_id or len(trip_id) > 100:
        raise ValueError('이동 정보를 확인해 주세요.')
    trip = state.get('trip') or {}
    if trip.get('id') != trip_id:
        raise JourneyConflict('현재 선택한 이동과 다른 요청이에요. 화면을 새로 확인해 주세요.')
    return trip


def select_place(state, place_id):
    if not isinstance(place_id, str) or not place_id or len(place_id) > 200:
        raise ValueError('추천 장소를 선택해 주세요.')
    current = state.get('trip') or {}
    if current.get('status') in ('selected', 'navigating', 'arrived'):
        if current.get('place', {}).get('id') == place_id:
            return copy.deepcopy(state)
        if current['status'] in ('navigating', 'arrived'):
            raise JourneyConflict('진행 중인 이동을 마친 뒤 다른 장소를 선택해 주세요.')
    place = next((p for p in state.get('recommendations', [])
                  if isinstance(p, dict) and p.get('id') == place_id), None)
    if place is None:
        raise ValueError('현재 추천 목록에 없는 장소예요. 다시 추천해 주세요.')
    if not isinstance(place.get('name'), str) or not place['name'].strip():
        raise ValueError('장소 이름을 확인할 수 없어요. 다시 추천해 주세요.')
    coordinates = {}
    for key, limit in (('latitude', 90), ('longitude', 180)):
        value = place.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not -limit <= value <= limit:
            raise ValueError('길안내에 필요한 장소 좌표가 없어요. 다른 장소를 선택해 주세요.')
        coordinates[key] = float(value)
    selected = copy.deepcopy(place)
    selected.update(coordinates)
    summary = state.get('recommendation_summary') or {}
    if not isinstance(summary, dict):
        summary = {}
    updated = copy.deepcopy(state)
    updated['trip'] = {
        'id': uuid.uuid4().hex, 'status': 'selected', 'place': selected,
        'summary': copy.deepcopy(summary), 'region': state.get('region', ''),
        'demo': bool(state.get('demo', False)),
        'selected_at': timestamp(), 'started_at': None, 'arrived_at': None,
        'provider': None, 'arrival_source': None,
    }
    updated.setdefault('saved_places', [])
    return updated


def start_trip(state, trip_id, provider):
    if not isinstance(provider, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', provider):
        raise ValueError('길안내 제공자를 확인해 주세요.')
    trip = _current(state, trip_id)
    updated = copy.deepcopy(state)
    if trip.get('status') in ('navigating', 'arrived', 'saved', 'dismissed', 'cancelled'):
        return updated  # A delayed native/UI duplicate cannot restart a trip.
    if trip.get('status') != 'selected':
        raise JourneyConflict('장소 선택 후 길안내를 시작해 주세요.')
    updated['trip'].update(status='navigating', started_at=timestamp(), provider=provider)
    return updated


def arrive_trip(state, trip_id, source):
    if not isinstance(source, str) or source not in ARRIVAL_SOURCES:
        raise ValueError('도착 정보는 순정 내비 도착 이벤트나 직접 도착 확인으로만 받을 수 있어요.')
    trip = _current(state, trip_id)
    updated = copy.deepcopy(state)
    if trip.get('status') in ('arrived', 'saved', 'dismissed', 'cancelled'):
        return updated  # Preserve the first arrival and its actual source.
    manual_selected = source == 'manual_confirmation' and trip.get('status') == 'selected'
    if trip.get('status') != 'navigating' and not manual_selected:
        raise JourneyConflict('길안내가 시작된 이동만 도착 확인할 수 있어요.')
    updated['trip'].update(status='arrived', arrived_at=timestamp(), arrival_source=source)
    return updated


def cancel_trip(state, trip_id, source):
    if not isinstance(source, str) or source not in CANCELLATION_SOURCES:
        raise ValueError('이동 취소 정보가 올바르지 않아요.')
    trip = _current(state, trip_id)
    updated = copy.deepcopy(state)
    if trip.get('status') in ('arrived', 'saved', 'dismissed', 'cancelled'):
        return updated  # A late cancellation cannot discard an arrival decision.
    if trip.get('status') not in ('selected', 'navigating'):
        raise JourneyConflict('선택했거나 안내 중인 이동만 취소할 수 있어요.')
    updated['trip'] = {'id': trip_id, 'status': 'cancelled',
                       'cancelled_at': timestamp(), 'cancellation_source': source}
    return updated


def decide_trip(state, trip_id, save):
    if type(save) is not bool:
        raise ValueError('장소 저장 여부를 선택해 주세요.')
    trip = _current(state, trip_id)
    expected = 'saved' if save else 'dismissed'
    if trip.get('status') in ('saved', 'dismissed'):
        if trip['status'] != expected:
            raise JourneyConflict('이미 완료한 저장 선택이에요.')
        return copy.deepcopy(state)
    if trip.get('status') != 'arrived':
        raise JourneyConflict('목적지에 도착한 뒤 저장 여부를 선택해 주세요.')
    updated = copy.deepcopy(state)
    decided_at = timestamp()
    if not save:
        # Keep only an idempotency marker. Declining creates no visit record and
        # removes the separate copy of the conversation and recommendation.
        updated['trip'] = {'id': trip_id, 'status': 'dismissed', 'decision_at': decided_at}
        return updated
    visits = updated.setdefault('saved_places', [])
    if not any(visit.get('trip_id') == trip_id for visit in visits):
        record = {key: copy.deepcopy(trip.get(key)) for key in (
            'place', 'summary', 'region', 'demo', 'selected_at', 'started_at',
            'arrived_at', 'provider', 'arrival_source')}
        record.update(id=trip_id, trip_id=trip_id, saved_at=decided_at)
        visits.append(record)
    updated['trip'].update(status='saved', decision_at=decided_at, saved_at=decided_at)
    return updated
