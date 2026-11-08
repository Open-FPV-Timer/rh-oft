"""rh-oft: an Open FPV Timer (OFT) gate as RotorHazard timing seats.

RotorHazard calls :func:`initialize` early in startup. We register the OFT hardware
interface with ``rhapi.interface.add`` so its seats join whatever nodes the Pi already
has, and add a settings panel for the Timer's address.
"""

import logging

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "0.1.0"
PANEL = "rh_oft"
OPT_URL = "rh_oft_url"
OPT_SEATS = "rh_oft_seats"
DEFAULT_URL = "http://oft.local:7270"
DEFAULT_SEATS = 1


def initialize(rhapi):
    from .interface import OftInterface

    url = rhapi.db.option(OPT_URL, DEFAULT_URL) or DEFAULT_URL
    try:
        seats = int(rhapi.db.option(OPT_SEATS, DEFAULT_SEATS) or DEFAULT_SEATS)
    except (TypeError, ValueError):
        seats = DEFAULT_SEATS
    seats = max(1, min(8, seats))

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
        UIField(OPT_SEATS, "Seats", UIFieldType.BASIC_INT, value=DEFAULT_SEATS, desc="OFT seats to add as RotorHazard nodes (1-8). Restart after changing."),
        PANEL,
    )
