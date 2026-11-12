"""Local public POIs around an explicitly named destination search center.

A geocoded point is not an administrative boundary or the trip's departure.
Keep the legacy 20km beach / 4.5km other-place scopes and expose them clearly.
No recording, speaker data or vehicle coordinates enter this module.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time

from poi_index import index_path, query_candidates
from recommend import nominatim_search, valid_coordinates
from travel_discovery import _groups


_CENTERS = OrderedDict()
_CENTER_LOCK = threading.Lock()
_CENTER_TTL = 86400


def _cached_center(region, cache_dir, groups, menus):
    # These are only legacy named-region public-data keys, never location keys.
    keys = [region]
    beach = 'beach' in groups
    specific = region
    if beach:
        specific += f':radius=20000:beach=1:only={int(groups == ("beach",))}'
    if menus:
        specific += ':menu-search-v2:foods='+','.join(sorted(menus))
    keys.append(specific)
    for key in dict.fromkeys(keys):
        path = Path(cache_dir) / (hashlib.sha256(key.encode()).hexdigest()+'.json')
        try:
            if not path.is_file() or time.time()-path.stat().st_mtime >= 7*86400:
                continue
            cached = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(cached, dict) or cached.get('region') != region:
                continue
            center = cached.get('center')
            if not isinstance(center, dict):
                continue
            point = valid_coordinates(center.get('lat'), center.get('lon'))
            if point is not None:
                return point
        except (OSError, ValueError, TypeError):
            continue
    return None


def _center(region, cache_dir, groups, menus):
    with _CENTER_LOCK:
        cached = _CENTERS.get(region)
        if cached and time.monotonic()-cached[0] < _CENTER_TTL:
            _CENTERS.move_to_end(region)
            return cached[1]
    point = _cached_center(region, cache_dir, groups, menus)
    if point is None:
        try:
            locations = nominatim_search({'q':region, 'countrycodes':'kr', 'format':'jsonv2',
                                          'limit':1, 'accept-language':'ko'})
            point = valid_coordinates(locations[0].get('lat'), locations[0].get('lon')) if locations else None
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            return None
    if point is not None:
        with _CENTER_LOCK:
            _CENTERS[region] = (time.monotonic(), point)
            _CENTERS.move_to_end(region)
            while len(_CENTERS) > 64:
                _CENTERS.popitem(last=False)
    return point


def discover_region_candidates(region, cache_dir, profile):
    """Return local candidates, or None to retain the legacy remote fallback."""
    if not isinstance(region, str) or not region.strip() or len(region.strip()) > 70:
        return None
    if not index_path().is_file():
        return None
    region = region.strip()
    groups, menus = _groups(profile)
    # An unspecified activity did not include beaches in legacy region search.
    wanted = set(profile.get('requested_topics') or ()) - set(profile.get('excluded_topics') or ())
    if '해변' not in wanted:
        groups = tuple(group for group in groups if group != 'beach')
    if not groups:
        return None
    point = _center(region, cache_dir, groups, menus)
    if point is None:
        return None
    center = {'lat':point[0], 'lon':point[1], 'label':region+' 검색 기준점'}
    scopes = []
    ordinary = tuple(group for group in groups if group != 'beach')
    if ordinary:
        scopes.append((4500, ordinary))
    if 'beach' in groups:
        scopes.append((20000, ('beach',)))
    batches = [query_candidates(center, radius, kinds, menus) for radius, kinds in scopes]
    if any(batch is None for batch in batches):
        return None
    payload = deepcopy(batches[0])
    elements = {(place['type'], place['id']):place for batch in batches for place in batch['elements']}
    spans = [('해변' if kinds == ('beach',) else '그 외 장소' if 'beach' in groups else '장소')+
             f' 반경 약 {radius/1000:g}km' for radius, kinds in scopes]
    snapshot = payload.get('source_data_at')
    coverage = region+' 검색 기준점 · '+' · '.join(spans)
    if snapshot:
        coverage += ' · 지도 자료 '+str(snapshot)[:10]+' 기준'
    payload.update(region=region, center={'lat':point[0], 'lon':point[1]},
                   elements=list(elements.values()), coverage=coverage,
                   search_note='지역 이름으로 찾은 기준점 주변 후보예요. 행정구역 전체를 검색한 결과는 아니에요. '+
                               ' '.join(dict.fromkeys(batch.get('search_note','') for batch in batches)),
                   discovery={'mode':'destination_region_local_index', 'region':region,
                              'scope_kind':'radius_around_named_search_center',
                              'scopes':[{'radius_m':radius, 'groups':list(kinds)} for radius,kinds in scopes],
                              'candidate_count':len(elements), 'complete_inventory':False,
                              'source_data_at':snapshot, 'sources':[batch.get('discovery', {}) for batch in batches]})
    return payload
