import copy
from datetime import datetime, timedelta, timezone
import unittest
from host_position import HostPosition, LocationUnavailable


class HostPositionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,11,10,tzinfo=timezone.utc)
        self.clock = 10
        self.position = HostPosition(utc_now=lambda:self.now,monotonic=lambda:self.clock)
        self.fix = {'latitude':37.0,'longitude':128.0,'accuracy_m':135,'source':'WiFi','updated_at':self.now.isoformat()}

    def test_no_position_is_not_a_default_city(self):
        self.assertFalse(self.position.public()['available'])
        with self.assertRaises(LocationUnavailable):self.position.for_search()

    def test_coordinates_only_available_to_search_not_public_state(self):
        result=self.position.update(self.fix)
        self.assertTrue(result['available'])
        self.assertEqual(result['accuracy_m'],135)
        self.assertNotIn('latitude',result); self.assertNotIn('longitude',result)
        search=self.position.for_search()
        self.assertEqual(search['latitude'],37.0)
        search['latitude']=0
        self.assertEqual(self.position.for_search()['latitude'],37.0)

    def test_invalid_fixes_do_not_replace_valid_position(self):
        self.position.update(self.fix)
        for key,value in [('latitude',True),('latitude',91),('longitude',float('nan')),
                          ('longitude',181),('accuracy_m',0),('accuracy_m',1001),
                          ('source','IPAddress'),('source','Default'),
                          ('updated_at',(self.now-timedelta(seconds=31)).isoformat()),
                          ('updated_at',(self.now+timedelta(seconds=6)).isoformat()),
                          ('updated_at','2026-11-10T00:00:00')]:
            with self.subTest(key=key,value=value):
                bad=copy.deepcopy(self.fix);bad[key]=value
                with self.assertRaises(ValueError):self.position.update(bad)
                self.assertEqual(self.position.for_search()['latitude'],37.0)

    def test_location_expires_even_when_wall_clock_changes(self):
        self.position.update(self.fix)
        self.now-=timedelta(hours=2)
        self.clock+=121
        self.assertFalse(self.position.public()['available'])
        with self.assertRaises(LocationUnavailable):self.position.for_search()

    def test_out_of_order_fix_cannot_move_position_backwards(self):
        self.position.update(self.fix)
        older={**self.fix,'updated_at':(self.now-timedelta(seconds=5)).isoformat(),'latitude':38.0}
        with self.assertRaises(ValueError):self.position.update(older)
        self.assertEqual(self.position.for_search()['latitude'],37.0)

    def test_duplicate_timestamp_preserves_original_measurement_and_age(self):
        self.position.update(self.fix)
        self.now+=timedelta(seconds=20)
        self.clock+=20
        changed={**self.fix,'latitude':38.0,'accuracy_m':10,'source':'Satellite'}
        result=self.position.update(changed)
        self.assertEqual(result['age_seconds'],20)
        self.assertEqual(result['accuracy_m'],135)
        self.assertEqual(result['source'],'WiFi')
        self.assertEqual(self.position.for_search()['latitude'],37.0)
        self.clock+=101
        self.assertFalse(self.position.public()['available'])

    def test_replayed_timestamp_after_clock_correction_cannot_revive_expired_fix(self):
        self.position.update(self.fix)
        self.clock+=121
        self.assertFalse(self.position.public()['available'])
        # Wall time returns to the capture time; elapsed monotonic time does not.
        result=self.position.update(self.fix)
        self.assertFalse(result['available'])
        self.assertEqual(result['age_seconds'],121)
        with self.assertRaises(LocationUnavailable):self.position.for_search()

    def test_measurement_age_counts_toward_expiry_and_fresh_fix_recovers(self):
        self.position.update({**self.fix,'updated_at':(self.now-timedelta(seconds=20)).isoformat()})
        self.clock+=101
        self.assertFalse(self.position.public()['available'])
        self.now+=timedelta(seconds=101)
        self.position.update({**self.fix,'latitude':38.0,'updated_at':self.now.isoformat()})
        self.assertTrue(self.position.public()['available'])
        self.assertEqual(self.position.for_search()['latitude'],38.0)


if __name__=='__main__':unittest.main()
