"""Read-only local index of public OSM place data, independent of user trips.

The manual builder imports named nodes and ways from a regional OSM extract.
Only public place tags/coordinates are persisted: no OSM contributor metadata,
recordings, user locations, searches or route histories are written here.
"""
from datetime import datetime, timezone
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid


SCHEMA_VERSION = 2
COASTAL_MAX_DISTANCE_M = 1500
DEFAULT_DB_PATH = Path(__file__).resolve().parent / 'data' / 'maps' / 'pois.sqlite'
DEFAULT_SOURCE_URL = 'https://download.geofabrik.de/asia/south-korea-latest.osm.pbf'
GROUPS = frozenset(('meal', 'cafe', 'bakery', 'park', 'culture', 'beach'))
PUBLIC_TAGS = frozenset((
    'name', 'name:ko', 'name:en', 'amenity', 'cuisine', 'leisure', 'tourism',
    'shop', 'natural', 'brand', 'brand:ko', 'brand:en', 'brand:wikidata',
    'addr:city', 'addr:province', 'addr:suburb', 'addr:district', 'addr:street',
    'addr:housenumber', 'addr:postcode', 'opening_hours', 'website',
    'contact:website', 'phone', 'contact:phone', 'wheelchair', 'parking',
    'tidal', 'water', 'waterway',
))


def index_path(db_path=None):
    return Path(db_path or os.environ.get('CABIN_POI_INDEX') or DEFAULT_DB_PATH)


def category_for_tags(tags):
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


def public_tags(tags):
    """Copy only bounded business/place fields, never arbitrary OSM metadata."""
    return {key: value[:2000] for key, value in tags.items()
            if key in PUBLIC_TAGS and isinstance(value, str) and value.strip()}


def _row(element):
    from recommend import valid_coordinates
    if not isinstance(element, dict) or element.get('type') not in ('node', 'way'):
        return None
    identifier = element.get('id')
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
        return None
    raw_tags = element.get('tags')
    if not isinstance(raw_tags, dict):
        return None
    tags = public_tags(raw_tags)
    category = category_for_tags(tags)
    if category is None or not (tags.get('name:ko') or tags.get('name')):
        return None
    center = element.get('center') or {}
    if not isinstance(center, dict):
        center = {}
    point = valid_coordinates(element.get('lat', center.get('lat')),
                              element.get('lon', center.get('lon')))
    if point is None:
        return None
    return (element['type'], identifier, point[0], point[1], category,
            json.dumps(tags, ensure_ascii=False, separators=(',', ':')))


def _coast_row(coastline):
    from recommend import valid_coordinates
    if not isinstance(coastline, dict):
        return None
    identifier, points = coastline.get('id'), coastline.get('points')
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
        return None
    if not isinstance(points, (tuple, list)) or len(points) < 2:
        return None
    copied = []
    for point in points:
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            return None
        value = valid_coordinates(*point)
        if value is None:
            return None
        copied.append(value)
    return (identifier, min(p[0] for p in copied), max(p[0] for p in copied),
            min(p[1] for p in copied), max(p[1] for p in copied),
            json.dumps(copied, separators=(',', ':')))


