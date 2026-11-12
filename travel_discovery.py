"""Bounded, geographically distributed OSM discovery before road-time filtering.

The search envelope is deliberately larger than a likely driving catchment.
It is NOT an isochrone: every returned place still needs a road-time check.
Only fixed catalog expressions, coordinates, and numeric bounds reach Overpass;
conversation text and speaker information never leave this process.
"""
import copy
import math
import os
import threading
import time
from collections import OrderedDict, deque
from urllib.parse import urlencode


OVERPASS_URL = os.environ.get('CABIN_OVERPASS_URL', 'https://overpass-api.de/api/interpreter')
CACHE_TTL = 6 * 3600
CACHE_LIMIT = 24
MAX_ELEMENTS = 640
MAX_RADIUS_M = 2_000_000
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()


def _point(origin):
    from recommend import valid_coordinates
    if not isinstance(origin, dict):
        return None
    return valid_coordinates(origin.get('lat', origin.get('latitude')),
                             origin.get('lon', origin.get('longitude')))


def search_radius(max_minutes):
    """A conservative discovery envelope, never a predicted journey duration."""
    if isinstance(max_minutes, bool):
        raise ValueError('이동 시간을 확인해 주세요.')
    try:
        minutes = float(max_minutes)
    except (TypeError, ValueError):
        raise ValueError('이동 시간을 확인해 주세요.') from None
    if not math.isfinite(minutes) or not 1 <= minutes <= 1440:
        raise ValueError('이동 시간은 1분부터 24시간까지 지정해 주세요.')
    # A generous 130 km/h envelope avoids equating a small local radius with
    # a two-hour drive. The later routing stage rejects unreachable candidates.
    return min(MAX_RADIUS_M, max(3000, math.ceil(minutes * 130000 / 60)))


def _groups(profile):
    from recommend import MENU_TOPICS, MENU_SEARCH_TERMS
    wanted = set(profile.get('requested_topics') or ())
    excluded = set(profile.get('excluded_topics') or ())
    wanted -= excluded
    menus = tuple(sorted((set(profile.get('requested_menu_topics') or ()) | wanted)
                         & MENU_SEARCH_TERMS.keys() - excluded))
    groups = []
    if '식사' in wanted or wanted & (MENU_TOPICS - {'빵·디저트', '매운 음식'}):
        groups.append('meal')
    groups.extend(group for topic, group in [('카페', 'cafe'), ('빵·디저트', 'bakery'),
                                            ('산책', 'park'), ('전시·문화', 'culture'),
                                            ('해변', 'beach')] if topic in wanted)
    if not groups:
        groups = ['meal', 'cafe', 'bakery', 'park', 'culture', 'beach']
    return tuple(groups), menus


def _selectors(group, menus=()):
    from recommend import MENU_SEARCH_TERMS
    # Search both public names under the SAME output quota. Normalization
    # prefers name:ko, so requiring name here would otherwise discard a place
    # before its Korean label (or requested dish in that label) can be scored.
    def named(base):
        return tuple(base + '["' + key + '"]' for key in ('name', 'name:ko'))

    if group == 'meal':
        base = '["amenity"~"^(restaurant|fast_food|food_court)$"]'
        if menus:
            names = '|'.join(MENU_SEARCH_TERMS[topic][0] for topic in menus)
            cuisines = '|'.join(MENU_SEARCH_TERMS[topic][1] for topic in menus)
            return (tuple(base + '["' + key + '"~"' + names + '"]'
                          for key in ('name', 'name:ko'))
                    + named(base + '["cuisine"~"(^|;)(' + cuisines + ')(;|$)"]'))
        return named(base)
    bases = {
        'cafe': ('["amenity"="cafe"]',),
        'bakery': ('["shop"="bakery"]', '["amenity"="ice_cream"]'),
        'park': ('["leisure"="park"]',),
        'culture': ('["tourism"~"^(museum|gallery)$"]',),
        'beach': ('["natural"="beach"]',),
    }[group]
    return tuple(selector for base in bases for selector in named(base))


def _tiles(latitude, longitude, radius, divisions=None):
    """A grid rather than a city list: no city or user's home is hardcoded."""
    lat_span = radius / 111320
    lon_span = lat_span / max(math.cos(math.radians(latitude)), .1)
    divisions = divisions or (4 if radius > 30000 else 2)
    south, north = max(-89.9, latitude-lat_span), min(89.9, latitude+lat_span)
    west, east = max(-180, longitude-lon_span), min(180, longitude+lon_span)
    return [(south+(north-south)*row/divisions, west+(east-west)*col/divisions,
             south+(north-south)*(row+1)/divisions, west+(east-west)*(col+1)/divisions)
            for row in range(divisions) for col in range(divisions)]


