"""Opt-in live check using an authorized fictional public station departure.

Never reads meeting.json, recordings, vehicle GPS, or host location. Writes only
public POI responses and authored test results below tests/live-travel/.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import recommend
import routing_duration
import trip_recommendation

ORIGIN = {'lat': 37.3947, 'lon': 127.1112, 'label': '판교역 공개 기준점(가상 출발지)'}
CASES = {
    'coast': ('판교역에서 출발해서 차로 두 시간 이내에 바다를 보고 싶어.', 120),
    'meal': ('판교역에서 출발해서 차로 한 시간 이내에 파스타 먹고 싶어. 피자도 같이 먹자.', 60),
    'cafe': ('판교역에서 출발해서 차로 한 시간 이내에 카페 가자. 빵이나 디저트도 먹고 싶어.', 60),
    'culture': ('판교역에서 출발해서 차로 한 시간 이내에 미술관이나 박물관 가자.', 60),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=CASES, required=True)
    args = parser.parse_args()
    folder = ROOT / 'tests' / 'live-travel'
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    prefix = folder / (stamp + '-' + args.case)
    original_json = recommend.get_json
    original_table = routing_duration._fetch_table
    calls = []

    def record_json(url, *a, **kw):
        start = time.monotonic()
        response = original_json(url, *a, **kw)
        prefix.with_suffix('.poi.json').write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
        calls.append({'service': 'Overpass', 'seconds': round(time.monotonic()-start, 2),
                      'elements': len(response.get('elements', [])), 'remark': response.get('remark')})
        print(json.dumps(calls[-1], ensure_ascii=False), flush=True)
        return response

    def record_table(origin, destinations):
        start = time.monotonic()
        result = original_table(origin, destinations)
        calls.append({'service': 'OSRM', 'seconds': round(time.monotonic()-start, 2),
                      'destinations': len(destinations), 'statuses': dict(Counter(e.reason for e in result))})
        print(json.dumps(calls[-1], ensure_ascii=False), flush=True)
        return result

    recommend.get_json = record_json
    routing_duration._fetch_table = record_table
    text, minutes = CASES[args.case]
    start = time.monotonic()
    report = {'case': args.case, 'origin': ORIGIN, 'started_at': stamp, 'calls': calls}
    print(json.dumps({'case': args.case, 'status': 'starting', 'limit_minutes': minutes}), flush=True)
    try:
        result = trip_recommendation.recommend_trip(
            [{'id': 'authored-live-check', 'seat': 'unknown', 'text': text}],
            [], folder, ORIGIN, minutes)
        report['result'] = result
        places = result['recommendations']
        assert all(p['travel_constraint'].get('within_limit') is True for p in places)
        assert all(p['travel_constraint'].get('live_traffic') is False for p in places)
        print(json.dumps({'case': args.case, 'report': result['request_profile']['search_report'],
                          'places': [{'name': p['name'], 'minutes': p['travel_constraint']['duration_minutes'],
                                      'category': p['category'], 'url': p['url']} for p in places]}, ensure_ascii=False), flush=True)
    except Exception as error:
        report['error'] = repr(error)
        causes = []
        cause = error
        while cause is not None:
            causes.append(repr(cause))
            cause = cause.__cause__
        report['causes'] = causes
        print(json.dumps({'case': args.case, 'error': causes}, ensure_ascii=False), flush=True)
    finally:
        report['seconds'] = round(time.monotonic()-start, 2)
        prefix.with_suffix('.report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'saved': str(prefix.with_suffix('.report.json')), 'seconds': report['seconds']}), flush=True)
    return 1 if 'error' in report else 0


if __name__ == '__main__':
    raise SystemExit(main())