def _atomic_index(output_path, metadata, fill):
    """Build beside the final file, then atomically replace only on success."""
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.building-' + uuid.uuid4().hex)
    connection = None
    try:
        connection = sqlite3.connect(temporary)
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('PRAGMA synchronous=NORMAL')
        connection.executescript('''
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE pois (
                osm_type TEXT NOT NULL, osm_id INTEGER NOT NULL,
                lat REAL NOT NULL, lon REAL NOT NULL,
                category TEXT NOT NULL, tags TEXT NOT NULL,
                PRIMARY KEY (osm_type, osm_id)
            ) WITHOUT ROWID;
            CREATE TABLE coastlines (
                osm_id INTEGER PRIMARY KEY,
                min_lat REAL NOT NULL, max_lat REAL NOT NULL,
                min_lon REAL NOT NULL, max_lon REAL NOT NULL,
                points TEXT NOT NULL
            );
        ''')
        buffer, coast_buffer = [], []
        def append(element):
            row = _row(element)
            if row is not None:
                buffer.append(row)
            if len(buffer) >= 1000:
                flush()
        def flush():
            if buffer:
                connection.executemany('INSERT OR REPLACE INTO pois VALUES (?, ?, ?, ?, ?, ?)', buffer)
                buffer.clear()
            if coast_buffer:
                connection.executemany('INSERT OR REPLACE INTO coastlines VALUES (?, ?, ?, ?, ?, ?)', coast_buffer)
                coast_buffer.clear()
        def append_coast(coastline):
            row = _coast_row(coastline)
            if row is not None:
                coast_buffer.append(row)
            if len(coast_buffer) >= 500:
                flush()
        fill(append, append_coast)
        flush()
        connection.execute('CREATE INDEX pois_category_lat_lon ON pois (category, lat, lon)')
        connection.execute('CREATE INDEX pois_lat_lon ON pois (lat, lon)')
        connection.execute('CREATE INDEX coastlines_bounds ON coastlines (min_lat, max_lat, min_lon, max_lon)')
        counts = dict(connection.execute('SELECT category, count(*) FROM pois GROUP BY category'))
        bounds = connection.execute('SELECT min(lat), min(lon), max(lat), max(lon) FROM pois').fetchone()
        result = dict(metadata)
        result.update(schema_version=SCHEMA_VERSION, status='ready',
                      built_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                      place_count=sum(counts.values()), category_counts=counts,
                      indexed_bounds=list(bounds) if counts else None,
                      coastline_way_count=connection.execute('SELECT count(*) FROM coastlines').fetchone()[0],
                      coastline_basis='OpenStreetMap natural=coastline',
                      included_osm_types=['node', 'way'], relations_included=False,
                      complete_inventory=False,
                      attribution='© OpenStreetMap contributors',
                      attribution_url='https://www.openstreetmap.org/copyright')
        connection.executemany('INSERT INTO metadata VALUES (?, ?)',
            [(key, json.dumps(value, ensure_ascii=False)) for key, value in result.items()])
        connection.commit()
        connection.close()
        connection = None
        os.replace(temporary, output)
        return result
    finally:
        if connection is not None:
            connection.close()
        # This is only our uniquely named, unfinished output; an existing final
        # index survives parsing errors and is never deleted before replacement.
        if temporary.is_file():
            temporary.unlink()


def write_index(elements, output_path, *, source_url=DEFAULT_SOURCE_URL,
                source_data_at=None, coverage='대한민국 공개 지도 자료', coastlines=()):
    """Create an index from already obtained public POIs (also used by tests)."""
    metadata = {'source_url': source_url, 'source_data_at': source_data_at,
                'coverage': coverage}
    def fill(append, append_coast):
        for element in elements:
            append(element)
        for coastline in coastlines:
            append_coast(coastline)
    return _atomic_index(output_path, metadata, fill)


def build_index(input_path, output_path=None, *, source_url=DEFAULT_SOURCE_URL,
                coverage='대한민국 공개 지도 자료'):
    """Manually build from an existing .osm.pbf/.osm file. Never downloads data.

    Way pins use the bounding-box center of complete known node geometry,
    matching Overpass's ``out center`` representation. They are not entrances.
    Relations are intentionally omitted and this limitation is stored/displayed.
    """
    import osmium
    from osmium.replication import get_replication_header
    source = Path(input_path).resolve()
    if not source.is_file():
        raise ValueError('먼저 공개 지도 원본 파일을 준비해 주세요.')
    output = index_path(output_path).resolve()
    if source == output:
        raise ValueError('지도 원본과 장소 색인 경로는 달라야 해요.')
    # libosmium's Windows file opener can reject an absolute path whose parent
    # contains non-ASCII characters. A path relative to this process avoids
    # changing cwd and works for the project's public data directory.
    osmium_path = os.path.relpath(source, Path.cwd())
    header = get_replication_header(osmium_path)
    source_data_at = header.timestamp.isoformat() if header.timestamp else None
    metadata = {'source_url': source_url, 'source_data_at': source_data_at,
                'source_file_bytes': source.stat().st_size, 'coverage': coverage,
                'geometry_basis': 'node point / complete-way bounding-box center'}
    def fill(append, append_coast):
        class PlaceHandler(osmium.SimpleHandler):
            def node(self, node):
                if not (node.tags.get('name') or node.tags.get('name:ko')):
                    return
                tags = public_tags(dict(node.tags))
                if category_for_tags(tags) is None or not node.location.valid():
                    return
                append({'type':'node', 'id':node.id, 'lat':node.lat, 'lon':node.lon, 'tags':tags})

            def way(self, way):
                coastal = way.tags.get('natural') == 'coastline'
                if not coastal and not (way.tags.get('name') or way.tags.get('name:ko')):
                    return
                tags = public_tags(dict(way.tags))
                if not coastal and category_for_tags(tags) is None:
                    return
                points = []
                for node in way.nodes:
                    if not node.location.valid():
                        return
                    points.append((node.lat, node.lon))
                if not points:
                    return
                if coastal:
                    append_coast({'id':way.id, 'points':points})
                    return
                append({'type':'way', 'id':way.id, 'tags':tags,
                        'lat':(min(p[0] for p in points) + max(p[0] for p in points)) / 2,
                        'lon':(min(p[1] for p in points) + max(p[1] for p in points)) / 2})
        # Location caching runs before these C++ filters in SimpleHandler.
        # Untagged geometry nodes remain available to ways without invoking a
        # Python callback for every road/building node in the country extract.
        # Unnamed coastline ways are needed to distinguish sea beaches from
        # inland/river beaches. The handler checks names only for actual POIs.
        filters = [osmium.filter.KeyFilter('amenity', 'leisure', 'tourism', 'shop', 'natural')]
        PlaceHandler().apply_file(osmium_path, locations=True, idx='flex_mem', filters=filters)
    return _atomic_index(output, metadata, fill)


