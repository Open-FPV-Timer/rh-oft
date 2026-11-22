"""The settings panel: the Timers in use, the Timers on the network, add and remove.

Built from RotorHazard's own panel parts (text, fields, buttons), so it needs no page of
its own. RotorHazard fixes its nodes when it starts, so adding or removing a Timer takes
effect at the next restart; the panel says so.

The Timers in use are kept in RotorHazard's config file (section ``RH_OFT``, ``TIMERS``):
``[{"id": "oft-3fa2", "url": "http://192.168.1.50:7270", "seats": 2}]``. ``url`` is where
the Timer was last seen and ``seats`` how many it had then. The config file, not the
options database, because RotorHazard opens the database only after plugins have started,
and the Timers must be known by then.
"""

import logging

from .oft_client import OftClient, OftError

logger = logging.getLogger(__name__)

PANEL = "rh_oft"
SECTION = "RH_OFT"
KEY_TIMERS = "TIMERS"
OPT_FOUND = "rh_oft_found"  # which found Timer is selected
OPT_ADDRESS = "rh_oft_address"  # an address typed in by hand
OPT_REMOVE = "rh_oft_remove"  # which added Timer is selected
RESTART = "Restart RotorHazard to apply."


def set_option(rhapi, name, value):
    """Save an option; False if RotorHazard cannot (its database is not there yet)."""
    try:
        rhapi.db.option_set(name, value)
        return True
    except Exception as e:  # on a new install plugins load before the database exists
        logger.debug("rh-oft: option %s not saved: %s", name, e)
        return False


def load_timers(rhapi):
    rhapi.config.register_section(SECTION)  # or RotorHazard drops the section on its next save
    timers = rhapi.config.get(SECTION, KEY_TIMERS)
    if not isinstance(timers, list):
        return []
    return [{"id": t.get("id"), "url": t["url"], "seats": int(t.get("seats") or 0)}
            for t in timers if isinstance(t, dict) and t.get("url")]


def save_timers(rhapi, timers):
    """Save the Timers in use; False if RotorHazard could not."""
    return bool(rhapi.config.set(SECTION, KEY_TIMERS, [{"id": t["id"], "url": t["url"], "seats": t.get("seats", 0)} for t in timers]))


