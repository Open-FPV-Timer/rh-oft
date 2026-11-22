"""rh-oft: an Open FPV Timer (OFT) gate as RotorHazard timing seats.

RotorHazard calls :func:`initialize` early in startup. We register the OFT hardware
interface with ``rhapi.interface.add`` so its seats join whatever nodes the Pi already
has, and add a settings panel for the Timer's address.
"""

import logging

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "0.2.0"
PANEL = "rh_oft"
OPT_URL = "rh_oft_url"
OPT_SEATS = "rh_oft_seats"
DEFAULT_URL = "http://oft.local:7270"
DEFAULT_SEATS = 0  # 0 = as many as the Timer is set up with
MAX_SEATS = 8


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


def initialize(rhapi):
    from .interface import OftInterface

    url = rhapi.db.option(OPT_URL, DEFAULT_URL) or DEFAULT_URL
    try:
        seats = int(rhapi.db.option(OPT_SEATS, DEFAULT_SEATS) or DEFAULT_SEATS)
    except (TypeError, ValueError):
        seats = DEFAULT_SEATS
    if seats <= 0:
        seats = timer_seat_count(url)
        if seats is None:
            seats = 1
            logger.warning("rh-oft: could not ask %s for its seats; adding 1. Set the seat count in Settings -> OFT Timer.", url)
    seats = max(1, min(MAX_SEATS, seats))

    iface = OftInterface(url, seats=seats)
    rhapi.interface.add(iface)
    logger.info("rh-oft %s: added %d OFT seat(s) from %s", PLUGIN_VERSION, seats, url)

    _register_ui(rhapi)


def _register_ui(rhapi):
    try:
        from RHUI import UIField, UIFieldType
    except ImportError:  # running outside RotorHazard
        return
    rhapi.ui.register_panel(PANEL, "OFT Timer", "settings")
    rhapi.fields.register_option(
        UIField(OPT_URL, "Timer URL", UIFieldType.TEXT, value=DEFAULT_URL, desc="http://<timer>:7270. Restart RotorHazard after changing."),
        PANEL,
    )
    rhapi.fields.register_option(
        UIField(OPT_SEATS, "Seats", UIFieldType.BASIC_INT, value=DEFAULT_SEATS, desc="OFT seats to add as RotorHazard nodes (1-8), or 0 to use as many as the Timer is set up with. Restart after changing."),
        PANEL,
    )
