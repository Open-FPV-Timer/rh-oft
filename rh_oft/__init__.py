"""rh-oft: Open FPV Timer (OFT) gates as RotorHazard timing seats.

RotorHazard calls :func:`initialize` early in startup. We register one OFT hardware
interface for each Timer the user has added, with ``rhapi.interface.add``, so their seats
join whatever nodes the Pi already has. The settings panel (:mod:`rh_oft.panel`) lists the
Timers on the network and lets the user add and remove them.

A Timer is remembered by its **id**. Its address is only where it was last seen: at
startup, and whenever a Timer stops answering, it is looked up again by id over mDNS.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "0.5.0"
MAX_SEATS = 8
#: How long to listen for Timers answering an mDNS search, seconds.
DISCOVER_S = 2.0


def parse_urls(text):
    """Timer addresses split by commas or spaces; ``http://`` is added where missing."""
    urls = []
    for part in re.split(r"[,\s]+", str(text or "")):
        if not part:
            continue
        if "://" not in part:
            part = "http://" + part
        part = part.rstrip("/")
        if part not in urls:
            urls.append(part)
    return urls


def search(discover=None):
    """The Timers on the network now. Never raises: a failed search finds nothing."""
    if discover is None:
        from .mdns import discover
    try:
        return discover(DISCOVER_S)
    except Exception:
        logger.exception("rh-oft: looking for Timers failed")
        return []


def timer_seat_count(url, client=None):
    """How many seats the Timer's ``/config`` has, or None if it cannot be asked.

    RotorHazard fixes its node count at startup, so this is asked once, there.
    """
    from .oft_client import OftClient, OftError

    try:
        config = (client or OftClient(url, timeout=2.0)).get_config()
        return len(config.get("seats") or []) or None
    except (OftError, AttributeError):
        return None


def initialize(rhapi, discover=None):
    from .interface import OftInterface
    from .panel import Panel, load_timers, save_timers

    timers = load_timers(rhapi)
    saved = [dict(t) for t in timers]
    # RH_OFT_URL: Timers to use without adding them in the settings, for containers.
    for url in parse_urls(os.environ.get("RH_OFT_URL")):
        if not any(t["url"] == url for t in timers):
            timers.append({"id": None, "url": url, "seats": 0, "fixed": True})

    def locate(timer_id):
        """Where the Timer with this id is now, or None."""
        for found in search(discover):
            if found.id == timer_id:
                return found.url
        return None

    def moved(timer_id, url):
        stored = load_timers(rhapi)
        for timer in stored:
            if timer["id"] == timer_id:
                timer["url"] = url
        save_timers(rhapi, stored)

    # Addresses change (DHCP); ids do not. Look every added Timer up before using it.
    if any(t["id"] for t in timers):
        now = {found.id: found.url for found in search(discover)}
        for timer in timers:
            if timer["id"] in now and now[timer["id"]] != timer["url"]:
                logger.info("rh-oft: Timer %s moved from %s to %s", timer["id"], timer["url"], now[timer["id"]])
                timer["url"] = now[timer["id"]]

    interfaces, fixed = [], []
    for timer in timers:
        seats = timer_seat_count(timer["url"])
        if seats is None:
            seats = timer.get("seats") or 1
            logger.warning("rh-oft: Timer %s at %s does not answer; adding %d seat(s) for it", timer["id"] or "?", timer["url"], seats)
        seats = max(1, min(MAX_SEATS, seats))
        timer["seats"] = seats
        iface = OftInterface(timer["url"], seats=seats, timer_id=timer["id"], locate=locate, on_moved=moved)
        rhapi.interface.add(iface)
        interfaces.append(iface)
        if timer.get("fixed"):
            fixed.append(iface)
        logger.info("rh-oft %s: added %d OFT seat(s) from %s at %s", PLUGIN_VERSION, seats, timer["id"] or "a Timer", timer["url"])
    if not timers:
        logger.info("rh-oft %s: no Timers added yet. Add one in Settings -> OFT Timers.", PLUGIN_VERSION)
    keep = [t for t in timers if not t.get("fixed")]
    if keep != saved:  # a new address or seat count
        save_timers(rhapi, keep)

    panel = Panel(rhapi, interfaces, discover=lambda: search(discover), fixed=fixed)
    panel.register()
    panel.watch_control()
    return panel
