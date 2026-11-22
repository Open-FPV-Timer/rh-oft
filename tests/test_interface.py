import unittest
from time import monotonic

from fake_timer import FakeTimer
from rh_oft.interface import LevelMap, OftInterface, ddb_to_rh, rh_to_ddb


class Harness:
    """An OftInterface wired to a FakeTimer, with the callbacks RotorHazard would attach."""

    def __init__(self, seats=1, **fake_kwargs):
        self.fake = FakeTimer(**fake_kwargs)
        url = self.fake.start()
        self.iface = OftInterface(url, seats=seats, sleep=lambda s: None)
        self.laps = []
        self.crossing_events = []
        self.iface.pass_record_callback = lambda node, ts, source, peak=None: self.laps.append((node.index, ts, source, peak))
        self.iface.node_crossing_callback = lambda node: self.crossing_events.append((node.index, node.crossing_flag))

    def run(self, polls=3):
        for _ in range(polls):
            self.iface.update()
        # Force the periodic jobs to run on the next poll too.
        self.iface._next_time = 0.0
        self.iface._next_status = 0.0

    def close(self):
        self.fake.stop()


class UnitsTest(unittest.TestCase):
    def test_mapping_round_trips_and_lands_in_rh_range(self):
        self.assertEqual(ddb_to_rh(-920), 35)
        self.assertEqual(ddb_to_rh(-600), 75)
        self.assertEqual(ddb_to_rh(-9999), 1)
        self.assertEqual(ddb_to_rh(9999), 255)
        for ddb in (-920, -770, -600, -200):
            self.assertLessEqual(abs(rh_to_ddb(ddb_to_rh(ddb)) - ddb), 4)


    def test_level_map_follows_the_node_unit(self):
        db = LevelMap.from_info({"unit": "dB", "scale": 0.1})
        self.assertEqual(db.to_rh(-600), ddb_to_rh(-600))
        self.assertEqual(db.from_rh(75), -600)
        self.assertEqual(LevelMap.from_info(None), db, "a Node with no level reports tenths of a dB")
        mv = LevelMap.from_info({"unit": "mV", "scale": 1})
        self.assertEqual(mv.to_rh(850), 131)  # 850 mV / 6.5
        self.assertEqual(mv.from_rh(131), 852)
        self.assertEqual(mv.to_rh(5000), 255)
        other = LevelMap.from_info({"unit": "counts", "scale": 1})
        self.assertFalse(other.known)
        self.assertEqual(other.from_rh(other.to_rh(90)), 90)


class InterfaceTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_connects_and_aligns_clock(self):
        self.h.run(2)
        self.assertTrue(self.h.iface.connected)
        self.assertTrue(self.h.iface.clock.synced)
        self.assertEqual(self.h.iface.timer_info["id"], "oft-fake")
        self.assertLess(self.h.iface.clock.error_estimate_s(), 0.05)

    def test_crossing_becomes_a_lap_at_the_timer_time_not_arrival_time(self):
        self.h.run(2)
        # A pass that happened 0.8 s ago, reported now.
        true_local = monotonic() - 0.8
        self.h.fake.add_crossing(true_local, peak=-610)
        self.h.iface.nodes[0].frequency = 5800
        self.h.run(1)
        self.assertEqual(len(self.h.laps), 1)
        idx, ts, source, peak = self.h.laps[0]
        self.assertEqual(idx, 0)
        self.assertEqual(source, 0)  # LAP_SOURCE_REALTIME
        self.assertLess(abs(ts - true_local), 0.01, "lap time off by %.4f s" % (ts - true_local))
        self.assertEqual(peak, ddb_to_rh(-610))
        self.assertEqual(self.h.iface.nodes[0].node_lap_id, 1)

    def test_no_duplicate_laps_across_polls(self):
        self.h.run(2)
        self.h.iface.nodes[0].frequency = 5800
        self.h.fake.add_crossing(monotonic() - 1.0)
        self.h.fake.add_crossing(monotonic() - 0.5)
        self.h.run(5)
        self.assertEqual(len(self.h.laps), 2)
        self.assertEqual(self.h.iface.last_seq, 2)
        self.assertTrue(all(("GET", "/oft/v1/crossings") == r for r in self.h.fake.requests if r[1].endswith("crossings")))

    def test_disabled_seat_drops_laps(self):
        self.h.run(2)
        self.h.iface.nodes[0].frequency = 0
        self.h.fake.add_crossing(monotonic() - 0.2)
        self.h.run(1)
        self.assertEqual(self.h.laps, [])
        self.assertEqual(self.h.iface.last_seq, 1, "the crossing is still consumed")

    def test_timer_reboot_resets_sequence_and_clock(self):
        self.h.run(2)
        self.h.iface.nodes[0].frequency = 5800
        self.h.fake.add_crossing(monotonic() - 0.3)
        self.h.run(1)
        self.assertEqual(len(self.h.laps), 1)
        self.h.fake.reboot("cafef00d", new_offset_s=-5.0)
        self.h.run(3)
        self.assertEqual(self.h.iface.boot_id, "cafef00d")
        self.assertEqual(self.h.iface.last_seq, 0)
        true_local = monotonic() - 0.1
        self.h.fake.add_crossing(true_local)
        self.h.run(2)
        self.assertEqual(len(self.h.laps), 2)
        self.assertLess(abs(self.h.laps[1][1] - true_local), 0.01)

    def test_frequency_and_thresholds_reach_the_timer(self):
        self.h.run(2)
        self.h.iface.set_frequency(0, 5740)
        self.h.iface.set_enter_at_level(0, ddb_to_rh(-760))
        self.h.iface.set_exit_at_level(0, ddb_to_rh(-800))
        self.h.run(1)
        self.assertTrue(self.h.fake.puts, "no config PUT")
        cfg = self.h.fake.puts[-1]
        seat = cfg["seats"][0]
        self.assertEqual((seat["seat"], seat["freq_mhz"], seat["enabled"]), (0, 5740, True))
        self.assertEqual(seat["threshold"]["mode"], "manual")
        self.assertLessEqual(abs(seat["threshold"]["enter"] - (-760)), 4)
        self.assertLessEqual(abs(seat["threshold"]["exit"] - (-800)), 4)
        self.assertEqual(cfg["threshold"]["mode"], "auto", "the Timer's default threshold is left alone")
        self.assertEqual(cfg["min_lap_ms"], 2000, "settings the plugin does not own are kept")
        # Disabling the seat is pushed too.
        self.h.iface.set_frequency(0, 0)
        self.h.run(1)
        self.assertFalse(self.h.fake.puts[-1]["seats"][0]["enabled"])

    def test_status_updates_rssi_and_crossing_flag(self):
        self.h.run(2)
        self.h.fake.status_seats[0]["rssi"] = -600
        self.h.fake.status_seats[0]["crossing"] = True
        self.h.run(1)
        node = self.h.iface.nodes[0]
        self.assertEqual(node.current_rssi, ddb_to_rh(-600))
        self.assertTrue(node.crossing_flag)
        self.assertTrue(node.pass_crossing_flag)
        self.assertEqual(self.h.crossing_events[-1], (0, True))
        self.assertTrue(node.history_values)
        self.h.fake.status_seats[0]["crossing"] = False
        self.h.run(1)
        self.assertEqual(self.h.crossing_events[-1], (0, False))

    def test_survives_the_timer_being_unplugged(self):
        self.h.run(2)
        self.assertTrue(self.h.iface.connected)
        self.h.fake.fail = True
        self.h.run(2)
        self.assertFalse(self.h.iface.connected)
        self.assertIsNotNone(self.h.iface.ready_failure_msg)
        self.assertTrue(self.h.iface.ready, "RotorHazard must keep running without the gate")
        self.h.fake.fail = False
        self.h.iface.nodes[0].frequency = 5800
        self.h.run(3)
        self.assertTrue(self.h.iface.connected)
        self.h.fake.add_crossing(monotonic() - 0.2)
        self.h.run(1)
        self.assertEqual(len(self.h.laps), 1)
        # Reconnect re-sent RotorHazard's frequencies to the Timer.
        self.assertTrue(any(p["seats"][0]["freq_mhz"] == 5800 for p in self.h.fake.puts))

    def test_two_seats_map_to_two_nodes(self):
        self.h.close()
        self.h = Harness(seats=2)
        self.h.fake.status_seats.append({"seat": 1, "rssi": -900, "floor": -920, "enter": -770, "exit": -820, "crossing": False, "rate_hz": 500})
        self.h.run(2)
        for n in self.h.iface.nodes:
            self.h.iface.set_frequency(n.index, 5800 + 40 * n.index)
        self.h.fake.add_crossing(monotonic() - 0.4, seat=1)
        self.h.fake.add_crossing(monotonic() - 0.2, seat=0)
        self.h.run(1)
        self.assertEqual([lap[0] for lap in self.h.laps], [1, 0])
        self.h.run(1)
        self.assertEqual(self.h.fake.puts[-1]["seats"], [
            {"seat": 0, "freq_mhz": 5800, "enabled": True},
            {"seat": 1, "freq_mhz": 5840, "enabled": True},
        ])


    def test_each_seat_gets_its_own_thresholds(self):
        self.h.close()
        self.h = Harness(seats=2)
        self.h.run(2)
        self.h.iface.set_frequency(0, 5658)
        self.h.iface.set_frequency(1, 5732)
        self.h.iface.set_enter_at_level(1, 80)
        self.h.iface.set_exit_at_level(1, 70)
        self.h.run(1)
        seats = self.h.fake.puts[-1]["seats"]
        self.assertNotIn("threshold", seats[0], "RotorHazard has not set seat 0: it keeps the Timer default")
        self.assertEqual(seats[1]["threshold"], {"mode": "manual", "enter": rh_to_ddb(80), "exit": rh_to_ddb(70)})

    def test_timer_without_seat_thresholds_gets_one_threshold(self):
        self.h.fake.seat_thresholds = False
        self.h.run(2)
        self.h.iface.set_frequency(0, 5740)
        self.h.iface.set_enter_at_level(0, 80)
        self.h.iface.set_exit_at_level(0, 70)
        self.h.run(1)
        cfg = self.h.fake.puts[-1]
        self.assertNotIn("threshold", cfg["seats"][0], "must not send seat thresholds unless announced")
        self.assertEqual(cfg["threshold"], {"mode": "manual", "enter": rh_to_ddb(80), "exit": rh_to_ddb(70)})

    def test_seat_numbers_ignore_rotorhazards_node_numbering(self):
        # RotorHazard renumbers node.index across all interfaces: with 8 nodes of its own,
        # ours becomes node 8, but it is still seat 0 on the Timer.
        self.h.run(2)
        self.h.iface.nodes[0].index = 8
        self.h.iface.set_frequency(0, 5740)
        self.h.iface.set_enter_at_level(0, 80)
        self.h.run(1)
        self.assertEqual(self.h.fake.puts[-1]["seats"][0]["seat"], 0)
        self.h.fake.add_crossing(monotonic() - 0.2, seat=0)
        self.h.run(1)
        self.assertEqual([lap[0] for lap in self.h.laps], [8])

    def test_millivolt_node_levels(self):
        # An RX5808 Node reports millivolts (spec v0.8), not tenths of a dB.
        self.h.fake.nodes = [{"id": "f1f1", "chip": "stm32f1", "level": {"unit": "mV", "scale": 1}}]
        self.h.fake.status_seats[0].update(node="f1f1", rssi=390, floor=380, enter=850, exit=810)
        self.h.run(2)
        node = self.h.iface.nodes[0]
        self.assertEqual(node.current_rssi, 60)  # 390 mV / 6.5
        self.h.iface.set_frequency(0, 5800)
        self.h.iface.set_enter_at_level(0, 131)
        self.h.iface.set_exit_at_level(0, 125)
        self.h.fake.add_crossing(monotonic() - 0.2, peak=900)
        self.h.run(1)
        self.assertEqual(self.h.fake.puts[-1]["seats"][0]["threshold"], {"mode": "manual", "enter": 852, "exit": 812})
        self.assertEqual(self.h.laps[0][3], 138)  # 900 mV / 6.5

    def test_node_that_joins_later_gets_its_thresholds_resent_in_its_unit(self):
        # The Timer is up before its Node: no Nodes in /info, so levels default to dB.
        self.h.fake.nodes = []
        self.h.fake.status_seats[0]["node"] = None
        self.h.run(2)
        self.h.iface.set_frequency(0, 5800)
        self.h.iface.set_enter_at_level(0, 131)
        self.h.iface.set_exit_at_level(0, 125)
        self.h.run(1)
        self.assertEqual(self.h.fake.puts[-1]["seats"][0]["threshold"]["enter"], rh_to_ddb(131))
        self.h.fake.nodes = [{"id": "f1f1", "level": {"unit": "mV", "scale": 1}}]
        self.h.fake.status_seats[0].update(node="f1f1", rssi=390)
        self.h.run(2)
        self.assertEqual(self.h.fake.puts[-1]["seats"][0]["threshold"], {"mode": "manual", "enter": 852, "exit": 812})
        self.assertEqual(self.h.iface.nodes[0].current_rssi, 60)

    def test_methods_rotorhazard_calls_on_every_interface(self):
        self.h.iface.close_fwupd_serial_port()
        self.h.iface.set_intf_error_report_percent_limit(1.0)
        self.assertEqual(self.h.iface.get_intf_total_error_count(), 0)


