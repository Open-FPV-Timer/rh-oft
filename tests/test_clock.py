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

    def test_a_drifting_timer_clock_is_followed(self):
        # A real Timer's clock ran 70 ppm off the server's: laps 8 s apart must still be right.
        rnd = random.Random(2)
        c = ClockAlign()
        drift = -70e-6

        def timer_us(local):
            return int((local * (1 + drift) + 500.0) * 1e6)

        worst = 0.0
        for k in range(120):
            local = 10.0 + k
            rtt = rnd.uniform(0.012, 0.016)
            skew = rnd.uniform(-0.0003, 0.0003)  # the reply is not exactly mid round trip
            c.add(local - rtt / 2 + skew, local + rtt / 2 + skew, timer_us(local))
            if k >= 30:
                lap = c.to_local(timer_us(local - 1.0)) - c.to_local(timer_us(local - 9.0))
                worst = max(worst, abs(lap - 8.0))
        self.assertAlmostEqual(c.rate, -drift, delta=20e-6)
        self.assertLess(worst, 0.0003, "a lap was wrong by %.3f ms" % (worst * 1e3))

    def test_one_lucky_fast_reply_does_not_stop_the_alignment(self):
        # Seen on a real Wi-Fi Timer: one 2 ms reply among 13 ms ones froze the clock fit.
        c = ClockAlign()
        drift = -70e-6
        for k in range(200):
            local = 10.0 + k
            rtt = 0.002 if k == 3 else 0.013
            c.add(local - rtt / 2, local + rtt / 2, int((local * (1 + drift) + 500.0) * 1e6))
        now_us = int((209.0 * (1 + drift) + 500.0) * 1e6)
        self.assertAlmostEqual(c.to_local(now_us), 209.0, delta=0.0002)

    def test_boot_id_change_resets(self):
        c = ClockAlign()
        c.add(0.0, 0.002, 1_000_000, boot_id="a")
        self.assertTrue(c.synced)
        c.add(10.0, 10.002, 5_000_000, boot_id="b")
        self.assertEqual(c.boot_id, "b")
        self.assertAlmostEqual(c.to_local(5_000_000), 10.001, places=6)
