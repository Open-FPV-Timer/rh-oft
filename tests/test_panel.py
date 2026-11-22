import os
import unittest

import rh_oft
from fake_timer import FakeTimer
from rh_oft.mdns import FoundTimer
from rh_oft.panel import OPT_ADDRESS, OPT_FOUND, OPT_REMOVE, load_timers


class FakeRhapi:
    """The parts of RHAPI the plugin uses, recording what it is told."""

    def __init__(self, timers=None):
        api = self
        self.options = {}
        self.config_file = {"RH_OFT": {"TIMERS": timers}} if timers is not None else {}
        self.interfaces, self.said, self.buttons, self.markdown = [], [], {}, {}
        self.broadcasts = 0

        class Db:
            def option(self, name, default=None):
                return api.options.get(name, default)

            def option_set(self, name, value):
                api.options[name] = value

        class Config:
            def register_section(self, section):
                api.config_file.setdefault(section, {})

            def get(self, section, name):
                return api.config_file.get(section, {}).get(name, False)

            def set(self, section, name, value):
                api.config_file[section][name] = value
                return True

        class Interface:
            def add(self, iface):
                api.interfaces.append(iface)

        class Ui:
            def register_panel(self, *a, **k):
                pass

            def register_markdown(self, panel, name, text):
                api.markdown[name] = text

            def register_quickbutton(self, panel, name, label, fn, args=None):
                api.buttons[name] = fn

            def broadcast_ui(self, page):
                api.broadcasts += 1

            def message_notify(self, message):
                api.said.append(message)

        class Fields:
            by_name = {}

            def register_option(self, field, panel):
                self.by_name[field.name] = field

        self.db, self.interface, self.ui, self.fields, self.config = Db(), Interface(), Ui(), Fields(), Config()
        self.fields.by_name = {}

    @property
    def text(self):
        return self.markdown["rh_oft_list"]


