"""Local public-index tests; never query the network or read user state."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import poi_index as index


class PoiIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent/'tests')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'pois.sqlite'
        self.origin = {'lat':37.4, 'lon':127.1}

    def poi(self, identifier, *, lat=37.41, lon=127.11, **tags):
        return {'type':'node', 'id':identifier, 'lat':lat, 'lon':lon,
                'tags':{'name':'테스트 장소 '+str(identifier), **tags}}

    def build(self, elements, coastlines=None):
        if coastlines is None:
            coastlines = [{'id':10000, 'points':[(37.2,127.11),(37.8,127.11)]}]
        return index.write_index(elements, self.path, source_data_at='2026-11-11T21:22:47+00:00',
                                 coastlines=coastlines)

    def test_public_tag_allowlist_and_all_groups_include_korean_only_names(self):
        specs = [{'amenity':'restaurant'}, {'amenity':'cafe'}, {'shop':'bakery'},
                 {'leisure':'park'}, {'tourism':'museum'}, {'natural':'beach'}]
        elements = [self.poi(i, **tags) for i, tags in enumerate(specs,1)]
        elements[0]['tags'].pop('name')
        elements[0]['tags'].update({'name:ko':'한글 식당', 'user':'contributor-name',
                                    'uid':'1234', 'email':'private@example.invalid'})
        metadata = self.build(elements)
        result = index.query_candidates(self.origin, 10000, index.GROUPS, db_path=self.path)
        self.assertEqual(6, metadata['place_count'])
        self.assertEqual(set(range(1,7)), {p['id'] for p in result['elements']})
        restaurant = next(p for p in result['elements'] if p['id']==1)
        self.assertEqual('한글 식당',restaurant['tags']['name:ko'])
        self.assertFalse({'user','uid','email'} & restaurant['tags'].keys())
        self.assertFalse(result['discovery']['relations_included'])
        self.assertEqual('2026-11-11T21:22:47+00:00', result['source_data_at'])

    def test_river_beach_is_excluded_using_geometry_not_name_or_longitude(self):
        # Both candidates have the same longitude and arbitrary names. Only
        # one lies beside the synthetic coast segment; names cannot decide it.
        coastal = self.poi(1, lat=37.41, lon=127.11, natural='beach')
        river = self.poi(2, lat=37.9, lon=127.11, natural='beach')
        self.build([coastal,river], coastlines=[{'id':80,'points':[(37.40,127.115),(37.42,127.115)]}])
        result = index.query_candidates(self.origin,100000,('beach',),db_path=self.path)
        self.assertEqual([1],[p['id'] for p in result['elements']])
        proof = result['elements'][0]
        self.assertLess(proof['coastal_distance_m'], 1000)
        self.assertEqual('OpenStreetMap natural=coastline',proof['coastal_basis'])
        self.assertNotIn('coastal_distance_m',proof['tags'])
        self.assertEqual(1,result['discovery']['coastal_filter']['excluded_inland_or_unverified_count'])

    def test_no_coast_geometry_cannot_claim_river_or_unknown_beach_is_sea(self):
        self.build([self.poi(1,natural='beach'),self.poi(2,amenity='cafe')],coastlines=[])
        result = index.query_candidates(self.origin,10000,('beach','cafe'),db_path=self.path)
        self.assertEqual([2],[p['id'] for p in result['elements']])
        self.assertFalse(result['discovery']['coastal_filter']['geometry_available'])
        self.assertEqual(1,result['discovery']['coastal_filter']['excluded_inland_or_unverified_count'])

    def test_distance_uses_segment_not_just_coast_vertices(self):
        self.build([self.poi(1,lat=37.4,lon=127.1,natural='beach')],
                   coastlines=[{'id':90,'points':[(37.2,127.11),(37.6,127.11)]}])
        result = index.query_candidates(self.origin,10000,('beach',),db_path=self.path)
        self.assertEqual(1,len(result['elements']))
        self.assertLess(result['elements'][0]['coastal_distance_m'], 1000)

    def test_beach_between_1500_and_2000_meters_from_coast_is_excluded(self):
        # Around 1.77km inland: the former 2km threshold was broad enough to
        # admit an actual inland beach. Keep both public entry points strict.
        self.build([self.poi(1,lat=37.4,lon=127.1,natural='beach')],
                   coastlines=[{'id':90,'points':[(37.2,127.12),(37.6,127.12)]}])
        result = index.query_candidates(self.origin,10000,('beach',),db_path=self.path)
        self.assertEqual([],result['elements'])
        self.assertEqual(1,result['discovery']['coastal_filter']['excluded_inland_or_unverified_count'])
        self.assertIsNone(index.coastal_proximity(37.4,127.1,db_path=self.path))

    def test_public_coastal_helper_matches_query_evidence_and_fails_closed(self):
        self.assertIsNone(index.coastal_proximity(37.4,127.1,db_path=self.path))
        self.build([self.poi(1,lat=37.4,lon=127.1,natural='beach')],
                   coastlines=[{'id':90,'points':[(37.2,127.11),(37.6,127.11)]}])
        evidence = index.coastal_proximity(37.4,127.1,db_path=self.path)
        result = index.query_candidates(self.origin,10000,('beach',),db_path=self.path)
        self.assertEqual(result['elements'][0]['coastal_distance_m'],evidence['distance_m'])
        self.assertEqual('OpenStreetMap natural=coastline',evidence['basis'])
        self.assertEqual(1500,evidence['max_distance_m'])
        self.assertEqual('2026-11-11T21:22:47+00:00',evidence['source_data_at'])
        self.assertIsNone(index.coastal_proximity(38.4,127.1,db_path=self.path))
        self.assertIsNone(index.coastal_proximity(True,127.1,db_path=self.path))

    def test_menu_matching_and_exact_radius_filter_after_sql_bbox(self):
        self.build([self.poi(1, amenity='restaurant', cuisine='pasta'),
                    self.poi(2, amenity='restaurant', cuisine='korean'),
                    self.poi(3, lat=37.48, lon=127.20, amenity='restaurant', cuisine='pasta'),
                    self.poi(4, lat=38.4, amenity='restaurant', cuisine='pasta')])
        result = index.query_candidates(self.origin, 10000, ('meal',), ('파스타',), db_path=self.path)
        self.assertEqual([1], [p['id'] for p in result['elements']])

    def test_missing_corrupt_or_wrong_version_index_returns_none(self):
        self.assertIsNone(index.query_candidates(self.origin, 10000, ('cafe',), db_path=self.path))
        self.path.write_text('not sqlite',encoding='utf-8')
        self.assertIsNone(index.query_candidates(self.origin, 10000, ('cafe',), db_path=self.path))
        self.build([self.poi(1, amenity='cafe')])
        with sqlite3.connect(self.path) as connection:
            connection.execute('UPDATE metadata SET value=? WHERE key=?', ('999','schema_version'))
        connection.close()
        self.assertIsNone(index.query_candidates(self.origin, 10000, ('cafe',), db_path=self.path))

    def test_query_does_not_modify_index_and_rebuild_uses_new_data(self):
        self.build([self.poi(1, amenity='cafe')])
        before = self.path.read_bytes()
        result = index.query_candidates(self.origin, 10000, ('cafe',), db_path=self.path)
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual([1], [p['id'] for p in result['elements']])
        self.build([self.poi(2, amenity='cafe')])
        result = index.query_candidates(self.origin, 10000, ('cafe',), db_path=self.path)
        self.assertEqual([2], [p['id'] for p in result['elements']])

    def test_failed_build_preserves_previous_database(self):
        self.build([self.poi(1, amenity='cafe')])
        before = self.path.read_bytes()
        def broken():
            yield self.poi(2, amenity='cafe')
            raise RuntimeError('fixture interrupted import')
        with self.assertRaises(RuntimeError):
            self.build(broken())
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual([],list(self.path.parent.glob('*.building-*')))

    def test_invalid_query_and_environment_path(self):
        self.build([self.poi(1, amenity='cafe')])
        for origin, radius, groups in [(None,1000,('cafe',)), (self.origin,True,('cafe',)),
                                      (self.origin,float('nan'),('cafe',)), (self.origin,1000,('unknown',))]:
            with self.subTest(origin=origin,radius=radius,groups=groups), self.assertRaises(ValueError):
                index.query_candidates(origin,radius,groups,db_path=self.path)
        with patch.dict('os.environ', {'CABIN_POI_INDEX':str(self.path)}):
            self.assertEqual(1,len(index.query_candidates(self.origin,10000,('cafe',))['elements']))

    def test_large_result_keeps_other_areas_before_capping(self):
        elements = [self.poi(i,lat=37.4+i*.000001,amenity='cafe') for i in range(1,800)]
        elements += [self.poi(900,lat=36.9,lon=126.6,amenity='cafe'),
                     self.poi(901,lat=38.4,lon=128.1,amenity='cafe')]
        self.build(elements)
        result = index.query_candidates(self.origin,260000,('cafe',),db_path=self.path)
        self.assertEqual(640,len(result['elements']))
        self.assertTrue({900,901} <= {p['id'] for p in result['elements']})

    @unittest.skipUnless(importlib.util.find_spec('osmium'), 'optional builder dependency')
    def test_osm_builder_copies_named_nodes_and_complete_way_centers(self):
        source = self.path.parent/'fixture.osm'
        source.write_text('''<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="fixture">
<node id="1" lat="37.4" lon="127.1" version="1" user="ignored" uid="1"><tag k="name:ko" v="테스트 카페"/><tag k="amenity" v="cafe"/></node>
<node id="2" lat="37.42" lon="127.12" version="1"/>
<node id="3" lat="37.44" lon="127.14" version="1"/>
<node id="6" lat="37.43" lon="127.115" version="1"><tag k="name:ko" v="테스트 해변"/><tag k="natural" v="beach"/></node>
<node id="21" lat="37.40" lon="127.12" version="1"/>
<node id="22" lat="37.45" lon="127.12" version="1"/>
<way id="4" version="1"><nd ref="2"/><nd ref="3"/><tag k="name" v="테스트 공원"/><tag k="leisure" v="park"/></way>
<way id="50" version="1"><nd ref="21"/><nd ref="22"/><tag k="natural" v="coastline"/></way>
<relation id="5" version="1"><member type="way" ref="4" role="outer"/><tag k="name" v="생략한 복합 공원"/><tag k="leisure" v="park"/></relation>
</osm>''',encoding='utf-8')
        metadata = index.build_index(source,self.path,source_url='https://example.invalid/public-fixture.osm')
        self.assertEqual(3,metadata['place_count'])
        self.assertEqual(1,metadata['coastline_way_count'])
        self.assertIsNone(metadata['source_data_at'])
        result = index.query_candidates(self.origin,10000,('cafe','park'),db_path=self.path)
        way = next(p for p in result['elements'] if p['type']=='way')
        self.assertAlmostEqual(37.43,way['lat'])
        self.assertAlmostEqual(127.13,way['lon'])
        beaches = index.query_candidates(self.origin,10000,('beach',),db_path=self.path)['elements']
        self.assertEqual([6],[p['id'] for p in beaches])
        self.assertLess(beaches[0]['coastal_distance_m'],1000)


if __name__ == '__main__':
    unittest.main()