def _bounds(point, radius):
    angular_radius = radius / 6371000
    latitude_span = math.degrees(angular_radius)
    longitude_span = (180 if abs(point[0])+latitude_span >= 90 else
                      math.degrees(math.asin(min(1, math.sin(angular_radius) / math.cos(math.radians(point[0]))))))
    return (max(-90, point[0]-latitude_span), min(90, point[0]+latitude_span),
            max(-180, point[1]-longitude_span), min(180, point[1]+longitude_span))


def _coastal_distance(connection, point):
    """Approximate local point-to-segment distance to indexed OSM coastlines.

    The geographic bounding box finds nearby coast geometry first. Distances
    use a local equirectangular projection at the candidate latitude, adequate
    for this 1.5km proximity check. A result is not proof of a view, entrance,
    bathing access, current shoreline/tide or an ocean-side driving route.
    """
    south, north, west, east = _bounds(point, COASTAL_MAX_DISTANCE_M * 1.01)
    rows = connection.execute(
        'SELECT points FROM coastlines WHERE min_lat <= ? AND max_lat >= ? '
        'AND min_lon <= ? AND max_lon >= ?', (north, south, east, west))
    scale_y = 6371000 * math.pi / 180
    scale_x = scale_y * math.cos(math.radians(point[0]))
    best = math.inf
    for (raw_points,) in rows:
        points = json.loads(raw_points)
        previous = None
        for latitude, longitude in points:
            current = ((longitude-point[1])*scale_x, (latitude-point[0])*scale_y)
            if previous is not None:
                ax, ay = previous
                dx, dy = current[0]-ax, current[1]-ay
                length_squared = dx*dx + dy*dy
                fraction = max(0, min(1, -(ax*dx + ay*dy)/length_squared)) if length_squared else 0
                best = min(best, math.hypot(ax + fraction*dx, ay + fraction*dy))
            previous = current
    return best if best <= COASTAL_MAX_DISTANCE_M else None


def coastal_proximity(latitude, longitude, *, db_path=None):
    """Return local coastline evidence within 1.5km, otherwise None.

    This lets explicit-region/online POI results undergo the same coastal
    check. It verifies proximity to the snapshot's coastline geometry only;
    it does not certify beach access, a sea view or current tidal conditions.
    """
    from recommend import valid_coordinates
    point = valid_coordinates(latitude, longitude)
    path = index_path(db_path)
    if point is None or not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as connection:
            metadata = {key: json.loads(value) for key, value in connection.execute('SELECT key, value FROM metadata')}
            if (metadata.get('schema_version') != SCHEMA_VERSION or metadata.get('status') != 'ready'
                    or type(metadata.get('coastline_way_count')) is not int or metadata['coastline_way_count'] <= 0):
                return None
            distance = _coastal_distance(connection, point)
    except (sqlite3.Error, ValueError, OSError, TypeError):
        return None
    if distance is None:
        return None
    return {'distance_m':round(distance), 'basis':'OpenStreetMap natural=coastline',
            'max_distance_m':COASTAL_MAX_DISTANCE_M,
            'source_data_at':metadata.get('source_data_at'),
            'method':'center_to_osm_coastline_segments'}