class Panel:
    def __init__(self, rhapi, interfaces, discover, client=OftClient, fixed=()):
        self.rhapi = rhapi
        self.interfaces = interfaces  # the Timers RotorHazard is using since it started
        self.fixed = list(fixed)  # those of them that came from RH_OFT_URL, not from this panel
        self.discover = discover
        self.client = client
        self.found = None  # the last search: None until Find Timers is pressed

    def _option(self, name):
        try:
            return self.rhapi.db.option(name, "") or ""
        except Exception:  # no database yet
            return ""

    # -- what to show -----------------------------------------------------------------

    def _running(self, timer):
        for iface in self.interfaces:
            if (timer["id"] and iface.timer_id == timer["id"]) or iface.url == timer["url"]:
                return iface
        return None

    def text(self):
        timers = load_timers(self.rhapi)
        lines = ["**Timers in use**", ""]
        for timer in timers:
            iface = self._running(timer)
            if iface is None:
                state = "added. " + RESTART
            elif iface.connected:
                state = "connected, %d seat(s)" % len(iface.nodes)
                if iface.controlled_by:
                    state += ". **Controlled by %s**: RotorHazard cannot change this Timer until it is given control on the Timer's page" % iface.controlled_by
            else:
                state = "not answering, %d seat(s)" % len(iface.nodes)
            lines.append("- **%s** at %s: %s" % (timer["id"] or "?", (iface.url if iface else timer["url"]), state))
        for iface in self.interfaces:
            if not any(self._running(t) is iface for t in timers):
                where = "set by RH_OFT_URL" if any(iface is f for f in self.fixed) else "removed. " + RESTART
                lines.append("- **%s** at %s: %s" % (iface.timer_id or "?", iface.url, where))
        if len(lines) == 2:
            lines.append("- None yet. Press Find Timers, pick one and add it.")
        if self.found is not None:
            lines += ["", "**Timers on the network**", ""]
            ids = {t["id"] for t in timers}
            for found in self.found:
                lines.append("- %s%s" % (found.describe(), " (in use)" if found.id in ids else ""))
            if not self.found:
                lines.append("- None found. A Timer on another network can be added by its address.")
        return "\n".join(lines)

    def _choices(self):
        """(found Timers that can be added, added Timers that can be removed) as select options."""
        timers = load_timers(self.rhapi)
        ids = {t["id"] for t in timers}
        can_add = [(f.id, f.describe()) for f in (self.found or []) if f.id not in ids]
        can_remove = [(t["id"] or t["url"], "%s at %s" % (t["id"] or "?", t["url"])) for t in timers]
        return can_add, can_remove

    # -- RotorHazard panel ----------------------------------------------------------

    def watch_control(self):
        for iface in self.interfaces:
            iface.on_control = self.control_changed

    def register(self):
        from RHUI import UIField, UIFieldSelectOption, UIFieldType

        ui, fields = self.rhapi.ui, self.rhapi.fields
        ui.register_panel(PANEL, "OFT Timers", "settings")
        ui.register_markdown(PANEL, "rh_oft_list", self.text())
        can_add, can_remove = self._choices()

        def select(name, label, choices, empty):
            options = [UIFieldSelectOption(value, text) for value, text in choices] or [UIFieldSelectOption("", empty)]
            # Keep the saved selection on something that is in the list.
            if self._option(name) not in [o.value for o in options]:
                set_option(self.rhapi, name, options[0].value)
            fields.register_option(UIField(name, label, UIFieldType.SELECT, options=options, value=options[0].value), PANEL)

        select(OPT_FOUND, "Timers found", can_add, "Press Find Timers" if self.found is None else "No new Timers found")
        fields.register_option(UIField(OPT_ADDRESS, "Or a Timer's address", UIFieldType.TEXT, value="", placeholder="http://192.168.1.50:7270",
                                       desc="For a Timer that Find Timers cannot see, such as one on another network."), PANEL)
        select(OPT_REMOVE, "Timers in use", can_remove, "None")
        ui.register_quickbutton(PANEL, "rh_oft_find", "Find Timers", self.find)
        ui.register_quickbutton(PANEL, "rh_oft_add", "Add the Timer found", self.add_found)
        ui.register_quickbutton(PANEL, "rh_oft_add_address", "Add the Timer at the address", self.add_address)
        ui.register_quickbutton(PANEL, "rh_oft_remove", "Remove the Timer in use", self.remove)

    def refresh(self, message=None):
        self.register()  # same names: RotorHazard replaces what is there
        self.rhapi.ui.broadcast_ui("settings")
        if message:
            logger.info("rh-oft: %s", message)
            self.rhapi.ui.message_notify(message)

    def control_changed(self, iface, name):
        """An interface lost control of its Timer to ``name``, or (None) has it back."""
        timer = iface.timer_id or iface.url
        if name:
            self.refresh("OFT Timer %s is controlled by %s. RotorHazard's frequencies and levels will not reach it until RotorHazard is given control on the Timer's page." % (timer, name))
        else:
            self.refresh("OFT Timer %s: RotorHazard can change it again, and has sent it its settings." % timer)

    # -- buttons ------------------------------------------------------------------------

    def find(self, _args=None):
        self.found = self.discover()
        new = self._choices()[0]
        if not self.found:
            message = "No OFT Timers found on the network."
        else:
            message = "Found %d OFT Timer(s), %d not in use yet." % (len(self.found), len(new))
        self.refresh(message)

    def _add(self, timer_id, url, seats):
        timers = load_timers(self.rhapi)
        if any(t["id"] == timer_id for t in timers):
            return self.refresh("Timer %s is already in use." % timer_id)
        timers.append({"id": timer_id, "url": url, "seats": seats})
        if not save_timers(self.rhapi, timers):
            return self.refresh("Timer %s could not be saved." % timer_id)
        self.refresh("Added Timer %s. %s" % (timer_id, RESTART))

    def add_found(self, _args=None):
        picked = self._option(OPT_FOUND)
        for found in self.found or []:
            if found.id == picked:
                try:
                    seats = int(found.txt.get("seats") or 0)
                except ValueError:
                    seats = 0
                return self._add(found.id, found.url, seats)
        self.refresh("Press Find Timers and pick a Timer first.")

    def add_address(self, _args=None):
        from . import parse_urls

        urls = parse_urls(self._option(OPT_ADDRESS))
        if len(urls) != 1:
            return self.refresh("Type one Timer address, like http://192.168.1.50:7270.")
        try:
            info = self.client(urls[0], timeout=2.0).info()
            timer_id = info["id"]
        except (OftError, KeyError, TypeError) as e:
            return self.refresh("No OFT Timer answers at %s (%s)." % (urls[0], e))
        set_option(self.rhapi, OPT_ADDRESS, "")
        self._add(timer_id, urls[0], len(info.get("seats") or []))

    def remove(self, _args=None):
        picked = self._option(OPT_REMOVE)
        timers = load_timers(self.rhapi)
        keep = [t for t in timers if (t["id"] or t["url"]) != picked]
        if not picked or len(keep) == len(timers):
            return self.refresh("Pick a Timer in use first.")
        save_timers(self.rhapi, keep)
        self.refresh("Removed Timer %s. %s" % (picked, RESTART))