def build_query(point, radius, groups, menus):
    """One request, with separate quotas so one dense city cannot fill it."""
    latitude, longitude = point
    around = f'(around:{radius},{latitude:.6f},{longitude:.6f})'
    query = ['[out:json][timeout:25][maxsize:134217728];']
    tiles = _tiles(latitude, longitude, radius)
    if radius > 30000:
        # The broad grid alone has large cells (130km wide for a 2h trip).
        # Additional smaller cells reserve nearer candidates rather than
        # relying on the first few OSM IDs in each huge cell being reachable.
        inner_radius = min(radius / 3, 80000)
        tiles = _tiles(latitude, longitude, inner_radius, divisions=2) + tiles
    local_quota, menu_quota = 8, 8
    remaining = MAX_ELEMENTS - len(groups)*local_quota - len(menus)*menu_quota
    tile_quota = max(2, min(24, remaining // (len(groups)*len(tiles))))

    # Fetch each kind once using the indexed rectangle, then partition the
    # small named set. Repeating a 260km around scan for every tile timed out
    # against the live service even for beaches.
    envelope = _tiles(latitude, longitude, radius, divisions=1)[0]
    bbox = '(' + ','.join(f'{value:.6f}' for value in envelope) + ')'
    for group in groups:
        query.append('(' + ''.join('nwr'+bbox+selector+';' for selector in _selectors(group, menus))
                     + f')->.g_{group};')

    def add(group, area, quota, selectors=('',)):
        query.append('(' + ''.join('nwr.g_'+group+area+selector+';' for selector in selectors)
                     + f');out center tags {quota};')

    # Include nearby options even when the broad tile also covers a large city.
    local = f'(around:{min(radius, 4500)},{latitude:.6f},{longitude:.6f})'
    for group in groups:
        add(group, local, local_quota)
    for bounds in tiles:
        area = around + '(' + ','.join(f'{value:.6f}' for value in bounds) + ')'
        for group in groups:
            add(group, area, tile_quota)
    # A requested dish gets its own allowance, even if another common cuisine
    # dominates the food results. These are fixed dictionary patterns only.
    for topic in menus:
        add('meal', around, menu_quota, _selectors('meal', (topic,)))
    return ''.join(query)


def _element_point(element):
    from recommend import valid_coordinates
    center = element.get('center') or {}
    if not isinstance(center, dict):
        center = {}
    return valid_coordinates(element.get('lat', center.get('lat')),
                             element.get('lon', center.get('lon')))


def _category(tags):
    if tags.get('natural') == 'beach':
        return 'beach'
    if tags.get('leisure') == 'park':
        return 'park'
    if tags.get('tourism') in ('museum', 'gallery'):
        return 'culture'
    if tags.get('shop') == 'bakery' or tags.get('amenity') == 'ice_cream':
        return 'bakery'
    if tags.get('amenity') == 'cafe':
        return 'cafe'
    if tags.get('amenity') in ('restaurant', 'fast_food', 'food_court'):
        return 'meal'
    return None


def _normalize(elements, point, radius, groups, menus):
    from recommend import straight_line_meters, mapped_meal_topics
    seen, buckets = set(), OrderedDict()
    lat_scale = radius / 111320 / 4
    lon_scale = lat_scale / max(math.cos(math.radians(point[0])), .1)
    for element in elements:
        if not isinstance(element, dict):
            continue
        kind, identifier = element.get('type'), element.get('id')
        if kind not in ('node', 'way', 'relation') or isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            continue
        key = kind, identifier
        tags = element.get('tags')
        if key in seen or not isinstance(tags, dict):
            continue
        name = tags.get('name:ko') or tags.get('name')
        coordinates = _element_point(element)
        category = _category(tags)
        if not isinstance(name, str) or not name.strip() or coordinates is None or category not in groups:
            continue
        distance = straight_line_meters(point, coordinates)
        if distance > radius:
            continue
        matched_menus = tuple(sorted(set(menus) & mapped_meal_topics(tags))) if category == 'meal' else ()
        if category == 'meal' and menus and not matched_menus:
            continue
        seen.add(key)
        # Only cap an unexpectedly large/malformed response after geography and
        # topic partitioning; preserve the original public tags for scoring.
        cell = (math.floor((coordinates[0]-point[0])/lat_scale),
                math.floor((coordinates[1]-point[1])/lon_scale), category, matched_menus)
        clean = {'type':kind, 'id':identifier, 'lat':coordinates[0], 'lon':coordinates[1],
                 'tags':copy.deepcopy(tags)}
        if element.get('coastal_basis') == 'OpenStreetMap natural=coastline':
            clean['coastal_basis'] = element['coastal_basis']
            clean['coastal_distance_m'] = element.get('coastal_distance_m')
        buckets.setdefault(cell, []).append((distance, clean))
    queues = deque(deque(element for _, element in sorted(values, key=lambda pair: (pair[0], pair[1]['id'])))
                   for values in buckets.values())
    result = []
    while queues and len(result) < MAX_ELEMENTS:
        bucket = queues.popleft()
        result.append(bucket.popleft())
        if bucket:
            queues.append(bucket)
    return result


def discover_candidates(origin, max_minutes, profile, *, fetcher=None):
    """Return lookup-compatible candidates; caller MUST route-filter them.

    ``origin`` accepts {lat,lon,label} or {latitude,longitude}. ``profile`` is
    recommend.request_profile output. No location-derived disk cache is used.
    An injected get_json-style fetcher is useful for deterministic tests.
    """
    from recommend import get_json
    point = _point(origin)
    if point is None:
        raise ValueError('출발 위치를 확인한 뒤 이동 시간에 맞는 장소를 찾을 수 있어요.')
    if not isinstance(profile, dict):
        raise ValueError('대화에서 나온 조건을 확인해 주세요.')
    radius = search_radius(max_minutes)
    groups, menus = _groups(profile)
    # An installed public country extract avoids depending on a public query
    # server for each drive. It contains public POIs, never a user's location.
    # Injected fetchers intentionally exercise the remote-query path in tests.
    if fetcher is None:
        from poi_index import query_candidates
        local = query_candidates(origin, radius, groups, menus)
        if local is not None:
            local.setdefault('discovery', {}).update(requires_route_check=True,
                radius_capped=float(max_minutes)*130000/60 > MAX_RADIUS_M)
            return local
    key = (point, radius, groups, menus)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic()-cached[0] < CACHE_TTL:
            _CACHE.move_to_end(key)
            return copy.deepcopy(cached[1])
    # The public service receives one bounded query, never a request per city.
    with _FETCH_LOCK:
        query = build_query(point, radius, groups, menus)
        try:
            result = (fetcher or get_json)(OVERPASS_URL,
                data=urlencode({'data':query}).encode('utf-8'), timeout=30)
            if not isinstance(result, dict) or result.get('remark') or not isinstance(result.get('elements'), list):
                raise ValueError('Incomplete place response')
            elements = _normalize(result['elements'], point, radius, groups, menus)
        except Exception as error:
            raise ValueError('여러 지역의 장소 검색을 완료하지 못했어요. 대화는 유지돼요. 잠시 후 다시 추천해 주세요.') from error
    payload = {
        'region':'출발지 기준 여러 지역', 'center':{'lat':point[0], 'lon':point[1]},
        'elements':elements, 'fetched_at':time.time(),
        'coverage':f'출발지에서 여러 방향으로 반경 약 {radius/1000:g}km 내 후보 탐색',
        'lookup_source':'OpenStreetMap · Overpass API', 'menu_search_complete':True,
        'search_note':'여러 지역의 공개 장소 후보를 찾아 도로 이동 시간을 비교해요. 검색 반경 전체가 시간 안에 도착 가능한 범위는 아니며, 모든 장소를 포함하지는 않아요.',
        'discovery':{'mode':'travel_envelope', 'radius_m':radius, 'candidate_count':len(elements),
                     'requested_groups':list(groups), 'requires_route_check':True,
                     'complete_inventory':False,
                     'radius_capped':float(max_minutes)*130000/60 > MAX_RADIUS_M},
    }
    if payload['discovery']['radius_capped']:
        payload['search_note'] += ' 검색 범위는 서비스 제한으로 출발지 반경 2,000km까지예요.'
    with _LOCK:
        _CACHE[key] = (time.monotonic(), copy.deepcopy(payload))
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return payload
