"""Test stub of RotorHazard's BaseHardwareInterface: the parts rh_oft touches."""

import logging
from time import monotonic

logger = logging.getLogger(__name__)


class MarshalType:
    FULL_RSSI = 1
    PEAKS_NADIRS = 2


class BaseHardwareInterface(object):
    LAP_SOURCE_REALTIME = 0
    LAP_SOURCE_MANUAL = 1
    LAP_SOURCE_RECALC = 2
    LAP_SOURCE_AUTOMATIC = 3
    LAP_SOURCE_API = 4

    RACE_STATUS_READY = 0
    RACE_STATUS_STAGING = 3
    RACE_STATUS_RACING = 1
    RACE_STATUS_DONE = 2

    def __init__(self):
        self.start_time = 1000 * monotonic()
        self.race_status = self.RACE_STATUS_READY
        self.pass_record_callback = None
        self.new_enter_or_exit_at_callback = None
        self.node_crossing_callback = None
        self.nodes = []
        self.marshal_type = None
        self.ready_failure_msg = None
        self.logged = []

    def log(self, message):
        self.logged.append(message)
        logger.info("%s: %s", type(self).__name__, message)

    def process_crossings(self, cross_list):
        for node in cross_list:
            self.node_crossing_callback(node)

    def get_heartbeat_json(self):
        return {"current_rssi": [n.current_rssi for n in self.nodes]}