class PluginInitTest(unittest.TestCase):
    def test_initialize_adds_interface_and_reads_options(self):
        import rh_oft

        class Db:
            def option(self, name, default=None):
                return {"rh_oft_url": "http://10.0.0.9:7270", "rh_oft_seats": "2"}.get(name, default)

        class Ifaces:
            added = []

            def add(self, iface):
                self.added.append(iface)

        class UI:
            def register_panel(self, *a, **k):
                pass

        class Fields:
            def register_option(self, *a, **k):
                pass

        class Api:
            db = Db()
            interface = Ifaces()
            ui = UI()
            fields = Fields()

        rh_oft.initialize(Api())
        self.assertEqual(len(Api.interface.added), 1)
        iface = Api.interface.added[0]
        self.assertEqual(iface.url, "http://10.0.0.9:7270")
        self.assertEqual(len(iface.nodes), 2)

    def test_seat_count_comes_from_the_timer(self):
        import rh_oft

        fake = FakeTimer()
        fake.config["seats"] = [{"seat": n, "freq_mhz": 5658 + 37 * n, "enabled": n < 2} for n in range(3)]
        url = fake.start()
        try:
            self.assertEqual(rh_oft.timer_seat_count(url), 3, "disabled seats count too")
        finally:
            fake.stop()
        self.assertIsNone(rh_oft.timer_seat_count("http://127.0.0.1:9"), "an unreachable Timer")


if __name__ == "__main__":
    unittest.main()