def query_candidates(origin, radius_m, groups, menus=(), *, db_path=None):
    """Return lookup-compatible local candidates, or None if no valid index.

    The database is opened read-only. Bounding-box lookup is followed by the
    same spherical-distance, category, menu and spatial-diversity normalization
    as the online discovery path. No request/user data is persisted or sent.
    """
    from recommend import valid_coordinates, straight_line_meters
    from travel_discovery import _normalize
    if not isinstance(origin, dict):
        raise ValueError('출발 위치를 확인해 주세요.')
    point = valid_coordinates(origin.get('lat', origin.get('latitude')),
                              origin.get('lon', origin.get('longitude')))
    if point is None or isinstance(radius_m, bool):
        raise ValueError('출발 위치와 검색 범위를 확인해 주세요.')
    try:
        radius = float(radius_m)
    except (TypeError, ValueError):
        raise ValueError('검색 범위를 확인해 주세요.') from None
    if not math.isfinite(radius) or not 0 < radius <= 2_000_000:
        raise ValueError('검색 범위를 확인해 주세요.')
    groups = tuple(dict.fromkeys(groups))
    if not groups or any(group not in GROUPS for group in groups):
        raise ValueError('검색할 장소 종류를 확인해 주세요.')
    path = index_path(db_path)
    if not path.is_file():
        return None
    south, north, west, east = _bounds(point, radius)
    coastal_proofs = {}
    sea_checked, sea_excluded, matched_index_count = 0, 0, 0
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as connection:
            metadata = {key: json.loads(value) for key, value in connection.execute('SELECT key, value FROM metadata')}
            if metadata.get('schema_version') != SCHEMA_VERSION or metadata.get('status') != 'ready':
                return None
            has_coastline = (type(metadata.get('coastline_way_count')) is int
                             and metadata['coastline_way_count'] > 0)
            placeholders = ','.join('?' for _ in groups)
            cursor = connection.execute(
                f'SELECT osm_type, osm_id, lat, lon, tags FROM pois WHERE category IN ({placeholders}) '
                'AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?',
                (*groups, south, north, west, east))
            elements = []
            for osm_type, identifier, lat, lon, raw_tags in cursor:
                matched_index_count += 1
                tags = json.loads(raw_tags)
                if isinstance(tags, dict):
                    if tags.get('natural') == 'beach':
                        if straight_line_meters(point, (lat, lon)) > radius:
                            continue
                        sea_checked += 1
                        proximity = _coastal_distance(connection, (lat, lon)) if has_coastline else None
                        if proximity is None:
                            sea_excluded += 1
                            continue
                        coastal_proofs[(osm_type, identifier)] = proximity
                    elements.append({'type':osm_type, 'id':identifier, 'lat':lat, 'lon':lon, 'tags':tags})
    except (sqlite3.Error, ValueError, OSError, TypeError):
        return None
    candidates = _normalize(elements, point, radius, groups, menus)
    for candidate in candidates:
        proximity = coastal_proofs.get((candidate['type'], candidate['id']))
        if proximity is not None:
            candidate['coastal_distance_m'] = round(proximity)
            candidate['coastal_basis'] = 'OpenStreetMap natural=coastline'
    stamp = metadata.get('source_data_at')
    note = ('공개 지도에 이름이 등록된 장소를 비교해요. 일부 복합 경계 장소와 아직 등록되지 않은 장소는 '
            '포함하지 않아요. 도로 이동 시간은 별도로 확인해야 해요.')
    note += ' 지도 자료 기준일: '+str(stamp)[:10]+'.' if stamp else ' 지도 자료의 정확한 기준일은 확인되지 않았어요.'
    if 'beach' in groups:
        note += (' 바다 후보는 공개 해안선에서 약 1.5km 이내로 확인된 해변만 포함해요. '
                 '해변 중심점의 해안 근접 여부이며 실제 입구·바다 전망·물놀이 가능 여부는 별도 확인이 필요해요.')
    return {
        'region':'출발지 기준 여러 지역', 'center':{'lat':point[0], 'lon':point[1]},
        'elements':candidates, 'fetched_at':time.time(), 'source_data_at':stamp,
        'coverage':str(metadata.get('coverage') or '공개 지도 자료')+f' · 출발지 반경 약 {radius/1000:g}km',
        'lookup_source':'OpenStreetMap · Geofabrik 공개 자료',
        'source_url':metadata.get('source_url'), 'menu_search_complete':True,
        'search_note':note,
        'discovery':{'mode':'local_public_index', 'radius_m':radius,
                     'candidate_count':len(candidates), 'matched_index_count':matched_index_count,
                     'indexed_place_count':metadata.get('place_count'),
                     'requested_groups':list(groups), 'requires_route_check':True,
                     'included_osm_types':metadata.get('included_osm_types', []),
                     'relations_included':False, 'complete_inventory':False,
                     'coastal_filter':{'enabled':'beach' in groups,
                         'method':'center_to_osm_coastline_segments',
                         'max_distance_m':COASTAL_MAX_DISTANCE_M,
                         'geometry_available':has_coastline,
                         'coastline_way_count':metadata.get('coastline_way_count', 0),
                         'evaluated_beach_count':sea_checked,
                         'excluded_inland_or_unverified_count':sea_excluded,
                         'source_data_at':stamp},
                     'source_data_at':stamp, 'source_url':metadata.get('source_url')},
    }
