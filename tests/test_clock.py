import random
import unittest

from rh_oft.clock import ClockAlign


class ClockAlignTest(unittest.TestCase):
    def test_unsynced_until_first_observation(self):
        c = ClockAlign()
        self.assertFalse(c.synced)
        with self.assertRaises(ValueError):
            c.to_local(0)

    def test_exact_exchanges_give_exact_offset(self):
        c = ClockAlign()
        offset = -1234.5678
        for k in range(10):
            local = 100.0 + k
            timer_us = int((local + offset) * 1e6)
            c.add(local - 0.0005, local + 0.0005, timer_us)
        self.assertTrue(c.synced)
        self.assertAlmostEqual(c.to_local(int((150.0 + offset) * 1e6)), 150.0, places=5)
        self.assertAlmostEqual(c.error_estimate_s(), 0.0005, places=6)

    def test_slow_round_trips_are_ignored(self):
        rnd = random.Random(1)
        c = ClockAlign()
        offset = 42.0
        used = 0
        for k in range(60):
            local = 10.0 + k
            # Fast exchanges: 1-3 ms, symmetric. Every 5th: a 200 ms stall after the reply.
            rtt = rnd.uniform(0.001, 0.003)
            t_send = local - rtt / 2
            t_recv = local + rtt / 2
            if k % 5 == 4:
                t_recv += 0.2
            timer_us = int((local + offset) * 1e6)
            if c.add(t_send, t_recv, timer_us):
                used += 1
        self.assertLess(used, 60)
        self.assertGreaterEqual(used, 48)
        err = c.to_local(int((100.0 + offset) * 1e6)) - 100.0
        self.assertLess(abs(err), 0.001, "alignment error %.4f s" % err)

    def test_boot_id_change_resets(self):
        c = ClockAlign()
        c.add(0.0, 0.002, 1_000_000, boot_id="a")
        self.assertTrue(c.synced)
        c.add(10.0, 10.002, 5_000_000, boot_id="b")
        self.assertEqual(c.boot_id, "b")
        self.assertAlmostEqual(c.to_local(5_000_000), 10.001, places=6)
