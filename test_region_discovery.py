"""Named destination scopes reuse public data without becoming departures."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import region_discovery as regions
import trip_recommendation as trip
from poi_index import write_index


class RegionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent / 'tests')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root/'public.sqlite'
        self.cache = self.root/'places'
        self.cache.mkdir()
        regions._CENTERS.clear()
        self.env = patch.dict(os.environ, {'CABIN_POI_INDEX':str(self.db)})
        self.env.start()
        self.addCleanup(self.env.stop)
        elements = [
            self.place(1, 37, 126, amenity='cafe'),
            self.place(2, 37, 126.06, amenity='cafe'),
            self.place(3, 37, 126.10, natural='beach'),
            self.place(4, 37, 126.30, natural='beach'),
            self.place(5, 37.3, 126, amenity='cafe'),
        ]
        write_index(elements, self.db, source_data_at='2026-11-11T00:00:00Z',
                    coastlines=[{'id':10, 'points':[(36.99,126.10),(37.01,126.10)]},
                                {'id':11, 'points':[(36.99,126.30),(37.01,126.30)]}])

    def place(self, identifier, lat, lon, **tags):
        return {'type':'node', 'id':identifier, 'lat':lat, 'lon':lon,
                'tags':{'name':'시험 장소 '+str(identifier), **tags}}

    def geocode(self, params):
        return [{'lat':37.3 if params['q']=='지역나' else 37, 'lon':126}]

    def test_mixed_beach_and_cafe_preserve_different_legacy_radii(self):
        with patch.object(regions, 'nominatim_search', side_effect=self.geocode), \
             patch('recommend.get_json', side_effect=AssertionError('no remote POI request')):
            data = regions.discover_region_candidates('지역가',self.cache,
                    {'requested_topics':['해변','카페']})
        self.assertEqual({1,3}, {p['id'] for p in data['elements']})
        self.assertIn('4.5km',data['coverage'])
        self.assertIn('20km',data['coverage'])
        self.assertIn('행정구역 전체',data['search_note'])
        self.assertEqual('destination_region_local_index',data['discovery']['mode'])
        self.assertEqual({'lat':37.0,'lon':126.0},data['center'])

    def test_public_named_cache_avoids_geocoder_and_rejects_mismatched_region(self):
        path=self.cache/(hashlib.sha256('지역가'.encode()).hexdigest()+'.json')
        path.write_text(json.dumps({'region':'지역가','center':{'lat':37,'lon':126}}),encoding='utf-8')
        with patch.object(regions,'nominatim_search',side_effect=AssertionError('cached public center')):
            data=regions.discover_region_candidates('지역가',self.cache,{'requested_topics':['카페']})
        self.assertEqual([1],[p['id'] for p in data['elements']])
        regions._CENTERS.clear()
        path.write_text(json.dumps({'region':'다른지역','center':{'lat':0,'lon':0}}),encoding='utf-8')
        with patch.object(regions,'nominatim_search',side_effect=self.geocode) as geocode:
            data=regions.discover_region_candidates('지역가',self.cache,{'requested_topics':['카페']})
        geocode.assert_called_once()
        self.assertEqual([1],[p['id'] for p in data['elements']])

    def test_no_index_or_no_center_retains_legacy_fallback_signal(self):
        with patch.object(regions,'index_path',return_value=self.root/'absent.sqlite'), \
             patch.object(regions,'nominatim_search') as geocode:
            self.assertIsNone(regions.discover_region_candidates('지역가',self.cache,{'requested_topics':['카페']}))
        geocode.assert_not_called()
        with patch.object(regions,'nominatim_search',side_effect=OSError('offline')), \
             patch.object(regions,'query_candidates') as lookup:
            self.assertIsNone(regions.discover_region_candidates('지역가',self.cache,{'requested_topics':['카페']}))
        lookup.assert_not_called()

    def test_timed_recording_and_sample_named_regions_keep_original_departure(self):
        origin={'lat':37.4,'lon':127.1,'label':'가상 출발점'}
        seen=[]
        def routes(places, actual_origin, minutes):
            seen.append(actual_origin)
            result=copy.deepcopy(places)
            for place in result:
                place['travel_constraint']={'within_limit':True,'verified':True,
                    'duration_minutes':25,'max_minutes':minutes,'live_traffic':False}
            return result
        with patch.object(regions,'nominatim_search',side_effect=self.geocode), \
             patch('recommend.lookup_places',side_effect=AssertionError('no remote POI lookup')), \
             patch.object(trip,'enrich_candidate_travel_times',side_effect=routes):
            outputs=[]
            for demo,seat in [(False,'unknown'),(True,'driver')]:
                result=trip.recommend_trip([{'id':'spoken','seat':seat,'text':'카페 가자. 한 시간 이내로.'}],
                          ['지역가','지역나'],self.cache,origin,60,demo=demo)
                outputs.append({p['id'] for p in result['recommendations']})
                self.assertEqual('destination_regions',result['request_profile']['search_report']['discovery']['mode'])
        self.assertEqual([{'node/1','node/5'}]*2,outputs)
        self.assertEqual([origin,origin],seen)

    def test_untimed_alternative_regions_also_use_the_local_index(self):
        with patch.object(regions,'nominatim_search',side_effect=self.geocode), \
             patch('recommend.lookup_places',side_effect=AssertionError('no remote POI lookup')):
            result=trip.recommend_regions([{'id':'spoken','seat':'unknown','text':'카페 가자.'}],
                                          ['지역가','지역나'],self.cache)
        self.assertEqual({'node/1','node/5'},{p['id'] for p in result['recommendations']})
        self.assertIn('지역가 검색 기준점',result['coverage'])
        self.assertIn('지역나 검색 기준점',result['coverage'])


if __name__ == '__main__':
    unittest.main()
