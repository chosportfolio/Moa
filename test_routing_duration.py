import io
import json
import threading
import unittest
from collections import OrderedDict
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import routing_duration as routing


class RoutingDurationTests(unittest.TestCase):
    def setUp(self):
        # Public landmarks only; no live user location in tests.
        self.origin = {'lat': 37.5663, 'lon': 126.9779, 'label': '서울 시청 기준점(예시)'}
        self.places = [{'id': 'a', 'latitude': 37.5796, 'longitude': 126.9770,
                        'metadata': {'tags': ['궁궐']}},
                       {'id': 'b', 'lat': 37.5512, 'lon': 126.9882}]
        for name, value in [('_CACHE', OrderedDict()), ('_ROUTE_CACHE', OrderedDict()), ('_LOCK', threading.Lock()),
                            ('_LAST_REQUEST', None), ('_COOLDOWN_UNTIL', 0),
                            ('MIN_REQUEST_INTERVAL', 0)]:
            patcher = patch.object(routing, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def response(self, durations, distances=None, **extra):
        if distances is None:
            distances = [1200] * len(durations)
        return io.BytesIO(json.dumps({'code': 'Ok', 'durations': [durations],
                                     'distances': [distances], **extra}).encode())

    def test_one_https_table_request_and_independent_results(self):
        self.places[0]['travel_constraint']={'message':'경로 소요 시간을 확인해야 해요.'}
        self.places[0]['cautions']=['주차 확인', '경로 소요 시간을 확인해야 해요.']
        with patch.object(routing, 'urlopen', return_value=self.response([7200, 7200.1])) as request:
            result = routing.enrich_travel_times(self.places, self.origin, 120)
        self.assertEqual(result[0]['cautions'], ['주차 확인'])
        self.assertEqual(request.call_count, 1)
        req = request.call_args.args[0]
        parts = urlsplit(req.full_url)
        self.assertEqual(parts.scheme, 'https')
        self.assertEqual(parts.netloc, 'routing.openstreetmap.de')
        self.assertTrue(parts.path.startswith('/routed-car/table/v1/driving/126.977900,37.566300;'))
        query = parse_qs(parts.query)
        self.assertEqual(query['sources'], ['0'])
        self.assertEqual(query['destinations'], ['1;2'])
        self.assertEqual(query['annotations'], ['duration,distance'])
        self.assertNotIn('fallback_speed', query)
        self.assertEqual(len(query['radiuses'][0].split(';')), 3)
        self.assertEqual(req.get_header('User-agent'), routing.USER_AGENT)
        self.assertEqual(request.call_args.kwargs['timeout'], routing.REQUEST_TIMEOUT)
        first, second = (p['travel_constraint'] for p in result)
        self.assertTrue(first['verified'])
        self.assertTrue(first['within_limit'])
        self.assertFalse(second['within_limit'])
        self.assertEqual(second['duration_minutes'], 121)
        self.assertIn('실시간 교통 미반영', first['message'])
        self.assertIn('기준점(예시)', first['origin_label'])
        self.assertEqual(first['source'], 'OSRM · OpenStreetMap')
        self.assertEqual(first['fix_map_url'], 'https://www.openstreetmap.org/edit')
        result[0]['metadata']['tags'].append('changed')
        self.assertEqual(self.places[0]['travel_constraint'], {'message':'경로 소요 시간을 확인해야 해요.'})
        self.assertEqual(self.places[0]['metadata']['tags'], ['궁궐'])

    def test_missing_invalid_origins_and_limits_make_no_network_call(self):
        with patch.object(routing, 'urlopen') as request:
            for origin in (None, {}, {'lat': float('nan'), 'lon': 127},
                           {'lat': True, 'lon': 127}, {'lat': 91, 'lon': 127}):
                result = routing.enrich_travel_times(self.places, origin, 120)
                self.assertFalse(result[0]['travel_constraint']['verified'])
                self.assertIsNone(result[0]['travel_constraint']['within_limit'])
            for limit in (None, 0, -1, float('nan'), float('inf'), True, 'bad'):
                result = routing.enrich_travel_times(self.places, self.origin, limit)
                self.assertIsNone(result[0]['travel_constraint']['max_minutes'])
            routing.enrich_travel_times([], self.origin, 120)
            request.assert_not_called()

    def test_null_negative_nonfinite_and_fallback_cells_stay_unknown(self):
        values = [None, -1, float('nan'), float('inf'), True, 60, 60]
        places = [dict(self.places[0], id=str(i)) for i in range(len(values))]
        with patch.object(routing, 'urlopen', return_value=self.response(
                values, [100, 100, 100, 100, 100, -1, 100], fallback_speed_cells=[[0, 6]])):
            results = routing.enrich_travel_times(places, self.origin, 120)
        for result in results:
            c = result['travel_constraint']
            self.assertFalse(c['verified'])
            self.assertIsNone(c['duration_minutes'])
            self.assertIsNone(c['distance_m'])
            self.assertIsNone(c['within_limit'])

    def test_unreachable_bad_schema_timeout_and_oversize_are_unknown(self):
        failures = [io.BytesIO(b'{"code":"NoTable"}'), io.BytesIO(b'{"code":"NoSegment"}'),
                    io.BytesIO(b'{"code":"Ok","durations":[[1]],"distances":[[2]]}'),
                    io.BytesIO(b'not json'), io.BytesIO(b'[]'),
                    io.BytesIO(b' ' * (routing.MAX_RESPONSE_BYTES + 1)),
                    TimeoutError('timeout'), URLError('offline')]
        for failure in failures:
            routing._CACHE.clear()
            kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with self.subTest(failure=type(failure).__name__), patch.object(routing, 'urlopen', **kwargs) as request:
                result = routing.enrich_travel_times(self.places, self.origin, 120)
                self.assertEqual(request.call_count, 1)
                self.assertFalse(result[0]['travel_constraint']['verified'])

    def test_twelve_destinations_max_invalid_coordinate_does_not_consume_slot(self):
        places = [{'id': 'invalid', 'latitude': 37}] + [dict(self.places[0], id=str(i)) for i in range(14)]
        with patch.object(routing, 'urlopen', return_value=self.response([60] * 12)) as request:
            result = routing.enrich_travel_times(places, self.origin, 120)
        self.assertEqual(request.call_count, 1)
        query = parse_qs(urlsplit(request.call_args.args[0].full_url).query)
        self.assertEqual(len(query['destinations'][0].split(';')), 12)
        self.assertFalse(result[0]['travel_constraint']['verified'])
        self.assertEqual(sum(p['travel_constraint']['verified'] for p in result), 12)
        self.assertIn('아직 확인하지', result[-1]['travel_constraint']['message'])

    def test_cache_reuses_road_result_but_recalculates_limit_and_origin_label(self):
        with patch.object(routing, 'urlopen', return_value=self.response([3600, 3700])) as request:
            first = routing.enrich_travel_times(self.places, self.origin, 120)
            second = routing.enrich_travel_times(self.places, dict(self.origin, label='다른 표기'), 30)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(first[0]['travel_constraint']['within_limit'])
        self.assertFalse(second[0]['travel_constraint']['within_limit'])
        self.assertEqual(second[0]['travel_constraint']['origin_label'], '다른 표기')

    def test_cache_is_bounded_and_expired_entries_are_removed(self):
        with patch.object(routing, 'CACHE_MAX_ENTRIES', 2), patch.object(routing, 'time') as clock, \
                patch.object(routing, 'urlopen', side_effect=lambda *a, **k: self.response([60, 60])) as request:
            clock.monotonic.return_value = 10
            for i in range(3):
                routing.enrich_travel_times(self.places, dict(self.origin, lat=37 + i * .01), 120)
            self.assertEqual(len(routing._CACHE), 2)
            clock.monotonic.return_value = 10 + routing.CACHE_TTL + 1
            routing.enrich_travel_times(self.places, self.origin, 120)
            self.assertEqual(len(routing._CACHE), 1)
            self.assertEqual(request.call_count, 4)

    def test_distinct_queries_are_throttled(self):
        with patch.object(routing, 'MIN_REQUEST_INTERVAL', 1.1), \
                patch.object(routing.time, 'monotonic', return_value=100), \
                patch.object(routing.time, 'sleep') as sleep, \
                patch.object(routing, 'urlopen', side_effect=lambda *a, **k: self.response([60, 60])) as request:
            routing.enrich_travel_times(self.places, self.origin, 120)
            routing.enrich_travel_times(self.places, dict(self.origin, lat=37.57), 120)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.1)

    def test_429_respects_retry_after_and_does_not_retry_or_fake_time(self):
        error = HTTPError(routing.OSRM_TABLE_URL, 429, 'Too Many Requests', {'Retry-After': '120'}, None)
        with patch.object(routing.time, 'monotonic', return_value=100), \
                patch.object(routing, 'urlopen', side_effect=error) as request:
            first = routing.enrich_travel_times(self.places, self.origin, 120)
            second = routing.enrich_travel_times(self.places, dict(self.origin, lat=37.57), 120)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(routing._COOLDOWN_UNTIL, 220)
        self.assertFalse(first[0]['travel_constraint']['verified'])
        self.assertIsNone(second[0]['travel_constraint']['duration_minutes'])

    def candidate_places(self, count):
        return [{'id': f'candidate-{i}', 'latitude': 37.5 + i * .001, 'longitude': 127}
                for i in range(count)]

    def response_for_request(self, request, **kwargs):
        query = parse_qs(urlsplit(request.full_url).query)
        count = len(query['destinations'][0].split(';'))
        return self.response([3600] * count)

    def test_candidate_pool_checks_beyond_twelve_in_bounded_batches(self):
        places = self.candidate_places(165)
        with patch.object(routing, 'urlopen', side_effect=self.response_for_request) as request:
            result = routing.enrich_candidate_travel_times(places, self.origin, 120)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(len(result), 165)
        self.assertEqual([p['id'] for p in result], [p['id'] for p in places])
        self.assertTrue(result[120]['travel_constraint']['within_limit'])
        self.assertEqual(sum(p['travel_constraint']['verified'] for p in result), 160)
        for call in request.call_args_list:
            query = parse_qs(urlsplit(call.args[0].full_url).query)
            self.assertEqual(len(query['destinations'][0].split(';')), 40)
        self.assertEqual(result[160]['travel_constraint']['status'], 'batch_limit')
        self.assertNotIn('travel_constraint', places[0])

    def test_candidate_duplicate_coordinates_share_slots_and_results(self):
        places = [{'id': 'invalid', 'lat': 37}] + self.candidate_places(160)
        places += [dict(places[1], id='duplicate-after-cap'), {'id': 'over-cap', 'lat': 38.5, 'lon': 127}]
        with patch.object(routing, 'urlopen', side_effect=self.response_for_request) as request:
            result = routing.enrich_candidate_travel_times(places, self.origin, 120)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(result[0]['travel_constraint']['status'], 'no_destination')
        self.assertTrue(result[-2]['travel_constraint']['within_limit'])
        self.assertEqual(result[-1]['travel_constraint']['status'], 'batch_limit')

    def test_candidate_cache_reuses_reordered_subset_and_rechecks_time_budget(self):
        places = self.candidate_places(60)
        with patch.object(routing, 'urlopen', side_effect=self.response_for_request) as request:
            first = routing.enrich_candidate_travel_times(places, self.origin, 120)
            second = routing.enrich_candidate_travel_times(
                list(reversed(places[10:50])), dict(self.origin, label='명시한 출발지'), 30)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(first[-1]['travel_constraint']['within_limit'])
        self.assertFalse(second[0]['travel_constraint']['within_limit'])
        self.assertEqual(second[0]['travel_constraint']['origin_label'], '명시한 출발지')

    def test_candidate_timeout_stops_requests_and_keeps_times_unknown(self):
        with patch.object(routing, 'urlopen', side_effect=TimeoutError('offline')) as request:
            results = routing.enrich_candidate_travel_times(self.candidate_places(120), self.origin, 120)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(all(p['travel_constraint']['within_limit'] is None for p in results))
        self.assertTrue(all(p['travel_constraint']['duration_minutes'] is None for p in results))

    def test_candidate_batch_failure_does_not_discard_cached_other_places(self):
        places = self.candidate_places(80)
        with patch.object(routing, 'urlopen', side_effect=self.response_for_request):
            routing.enrich_candidate_travel_times(places[40:], self.origin, 120)
        with patch.object(routing, 'urlopen', side_effect=TimeoutError('offline')) as request:
            results = routing.enrich_candidate_travel_times(places, self.origin, 120)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(all(p['travel_constraint']['within_limit'] is None for p in results[:40]))
        self.assertTrue(all(p['travel_constraint']['within_limit'] is True for p in results[40:]))

    def test_candidate_unreachable_batch_does_not_block_other_regions(self):
        error = HTTPError(routing.OSRM_TABLE_URL, 400, 'Bad Request', {},
                          io.BytesIO(b'{"code":"NoSegment"}'))
        with patch.object(routing, 'urlopen', side_effect=[
                error, self.response([600] * 20)]) as request:
            results = routing.enrich_candidate_travel_times(self.candidate_places(60), self.origin, 120)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(results[0]['travel_constraint']['status'], 'batch_unresolved')
        self.assertTrue(results[-1]['travel_constraint']['within_limit'])

    def no_segment(self, index=None):
        payload = {'code': 'NoSegment'}
        if index is not None:
            payload['message'] = f'Could not find a matching segment for coordinate {index}'
        return HTTPError(routing.OSRM_TABLE_URL, 400, 'Bad Request', {},
                         io.BytesIO(json.dumps(payload).encode()))

    def test_identified_bad_destination_does_not_poison_its_neighbors(self):
        places = self.candidate_places(60)
        with patch.object(routing, 'urlopen', side_effect=[
                self.no_segment(3), self.response([600]*39), self.response([600]*20)]) as request:
            results = routing.enrich_candidate_travel_times(places, self.origin, 120)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(results[2]['travel_constraint']['status'], 'unreachable')
        self.assertEqual(sum(p['travel_constraint']['verified'] for p in results), 59)
        repaired = urlsplit(request.call_args_list[1].args[0].full_url).path.split('/')[-1].split(';')
        self.assertEqual(len(repaired), 40)  # origin + 39 remaining destinations
        self.assertNotIn('127.000000,37.502000', repaired)
        self.assertIn('127.000000,37.501000', repaired)
        with patch.object(routing, 'urlopen') as cached_request:
            cached = routing.enrich_candidate_travel_times(list(reversed(places)), self.origin, 120)
        cached_request.assert_not_called()
        self.assertEqual(sum(p['travel_constraint']['verified'] for p in cached), 59)

    def test_unindexed_or_invalid_index_does_not_guess_or_cache_neighbors_as_unreachable(self):
        for index in (None, 99, -1):
            routing._ROUTE_CACHE.clear()
            with self.subTest(index=index), patch.object(routing.time, 'monotonic', return_value=100), \
                 patch.object(routing, 'urlopen', side_effect=self.no_segment(index)) as request:
                results = routing.enrich_candidate_travel_times(self.candidate_places(4), self.origin, 120)
            self.assertEqual(request.call_count, 1)
            self.assertTrue(all(p['travel_constraint']['status'] == 'batch_unresolved' for p in results))
            self.assertTrue(all(expires == 100 + routing.FAILURE_CACHE_TTL
                                for expires, _ in routing._ROUTE_CACHE.values()))

    def test_bad_origin_stops_all_uncached_batch_requests(self):
        with patch.object(routing, 'urlopen', side_effect=self.no_segment(0)) as request:
            results = routing.enrich_candidate_travel_times(self.candidate_places(120), self.origin, 120)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(all(p['travel_constraint']['status'] == 'no_origin_route' for p in results))
        self.assertTrue(all(p['travel_constraint']['within_limit'] is None for p in results))

    def test_repair_budget_is_shared_and_never_exceeds_eight_requests(self):
        with patch.object(routing, 'urlopen', side_effect=lambda *a, **k: (_ for _ in ()).throw(self.no_segment(1))) as request:
            results = routing.enrich_candidate_travel_times(self.candidate_places(160), self.origin, 120)
        self.assertEqual(request.call_count, 4 + routing.MAX_CANDIDATE_REPAIRS)
        self.assertEqual(sum(p['travel_constraint']['status'] == 'unreachable' for p in results), 8)
        self.assertTrue(all(p['travel_constraint']['within_limit'] is None for p in results))

    def test_service_failure_during_repair_stops_later_uncached_batches(self):
        with patch.object(routing, 'urlopen', side_effect=[self.no_segment(2), TimeoutError('offline')]) as request:
            results = routing.enrich_candidate_travel_times(self.candidate_places(120), self.origin, 120)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(results[1]['travel_constraint']['status'], 'unreachable')
        self.assertTrue(all(p['travel_constraint']['within_limit'] is None for p in results))

    def test_candidate_429_stops_network_and_respects_retry_after(self):
        error = HTTPError(routing.OSRM_TABLE_URL, 429, 'Too Many Requests', {'Retry-After': '120'}, None)
        with patch.object(routing.time, 'monotonic', return_value=100), \
                patch.object(routing, 'urlopen', side_effect=error) as request:
            results = routing.enrich_candidate_travel_times(self.candidate_places(120), self.origin, 120)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(routing._COOLDOWN_UNTIL, 220)
        self.assertTrue(all(p['travel_constraint']['status'] == 'busy' for p in results))

    def test_candidate_missing_origin_and_limit_make_no_calls(self):
        with patch.object(routing, 'urlopen') as request:
            for origin, limit in [(None, 120), (self.origin, None), (self.origin, -1),
                                  ({'lat': True, 'lon': 127}, 120)]:
                results = routing.enrich_candidate_travel_times(self.candidate_places(80), origin, limit)
                self.assertTrue(all(p['travel_constraint']['within_limit'] is None for p in results))
        request.assert_not_called()

    def test_candidate_requests_remain_rate_limited(self):
        with patch.object(routing, 'MIN_REQUEST_INTERVAL', 1.1), \
                patch.object(routing.time, 'monotonic', return_value=100), \
                patch.object(routing.time, 'sleep') as sleep, \
                patch.object(routing, 'urlopen', side_effect=self.response_for_request) as request:
            routing.enrich_candidate_travel_times(self.candidate_places(81), self.origin, 120)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(call.args == (1.1,) for call in sleep.call_args_list))

    def test_candidate_cache_bounded_and_expiring(self):
        with patch.object(routing, 'ROUTE_CACHE_MAX_ENTRIES', 3), \
                patch.object(routing.time, 'monotonic', return_value=100), \
                patch.object(routing, 'urlopen', side_effect=self.response_for_request):
            routing.enrich_candidate_travel_times(self.candidate_places(5), self.origin, 120)
        self.assertEqual(len(routing._ROUTE_CACHE), 3)
        with patch.object(routing.time, 'monotonic', return_value=100 + routing.CACHE_TTL + 1), \
                patch.object(routing, 'urlopen', side_effect=self.response_for_request) as request:
            routing.enrich_candidate_travel_times(self.candidate_places(1), self.origin, 120)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(routing._ROUTE_CACHE), 1)


if __name__ == '__main__':
    unittest.main()
