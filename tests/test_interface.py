import unittest
from time import monotonic, sleep

from fake_timer import FakeTimer
from rh_oft.interface import LevelMap, OftInterface, claim_name, ddb_to_rh, rh_to_ddb


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

    def test_another_timer_at_the_address_is_refused(self):
        # The address we remembered now belongs to a different Timer (DHCP gave it away).
        self.h.iface.timer_id = "oft-mine"
        self.h.iface.nodes[0].frequency = 5800
        self.h.fake.add_crossing(monotonic() - 0.2)
        self.h.run(2)
        self.assertFalse(self.h.iface.connected)
        self.assertIn("oft-fake, not oft-mine", self.h.iface.ready_failure_msg)
        self.assertEqual(self.h.laps, [], "another Timer's laps are not ours")

    def test_timer_is_found_again_by_id_at_a_new_address(self):
        self.h.run(2)
        self.assertEqual(self.h.iface.timer_id, "oft-fake", "the id is learned on first contact")
        old_url = self.h.iface.url
        # The Timer comes back on another address: same id, new port here.
        self.h.fake.stop()
        moved_to = FakeTimer()
        new_url = moved_to.start()
        asked, told = [], []
        self.h.iface.locate = lambda timer_id: asked.append(timer_id) or new_url
        self.h.iface.on_moved = lambda timer_id, url: told.append((timer_id, url))
        try:
            self.h.run(1)
            self.assertEqual(asked, ["oft-fake"])
            self.assertEqual(told, [("oft-fake", new_url)])
            self.assertNotEqual(self.h.iface.url, old_url)
            self.h.iface.nodes[0].frequency = 5800
            self.h.run(2)
            self.assertTrue(self.h.iface.connected)
            moved_to.add_crossing(monotonic() - 0.2)
            self.h.run(1)
            self.assertEqual(len(self.h.laps), 1)
        finally:
            moved_to.stop()

    def test_looking_a_lost_timer_up_is_rate_limited(self):
        self.h.run(2)
        asked = []
        self.h.iface.locate = lambda timer_id: asked.append(timer_id)
        self.h.fake.fail = True
        self.h.run(5)
        self.assertEqual(len(asked), 1)

    def test_methods_rotorhazard_calls_on_every_interface(self):
        self.h.iface.close_fwupd_serial_port()
        self.h.iface.set_intf_error_report_percent_limit(1.0)
        self.assertEqual(self.h.iface.get_intf_total_error_count(), 0)


