"""Small, on-demand road-time lookups for recommendation candidates.

Public service policy: https://routing.openstreetmap.de/about.html
At most one request per second, no bulk use, identifiable User-Agent, and UI
attribution plus an OpenStreetMap edit link. The caller must display attribution
and the returned qualification: these are road estimates without live traffic.

Legacy display enrichment makes one request (up to 12 destinations). Candidate
screening makes at most four table requests of 40 distinct destinations each,
plus at most four repairs that omit specifically identified unsnappable points.
Repeated routes are cached in memory before final recommendation selection.
No straight-line/speed fallback is used. Coordinates are only cached in memory.
For deployments with multiple server processes or substantial traffic, replace
the public endpoint with an operated routing service and shared rate limiting.
"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import math
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


OSRM_TABLE_URL = 'https://routing.openstreetmap.de/routed-car/table/v1/driving/'
USER_AGENT = 'CabinConversation/1.0 (Pleos prototype; on-demand route-time lookup)'
MAX_DESTINATIONS = 12
MAX_CANDIDATE_DESTINATIONS = 160
CANDIDATE_BATCH_SIZE = 40
MAX_CANDIDATE_REPAIRS = 4
REQUEST_TIMEOUT = 12
MIN_REQUEST_INTERVAL = 1.1
CACHE_TTL = 600
FAILURE_CACHE_TTL = 30
CACHE_MAX_ENTRIES = 128
ROUTE_CACHE_MAX_ENTRIES = 2048
MAX_RESPONSE_BYTES = 131072
LOCK_WAIT_SECONDS = REQUEST_TIMEOUT + MIN_REQUEST_INTERVAL + 1
SOURCE = 'OSRM · OpenStreetMap'
SOURCE_URL = 'https://routing.openstreetmap.de/about.html'
ATTRIBUTION = '© OpenStreetMap contributors'
ATTRIBUTION_URL = 'https://www.openstreetmap.org/copyright'
FIX_MAP_URL = 'https://www.openstreetmap.org/edit'

_CACHE = OrderedDict()
_ROUTE_CACHE = OrderedDict()
_LOCK = threading.Lock()
_LAST_REQUEST = None
_COOLDOWN_UNTIL = 0.0


@dataclass(frozen=True)
class _Estimate:
    seconds: float | None = None
    distance_m: float | None = None
    reason: str = 'unavailable'


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _coordinates(value):
    if not isinstance(value, dict):
        return None
    lat = _number(value.get('lat', value.get('latitude')))
    lon = _number(value.get('lon', value.get('longitude')))
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return round(lat, 6), round(lon, 6)


def _request_url(origin, destinations):
    coordinates = ';'.join(f'{lon:.6f},{lat:.6f}' for lat, lon in (origin, *destinations))
    options = {
        'sources': '0',
        'destinations': ';'.join(str(i) for i in range(1, len(destinations) + 1)),
        'annotations': 'duration,distance',
        'generate_hints': 'false',
        'skip_waypoints': 'true',
        # Do not silently snap an island/place pin onto a road far away.
        'radiuses': ';'.join(['1000'] * (len(destinations) + 1)),
    }
    return OSRM_TABLE_URL + coordinates + '?' + urlencode(options)


def _retry_delay(headers):
    raw = headers.get('Retry-After') if headers is not None else None
    seconds = _number(raw)
    if seconds is None and raw:
        try:
            seconds = parsedate_to_datetime(raw).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            pass
    return max(60.0, seconds or 0)


def _fetch_table(origin, destinations):
    """Called under the process lock; exactly one HTTP attempt, never retries."""
    global _LAST_REQUEST, _COOLDOWN_UNTIL
    count = len(destinations)
    unknown = lambda reason: tuple(_Estimate(reason=reason) for _ in range(count))
    now = time.monotonic()
    if now < _COOLDOWN_UNTIL:
        return unknown('busy')
    if _LAST_REQUEST is not None:
        delay = MIN_REQUEST_INTERVAL - (now - _LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
    _LAST_REQUEST = time.monotonic()
    request = Request(_request_url(origin, destinations), headers={
        'User-Agent': USER_AGENT, 'Accept': 'application/json',
    })
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        if error.code in (429, 503):
            _COOLDOWN_UNTIL = time.monotonic() + _retry_delay(error.headers)
            return unknown('busy')
        # OSRM reports NoSegment / NoRoute with HTTP 400. Preserve that local
        # batch outcome so a missing island/park road does not stop other areas.
        if error.code != 400:
            return unknown('unavailable')
        try:
            body = error.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError):
            return unknown('unavailable')
    except (URLError, TimeoutError, OSError):
        return unknown('unavailable')
    if len(body) > MAX_RESPONSE_BYTES:
        return unknown('invalid_response')
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return unknown('invalid_response')
    if not isinstance(data, dict):
        return unknown('invalid_response')
    if data.get('code') == 'NoSegment':
        # OSRM's MissingPhantomErrorMessage reports the zero-based coordinate
        # index, including the origin at 0. Only its exact known message form
        # establishes which point failed; never guess from an arbitrary error.
        # Source: Project-OSRM/osrm-backend include/engine/plugins/plugin_base.hpp
        message = data.get('message')
        match = (re.fullmatch(r'Could not find a matching segment for coordinate (\d+)\.?', message.strip())
                 if isinstance(message, str) else None)
        index = int(match[1]) if match else None
        if index == 0:
            return unknown('no_origin_route')
        if index is not None and 1 <= index <= count:
            return tuple(_Estimate(reason='unreachable' if i == index-1 else 'batch_unresolved')
                         for i in range(count))
        return unknown('batch_unresolved')
    if data.get('code') in ('NoTable', 'NoRoute'):
        return unknown('unreachable')
    if data.get('code') != 'Ok':
        return unknown('invalid_response')
    durations, distances = data.get('durations'), data.get('distances')
    if (not isinstance(durations, list) or len(durations) != 1
            or not isinstance(distances, list) or len(distances) != 1
            or not isinstance(durations[0], list) or len(durations[0]) != count
            or not isinstance(distances[0], list) or len(distances[0]) != count):
        return unknown('invalid_response')
    # Reject estimated fallback cells even if an upstream server adds them.
    fallback = data.get('fallback_speed_cells', [])
    if not isinstance(fallback, list):
        return unknown('invalid_response')
    fallback_indices = {cell[1] for cell in fallback
                        if isinstance(cell, list) and len(cell) == 2
                        and cell[0] == 0 and isinstance(cell[1], int)}
    estimates = []
    for i, (duration, distance) in enumerate(zip(durations[0], distances[0])):
        if duration is None or distance is None:
            estimates.append(_Estimate(reason='unreachable'))
            continue
        seconds, meters = _number(duration), _number(distance)
        if (i in fallback_indices or seconds is None or meters is None
                or seconds < 0 or meters < 0):
            estimates.append(_Estimate(reason='invalid_response'))
        else:
            estimates.append(_Estimate(seconds, meters, 'ok'))
    return tuple(estimates)


def _lookup(origin, destinations):
    key = (origin, tuple(destinations))
    if not _LOCK.acquire(timeout=LOCK_WAIT_SECONDS):
        return tuple(_Estimate(reason='busy') for _ in destinations)
    try:
        now = time.monotonic()
        for expired in [k for k, (expires, _) in _CACHE.items() if expires <= now]:
            _CACHE.pop(expired, None)
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached[1]
        estimates = _fetch_table(origin, destinations)
        ttl = CACHE_TTL if all(e.reason in ('ok', 'unreachable') for e in estimates) else FAILURE_CACHE_TTL
        _CACHE[key] = (time.monotonic() + ttl, estimates)
        while len(_CACHE) > CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)
        return estimates
    finally:
        _LOCK.release()


def _constraint(limit, label, estimate):
    result = {
        'max_minutes': limit,
        'verified': estimate.reason == 'ok',
        'duration_minutes': None,
        'distance_m': None,
        'within_limit': None,
        'origin_label': label,
        'message': '',
        'source': SOURCE,
        'source_url': SOURCE_URL,
        'attribution': ATTRIBUTION,
        'attribution_url': ATTRIBUTION_URL,
        'fix_map_url': FIX_MAP_URL,
        'live_traffic': False,
        'status': estimate.reason,
    }
    if estimate.reason == 'ok':
        minutes = math.ceil(estimate.seconds / 60)
        within = estimate.seconds <= limit * 60
        result.update(duration_minutes=minutes, distance_m=round(estimate.distance_m), within_limit=within)
        status = f'{limit:g}분 이내' if within else f'{limit:g}분 조건 초과'
        result['message'] = f'{label} 출발, 도로 기준 약 {minutes}분 · {status} · 실시간 교통 미반영'
    else:
        result['message'] = {
            'no_limit': '이동 시간 조건이 없어 경로 시간을 조회하지 않았어요.',
            'no_origin': '출발 위치를 확인해야 도로 이동 시간을 계산할 수 있어요.',
            'no_destination': '장소 좌표가 없어 도로 이동 시간을 확인할 수 없어요.',
            'batch_limit': '이번 검색의 경로 조회 범위를 넘어 이 장소는 아직 확인하지 않았어요.',
            'unreachable': '연결되는 자동차 경로를 확인하지 못했어요. 시간 조건 충족 여부는 미확인이에요.',
            'batch_unresolved': '함께 조회한 장소의 도로 연결 오류로 이 경로의 이동 시간을 확인하지 못했어요.',
            'no_origin_route': '출발 위치에서 연결되는 자동차 도로를 확인하지 못했어요. 출발 위치를 확인해 주세요.',
            'busy': '경로 조회가 잠시 지연되어 시간 조건 충족 여부를 확인하지 못했어요.',
            'invalid_response': '유효한 도로 이동 시간을 받지 못했어요. 시간 조건 충족 여부는 미확인이에요.',
        }.get(estimate.reason, '경로 시간을 불러오지 못했어요. 시간 조건 충족 여부는 미확인이에요.')
    return result


def enrich_travel_times(places, origin, max_minutes):
    """Return independent place copies enriched with road-time constraints.

    ``origin`` uses {lat, lon, label}; place coordinates may use latitude /
    longitude or lat / lon. For a city-level sample origin, the caller must use
    an explicit label such as '서울 시청 기준점(예시)'. No origin is inferred
    from the search region, and missing/invalid origins or limits make no calls.
    Only the first 12 valid-coordinate candidates are looked up; others remain
    unverified. ``verified`` means a road estimate exists, not a live ETA.
    """
    result = deepcopy(list(places))
    limit = _number(max_minutes)
    limit = limit if limit is not None and limit > 0 else None
    origin_point = _coordinates(origin)
    label = str(origin.get('label') or '출발 위치')[:160] if isinstance(origin, dict) else '출발 위치'
    selected, destinations = [], []
    for i, place in enumerate(result):
        if not isinstance(place, dict):
            raise TypeError('places must contain dictionaries')
        old_message = (place.get('travel_constraint') or {}).get('message')
        if old_message and isinstance(place.get('cautions'), list):
            place['cautions'] = [text for text in place['cautions'] if text != old_message]
        coordinates = _coordinates(place)
        reason = ('no_limit' if limit is None else 'no_origin' if origin_point is None
                  else 'no_destination' if coordinates is None else 'batch_limit')
        place['travel_constraint'] = _constraint(limit, label, _Estimate(reason=reason))
        if limit is not None and origin_point is not None and coordinates is not None and len(selected) < MAX_DESTINATIONS:
            selected.append(i)
            destinations.append(coordinates)
    if destinations:
        for index, estimate in zip(selected, _lookup(origin_point, destinations)):
            result[index]['travel_constraint'] = _constraint(limit, label, estimate)
    return result


def _lookup_candidate_batch(origin, destinations, *, network_failure=None, request_budget=None):
    """One bounded table lookup, reusing estimates by origin/destination pair.

    This cache deliberately excludes preference scores, time limits and labels:
    the same road estimate is reusable after a conversation changes its budget.
    It never persists a user's coordinates on disk.
    """
    if not _LOCK.acquire(timeout=LOCK_WAIT_SECONDS):
        return tuple(_Estimate(reason='busy') for _ in destinations)
    try:
        now = time.monotonic()
        for expired in [key for key, (expires, _) in _ROUTE_CACHE.items() if expires <= now]:
            _ROUTE_CACHE.pop(expired, None)
        resolved, missing = {}, []
        for destination in destinations:
            key = origin, destination
            cached = _ROUTE_CACHE.get(key)
            if cached is None:
                missing.append(destination)
            else:
                _ROUTE_CACHE.move_to_end(key)
                resolved[destination] = cached[1]
        if missing:
            if network_failure:
                estimates = tuple(_Estimate(reason=network_failure) for _ in missing)
            else:
                pending = list(missing)
                fetched = {}
                while pending:
                    response = _fetch_table(origin, pending)
                    if request_budget is not None and response and all(
                            item.reason in ('busy', 'unavailable', 'invalid_response', 'no_origin_route')
                            for item in response):
                        request_budget['failure'] = response[0].reason
                    fetched.update(zip(pending, response))
                    unresolved = [point for point, estimate in zip(pending, response)
                                  if estimate.reason == 'batch_unresolved']
                    # A valid indexed NoSegment yields exactly one unreachable
                    # point and unresolved neighbors. Retry only those neighbors
                    # with the known bad point removed, sharing a global budget.
                    can_repair = (unresolved and any(item.reason == 'unreachable' for item in response)
                                  and request_budget is not None and request_budget['repairs_left'] > 0)
                    if not can_repair:
                        break
                    request_budget['repairs_left'] -= 1
                    pending = unresolved
                estimates = tuple(fetched[point] for point in missing)
            for destination, estimate in zip(missing, estimates):
                resolved[destination] = estimate
                if network_failure:
                    continue
                ttl = CACHE_TTL if estimate.reason in ('ok', 'unreachable') else FAILURE_CACHE_TTL
                _ROUTE_CACHE[(origin, destination)] = (time.monotonic() + ttl, estimate)
            while len(_ROUTE_CACHE) > ROUTE_CACHE_MAX_ENTRIES:
                _ROUTE_CACHE.popitem(last=False)
        return tuple(resolved[destination] for destination in destinations)
    finally:
        _LOCK.release()


def enrich_candidate_travel_times(places, origin, max_minutes):
    """Screen a geographically diverse candidate pool before final ranking.

    Evaluate at most 160 distinct destination coordinates, in rate-limited
    batches of 40, with at most four additional repairs for identified bad
    destination points. Duplicated coordinates share a lookup and do not consume
    extra slots. Invalid/missing origins and limits make no requests. On a
    service-wide failure, stop this search instead of repeating failed calls.

    This function preserves every input place and its order. The caller must
    select only ``travel_constraint.within_limit is True`` for a time-bounded
    recommendation; unknown times and exceeded limits are not matches. Road
    times are estimates without live traffic, never straight-line guesses.
    """
    result = deepcopy(list(places))
    limit = _number(max_minutes)
    limit = limit if limit is not None and limit > 0 else None
    origin_point = _coordinates(origin)
    label = str(origin.get('label') or '출발 위치')[:160] if isinstance(origin, dict) else '출발 위치'
    coordinate_indices = OrderedDict()
    for index, place in enumerate(result):
        if not isinstance(place, dict):
            raise TypeError('places must contain dictionaries')
        old_message = (place.get('travel_constraint') or {}).get('message')
        if old_message and isinstance(place.get('cautions'), list):
            place['cautions'] = [text for text in place['cautions'] if text != old_message]
        coordinates = _coordinates(place)
        reason = ('no_limit' if limit is None else 'no_origin' if origin_point is None
                  else 'no_destination' if coordinates is None else 'batch_limit')
        place['travel_constraint'] = _constraint(limit, label, _Estimate(reason=reason))
        if limit is None or origin_point is None or coordinates is None:
            continue
        if coordinates in coordinate_indices:
            coordinate_indices[coordinates].append(index)
        elif len(coordinate_indices) < MAX_CANDIDATE_DESTINATIONS:
            coordinate_indices[coordinates] = [index]

    destinations = list(coordinate_indices)
    service_failure = None
    request_budget = {'repairs_left': MAX_CANDIDATE_REPAIRS, 'failure': None}
    for start in range(0, len(destinations), CANDIDATE_BATCH_SIZE):
        batch = destinations[start:start + CANDIDATE_BATCH_SIZE]
        estimates = _lookup_candidate_batch(origin_point, batch, network_failure=service_failure,
                                             request_budget=request_budget)
        for destination, estimate in zip(batch, estimates):
            for index in coordinate_indices[destination]:
                result[index]['travel_constraint'] = _constraint(limit, label, estimate)
        # A failed coordinate is local to its batch, but repeated timeouts or
        # server throttling must not turn one user's request into a retry loop.
        if request_budget['failure']:
            service_failure = request_budget['failure']
    return result