def found(timer_id, url, seats="2"):
    host, port = url.replace("http://", "").split(":")
    return FoundTimer(timer_id + "._oft._tcp.local.", host, int(port), {"id": timer_id, "seats": seats})


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTimer()
        self.url = self.fake.start()
        self.network = []  # what an mDNS search finds
        os.environ.pop("RH_OFT_URL", None)

    def tearDown(self):
        self.fake.stop()
        os.environ.pop("RH_OFT_URL", None)

    def start(self, timers=None):
        self.api = FakeRhapi(timers)
        self.panel = rh_oft.initialize(self.api, discover=lambda timeout: list(self.network))
        return self.panel

    def field(self, name):
        return self.api.fields.by_name[name]

    def choices(self, name):
        return [o.value for o in self.field(name).options]

    def test_nothing_is_added_by_itself(self):
        self.network = [found("oft-fake", self.url)]
        self.start()
        self.assertEqual(self.api.interfaces, [], "a Timer on the network is not used until the user adds it")
        self.assertIn("None yet", self.api.text)
        self.assertEqual(self.choices(OPT_FOUND), [""])

    def test_find_lists_timers_and_add_keeps_the_one_picked(self):
        self.start()
        self.network = [found("oft-fake", self.url), found("oft-other", "http://10.0.0.9:7270", seats="1")]
        self.api.buttons["rh_oft_find"](None)
        self.assertEqual(self.choices(OPT_FOUND), ["oft-fake", "oft-other"])
        self.assertIn("oft-other at http://10.0.0.9:7270 (1 seats)", self.api.text)
        self.assertEqual(self.api.said[-1], "Found 2 OFT Timer(s), 2 not in use yet.")
        self.assertEqual(load_timers(self.api), [], "finding adds nothing")

        self.api.options[OPT_FOUND] = "oft-other"  # the user picks it in the list
        self.api.buttons["rh_oft_add"](None)
        self.assertEqual(load_timers(self.api), [{"id": "oft-other", "url": "http://10.0.0.9:7270", "seats": 1}])
        self.assertEqual(self.api.said[-1], "Added Timer oft-other. Restart RotorHazard to apply.")
        self.assertEqual(self.choices(OPT_FOUND), ["oft-fake"], "what is in use is no longer offered")
        self.assertEqual(self.choices(OPT_REMOVE), ["oft-other"])
        self.assertIn("**oft-other** at http://10.0.0.9:7270: added. Restart", self.api.text)
        self.assertGreaterEqual(self.api.broadcasts, 2, "the open settings page is told to redraw")

        self.api.buttons["rh_oft_add"](None)  # the selection moved on to oft-fake
        self.assertEqual([t["id"] for t in load_timers(self.api)], ["oft-other", "oft-fake"])

    def test_add_by_address_asks_the_timer_for_its_id(self):
        self.start()
        self.api.options[OPT_ADDRESS] = self.url.replace("http://", "")
        self.api.buttons["rh_oft_add_address"](None)
        self.assertEqual([(t["id"], t["url"]) for t in load_timers(self.api)], [("oft-fake", self.url)])
        self.api.options[OPT_ADDRESS] = "http://127.0.0.1:9"
        self.api.buttons["rh_oft_add_address"](None)
        self.assertIn("No OFT Timer answers at http://127.0.0.1:9", self.api.said[-1])
        self.assertEqual(len(load_timers(self.api)), 1)

    def test_added_timer_is_used_after_a_restart_and_can_be_removed(self):
        self.fake.config["seats"] = [{"seat": n, "freq_mhz": 5658 + 37 * n, "enabled": True} for n in range(3)]
        self.start([{"id": "oft-fake", "url": self.url, "seats": 1}])
        self.assertEqual([(i.timer_id, i.url, len(i.nodes)) for i in self.api.interfaces], [("oft-fake", self.url, 3)])
        self.assertEqual(load_timers(self.api)[0]["seats"], 3, "the seat count seen is remembered")
        self.assertIn("not answering, 3 seat(s)", self.api.text)  # not polled yet in this test
        self.api.interfaces[0].update()
        self.panel.refresh()
        self.assertIn("**oft-fake** at %s: connected, 3 seat(s)" % self.url, self.api.text)

        self.api.buttons["rh_oft_remove"](None)
        self.assertEqual(load_timers(self.api), [])
        self.assertIn("removed. Restart RotorHazard", self.api.text)

    def test_timer_is_found_by_id_when_its_address_changed(self):
        self.network = [found("oft-fake", self.url)]
        self.start([{"id": "oft-fake", "url": "http://10.0.0.77:7270", "seats": 2}])
        self.assertEqual(self.api.interfaces[0].url, self.url)
        self.assertEqual(load_timers(self.api)[0]["url"], self.url, "the new address is saved")

    def test_timer_that_is_off_keeps_its_seats(self):
        self.start([{"id": "oft-gone", "url": "http://127.0.0.1:9", "seats": 2}])
        self.assertEqual(len(self.api.interfaces[0].nodes), 2)
        self.assertEqual(load_timers(self.api), [{"id": "oft-gone", "url": "http://127.0.0.1:9", "seats": 2}])

    def test_running_timer_that_moves_has_its_new_address_saved(self):
        self.start([{"id": "oft-fake", "url": self.url, "seats": 1}])
        iface = self.api.interfaces[0]
        iface.on_moved("oft-fake", "http://10.0.0.5:7270")
        self.assertEqual(load_timers(self.api)[0]["url"], "http://10.0.0.5:7270")
        self.network = [found("oft-fake", "http://10.0.0.6:7270")]
        self.assertEqual(iface.locate("oft-fake"), "http://10.0.0.6:7270")
        self.assertIsNone(iface.locate("oft-unknown"))

    def test_rh_oft_url_adds_timers_without_saving_them(self):
        os.environ["RH_OFT_URL"] = self.url + ", 127.0.0.1:9"
        self.start()
        self.assertEqual([i.url for i in self.api.interfaces], [self.url, "http://127.0.0.1:9"])
        self.assertEqual(load_timers(self.api), [])
        self.assertIn("set by RH_OFT_URL", self.api.text)

    def test_new_install_has_no_database_when_plugins_load(self):
        # RotorHazard creates its database after loading plugins: every option call fails.
        os.environ["RH_OFT_URL"] = self.url
        self.api = FakeRhapi()

        def no_database(*a, **k):
            raise RuntimeError("no such table: global_settings")

        self.api.db.option = self.api.db.option_set = no_database
        rh_oft.initialize(self.api, discover=lambda timeout: [])
        self.assertEqual(len(self.api.interfaces), 1)
        self.assertIn("rh_oft_find", self.api.buttons)

    def test_helpers(self):
        self.assertEqual(rh_oft.parse_urls("http://a:7270/, b:7270\nhttp://a:7270"), ["http://a:7270", "http://b:7270"])
        with self.assertLogs("rh_oft", level="ERROR"):
            self.assertEqual(rh_oft.search(lambda t: 1 / 0), [], "a failed search finds nothing")
        self.fake.config["seats"] = [{"seat": n, "freq_mhz": 5658, "enabled": n < 2} for n in range(3)]
        self.assertEqual(rh_oft.timer_seat_count(self.url), 3, "disabled seats count too")
        self.assertIsNone(rh_oft.timer_seat_count("http://127.0.0.1:9"), "an unreachable Timer")
        self.assertEqual(load_timers(FakeRhapi("not a list")), [])


if __name__ == "__main__":
    unittest.main()