class ControlTest(unittest.TestCase):
    """Spec 6.6: one client at a time may change a Timer."""

    def setUp(self):
        self.h = Harness()
        self.h.fake.control = True
        self.told = []
        self.h.iface.on_control = lambda iface, name: self.told.append(name)

    def tearDown(self):
        self.h.iface.stop()
        self.h.close()

    def until(self, done, what, seconds=3.0):
        """Poll like RotorHazard would until ``done()``; the stream answers from another thread."""
        end = monotonic() + seconds
        while monotonic() < end:
            self.h.iface._next_write = 0.0
            self.h.run(1)
            if done():
                return
            sleep(0.01)
        self.fail("never happened: " + what)

    def tune(self, freq):
        self.h.iface.set_frequency(0, freq)

    def written(self):
        return [put["seats"][0]["freq_mhz"] for put in self.h.fake.puts]

    def test_claims_on_its_stream_and_writes_with_the_key(self):
        self.until(lambda: self.h.iface.client.control_key, "RotorHazard holds control")
        self.assertEqual(self.h.fake.stream_messages, [{"type": "control", "claim": claim_name()}])
        self.assertEqual(self.h.iface.client.control_key, self.h.fake.key)
        self.tune(5732)
        self.until(lambda: 5732 in self.written(), "the frequency is written")
        self.assertEqual(self.h.fake.refused, [])
        self.assertIsNone(self.h.iface.controlled_by)

    def test_a_timer_without_control_gets_no_stream(self):
        self.h.fake.control = False
        self.tune(5732)
        self.h.run(3)
        self.assertIsNone(self.h.iface.stream)
        self.assertEqual(self.written()[-1], 5732)

    def test_waits_while_another_client_holds_control_then_sends_everything(self):
        page = self.h.fake.add_claim("Timer page")
        self.tune(5732)
        self.until(lambda: self.h.iface._control_state == "other", "RotorHazard sees it must wait")
        self.h.run(5)
        self.assertEqual(self.h.fake.puts, [], "nothing may be written while another client holds control")
        self.assertEqual(self.h.iface.controlled_by, "Timer page")
        self.assertEqual(self.told, ["Timer page"])
        self.assertFalse(any("takeover" in m for m in self.h.fake.stream_messages))
        # The page changes the Timer, then closes: control passes to RotorHazard's claim.
        self.h.fake.config["seats"][0]["freq_mhz"] = 5917
        self.h.fake.end_claim(page)
        self.until(lambda: self.h.fake.config["seats"][0]["freq_mhz"] == 5732, "RotorHazard's settings are sent when control comes")
        self.assertEqual(self.told, ["Timer page", None])
        self.assertIsNone(self.h.iface.controlled_by)

    def test_control_taken_away_and_given_back(self):
        self.tune(5732)
        self.until(lambda: 5732 in self.written(), "the frequency is written")
        page = self.h.fake.add_claim("Timer page")
        self.h.fake.grant(page)  # a person picks the page as the writer, on the Timer's page
        self.until(lambda: self.h.iface.controlled_by == "Timer page", "RotorHazard sees it lost control")
        self.assertIsNone(self.h.iface.client.control_key)
        self.tune(5769)
        self.h.run(5)
        self.assertNotIn(5769, self.written())
        self.h.fake.config["seats"][0]["freq_mhz"] = 5917  # the page's own change
        self.h.fake.grant(self.h.fake.claim_named(claim_name()))
        self.until(lambda: self.h.fake.config["seats"][0]["freq_mhz"] == 5769, "the pending frequency is sent")
        self.assertEqual(self.h.fake.refused, [])
        self.h.fake.end_claim(page)

    def test_writes_without_a_key_when_nobody_holds_control(self):
        self.until(lambda: self.h.iface.client.control_key, "RotorHazard holds control")
        self.h.fake.grant(None)
        self.until(lambda: self.h.iface._control_state == "free", "RotorHazard sees nobody holds control")
        self.assertIsNone(self.h.iface.client.control_key)
        self.tune(5806)
        self.until(lambda: 5806 in self.written(), "the frequency is written")

    def test_claims_again_when_the_stream_drops(self):
        self.until(lambda: self.h.iface.client.control_key, "RotorHazard holds control")
        first = self.h.fake.key
        self.h.fake.drop_streams()
        self.h.iface._next_stream = 0.0
        self.until(lambda: self.h.iface.stream is None, "the drop is noticed")
        self.h.iface._next_stream = 0.0
        self.until(lambda: self.h.iface.client.control_key not in (None, first), "RotorHazard holds control again")
        self.assertEqual([m["claim"] for m in self.h.fake.stream_messages], [claim_name()] * 2, "one claim per stream")
        self.tune(5843)
        self.until(lambda: 5843 in self.written(), "the frequency is written with the new key")

    def test_a_short_drop_keeps_control_by_showing_the_key_again(self):
        self.h.fake.grace = True
        self.until(lambda: self.h.iface.client.control_key, "RotorHazard holds control")
        key, claim = self.h.fake.key, self.h.fake.holder
        self.h.fake.add_claim("Timer page")  # waiting: it would get control if ours ended
        self.h.fake.drop_streams()
        self.h.iface._next_stream = 1e12  # stay away for now
        self.until(lambda: self.h.iface.stream is None, "the drop is noticed")
        self.tune(5880)
        self.until(lambda: 5880 in self.written(), "the key still works for writes while away")
        self.h.iface._next_stream = 0.0
        self.until(lambda: self.h.iface._control_state == "hold", "RotorHazard is back on a new stream")
        self.assertEqual(self.h.fake.stream_messages[-1], {"type": "control", "claim": claim_name(), "key": key})
        self.assertEqual((self.h.fake.key, self.h.fake.holder), (key, claim), "the same claim, still holding control")
        self.assertEqual(self.told, [])
        self.assertEqual(self.h.fake.refused, [])

    def test_claim_name_fits_the_spec(self):
        self.assertEqual(claim_name("pit-box"), "RotorHazard pit-box")
        self.assertEqual(claim_name('a"b\\c\u00e9'), "RotorHazard abc")
        self.assertEqual(len(claim_name("x" * 60)), 32)

    def test_a_refused_write_means_wait_when_there_is_no_stream(self):
        import rh_oft.interface as module

        class NoStream:
            def __init__(self, *a, **k):
                pass

            def connect(self):
                raise module.StreamError("no stream here")

        real, module.ControlStream = module.ControlStream, NoStream
        try:
            page = self.h.fake.add_claim("Timer page")
            self.tune(5732)
            self.h.run(3)
            self.assertEqual(self.h.fake.puts, [])
            self.assertGreaterEqual(len(self.h.fake.refused), 1)
            self.assertEqual(self.h.iface.controlled_by, "Timer page")
            self.assertTrue(self.h.iface.connected, "a refused write is not a lost Timer")
            refused = len(self.h.fake.refused)
            self.h.run(5)
            self.assertEqual(len(self.h.fake.refused), refused, "refused writes are not repeated every poll")
            self.h.fake.end_claim(page)
            self.until(lambda: 5732 in self.written(), "the frequency is written once the Timer is free")
            self.assertIsNone(self.h.iface.controlled_by)
        finally:
            module.ControlStream = real


if __name__ == "__main__":
    unittest.main()
