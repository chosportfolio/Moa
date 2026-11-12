from datetime import datetime, timedelta, timezone
import unittest
from host_position import VehiclePosition, LocationUnavailable


class VehiclePositionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, tzinfo=timezone.utc)
        self.clock = 1
        self.position = VehiclePosition(utc_now=lambda: self.now, monotonic=lambda: self.clock)
        self.fix = {'latitude':36.2, 'longitude':128.2, 'source':'vehicle',
                    'confidence':0.8, 'updated_at':self.now.isoformat()}

    def test_confidence_does_not_invent_distance_accuracy(self):
        result = self.position.update(self.fix)
        self.assertTrue(result['available'])
        self.assertIsNone(result['accuracy_m'])
        self.assertNotIn('latitude', result)
        self.assertEqual(self.position.for_search()['confidence'], 0.8)

    def test_permission_loss_stops_search_and_replayed_fix_cannot_restore_it(self):
        self.position.update(self.fix)
        self.position.update({'available':False, 'status':'permission_required'})
        self.assertFalse(self.position.update(self.fix)['available'])
        with self.assertRaises(LocationUnavailable): self.position.for_search()
        self.now += timedelta(seconds=1)
        self.clock += 1
        self.assertTrue(self.position.update({**self.fix,'updated_at':self.now.isoformat()})['available'])

    def test_expired_sdk_last_known_fix_is_not_a_current_position(self):
        with self.assertRaises(ValueError):
            self.position.update({**self.fix,'updated_at':(self.now-timedelta(minutes=2)).isoformat()})
        self.assertFalse(self.position.public()['available'])
        self.position.update(self.fix)
        self.clock += 121
        self.assertFalse(self.position.public()['available'])

    def test_invalid_sdk_payload_is_rejected_without_losing_valid_fix(self):
        self.position.update(self.fix)
        for change in ({'source':'WiFi'},{'latitude':float('nan')},{'confidence':float('inf')},
                       {'accuracy_m':-1},{'available':False,'status':'unknown'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.position.update({**self.fix,**change})
            self.assertTrue(self.position.public()['available'])


if __name__ == '__main__': unittest.main()
