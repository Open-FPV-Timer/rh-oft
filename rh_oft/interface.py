"""The RotorHazard hardware interface for an OFT Timer.

One OFT **seat** becomes one RotorHazard **node**. The interface polls the Timer over
HTTP (``/crossings`` for laps, ``/status`` for live signal) and pushes laps into
RotorHazard with the Timer's own timestamps, aligned to ``monotonic()`` by
:class:`~rh_oft.clock.ClockAlign`. RotorHazard's frequency and EnterAt/ExitAt settings are
forwarded to the Timer's ``/config``.

Signal units: OFT reports power in **0.1 dB**; RotorHazard's UI expects RX5808-like
integers (noise ~30, a close pass ~100-200). :func:`ddb_to_rh` / :func:`rh_to_ddb` map
linearly between them, 1 RH unit = 0.8 dB.
"""

import logging
from time import monotonic

import gevent

from BaseHardwareInterface import BaseHardwareInterface, MarshalType
from Node import Node

from .clock import ClockAlign
from .oft_client import OftClient, OftError

logger = logging.getLogger(__name__)

#: 0.1 dB -> RotorHazard units: (ddb + OFFSET) / DIV, clamped to 1..255.
RH_OFFSET_DDB = 1200
RH_DIV = 8

#: Poll period for crossings and status, seconds.
POLL_S = 0.1
#: Re-align the clock this often, seconds.
TIME_S = 1.0
#: Refresh /status this often (RSSI for the UI), seconds.
STATUS_S = 0.25


def ddb_to_rh(ddb):
    return max(1, min(255, int(round((ddb + RH_OFFSET_DDB) / RH_DIV))))


def rh_to_ddb(rh):
    return int(round(rh * RH_DIV - RH_OFFSET_DDB))


class OftInterface(BaseHardwareInterface):
    def __init__(self, url, seats=1, client=None, sleep=gevent.sleep, now=monotonic):
        BaseHardwareInterface.__init__(self)
        self.url = url
        self.client = client or OftClient(url)
        self.clock = ClockAlign()
        self.marshal_type = MarshalType.FULL_RSSI
        self._sleep = sleep
        self._now = now
        self.update_thread = None
        self.connected = False
        self.timer_info = None
        self.last_seq = 0
        self.boot_id = None
        self._next_time = 0.0
        self._next_status = 0.0
        self._config_dirty = False
        self._thresholds = {}  # seat -> (enter_ddb, exit_ddb) once RH has set them

        self.nodes = []
        for index in range(int(seats)):
            node = Node()
            node.index = index
            node.api_valid_flag = True
            node.max_rssi_value = 255
            node.enter_at_level = ddb_to_rh(-770)
            node.exit_at_level = ddb_to_rh(-820)
            self.nodes.append(node)

    # -- RotorHazard lifecycle ---------------------------------------------------------

    @property
    def ready(self):
        return True  # RotorHazard must start even if the gate is not plugged in yet

    def start(self):
        if self.update_thread is None:
            self.log("Starting OFT poll loop for %s" % self.url)
            self.update_thread = gevent.spawn(self.update_loop)

    def stop(self):
        if self.update_thread:
            self.update_thread.kill(block=True, timeout=0.5)
            self.update_thread = None

    def update_loop(self):
        while True:
            try:
                self.update()
            except Exception:  # keep the loop alive whatever happens
                logger.exception("OFT update failed")
            self._sleep(POLL_S)

    # -- one poll ----------------------------------------------------------------------

    def update(self):
        now = self._now()
        try:
            if not self.connected:
                self._connect()
            if now >= self._next_time:
                self._align_clock()
                self._next_time = now + TIME_S
            if self._config_dirty:
                self._push_config()
            self._poll_crossings()
            if now >= self._next_status:
                self._poll_status()
                self._next_status = now + STATUS_S
        except OftError as e:
            if self.connected:
                self.log("Lost OFT Timer at %s: %s" % (self.url, e))
            self.connected = False
            self.ready_failure_msg = str(e)

    def _connect(self):
        info = self.client.info()
        self.timer_info = info
        boot_id = info.get("boot_id")
        if boot_id != self.boot_id:
            # New Timer process: its sequence numbers and clock start over.
            self.boot_id = boot_id
            self.last_seq = 0
            self.clock.reset()
        self.connected = True
        self.ready_failure_msg = None
        self._config_dirty = True  # make sure the Timer has RH's frequencies
        self.log("Connected to OFT Timer %s (%s, level %s)" % (info.get("id"), info.get("name"), info.get("level")))
        for node in self.nodes:
            node.firmware_version_str = "oft %s" % info.get("fw", "?")

    def _align_clock(self):
        t_send, t_recv, timer_us, boot_id = self.client.time()
        if boot_id is not None and self.boot_id is not None and boot_id != self.boot_id:
            self.log("OFT Timer rebooted (boot_id %s -> %s)" % (self.boot_id, boot_id))
            self.boot_id = boot_id
            self.last_seq = 0
        self.clock.add(t_send, t_recv, timer_us, boot_id)

    def _poll_crossings(self):
        body = self.client.crossings(self.last_seq)
        if body.get("boot_id") not in (None, self.boot_id):
            self.boot_id = body["boot_id"]
            self.last_seq = 0
            self.clock.reset()
            return
        for c in body.get("crossings", []):
            seq = int(c["seq"])
            if seq <= self.last_seq:
                continue
            self.last_seq = seq
            seat = int(c["seat"])
            if seat >= len(self.nodes):
                continue
            node = self.nodes[seat]
            if not node.frequency:
                continue  # disabled seat
            if not self.clock.synced:
                self.log("Dropping crossing seq %d: clock not aligned yet" % seq)
                continue
            lap_ts = self.clock.to_local(int(c["t_mid"]))
            node.lap_timestamp = lap_ts
            node.pass_peak_rssi = ddb_to_rh(int(c.get("peak", -1200)))
            node.enter_at_timestamp = self.clock.to_local(int(c["t_enter"])) if "t_enter" in c else 0
            node.exit_at_timestamp = self.clock.to_local(int(c["t_exit"])) if "t_exit" in c else 0
            node.node_lap_id = seq
            if callable(self.pass_record_callback):
                self.pass_record_callback(node, lap_ts, BaseHardwareInterface.LAP_SOURCE_REALTIME, peak=node.pass_peak_rssi)

    def _poll_status(self):
        st = self.client.status()
        cross_list = []
        readtime = self._now()
        for s in st.get("seats", []):
            seat = int(s["seat"])
            if seat >= len(self.nodes):
                continue
            node = self.nodes[seat]
            if s.get("rssi") is not None:
                node.current_rssi = ddb_to_rh(int(s["rssi"]))
                node.node_peak_rssi = max(node.node_peak_rssi, node.current_rssi)
                node.node_nadir_rssi = node.current_rssi if not node.node_nadir_rssi else min(node.node_nadir_rssi, node.current_rssi)
                # Feed the RSSI graph; RotorHazard prunes this itself.
                if self.race_status != BaseHardwareInterface.RACE_STATUS_DONE:
                    node.history_values.append(node.current_rssi)
                    node.history_times.append(readtime)
            crossing = bool(s.get("crossing"))
            if crossing != node.crossing_flag:
                node.crossing_flag = crossing
                if crossing:
                    node.pass_crossing_flag = True
                cross_list.append(node)
        if cross_list and callable(self.node_crossing_callback):
            self.process_crossings(cross_list)

    # -- RotorHazard -> Timer --------------------------------------------------------------

    def _push_config(self):
        cfg = self.client.get_config()
        seats = []
        for node in self.nodes:
            seats.append({"seat": node.index, "freq_mhz": int(node.frequency) if node.frequency else 5800, "enabled": bool(node.frequency)})
        cfg["seats"] = seats
        if self._thresholds:
            # RotorHazard tunes per node; OFT v0.3 has one threshold for the Timer, so use
            # seat 0's (single-pilot B1). Per-seat thresholds are a spec TODO.
            enter, exit_ = self._thresholds.get(0, next(iter(self._thresholds.values())))
            cfg["threshold"] = {"mode": "manual", "enter": enter, "exit": exit_}
        self.client.put_config(cfg)
        self._config_dirty = False

    def set_frequency(self, node_index, frequency, *_args):
        node = self.nodes[node_index]
        node.debug_pass_count = 0
        node.frequency = frequency or 0
        self._config_dirty = True

    def transmit_enter_at_level(self, node, level):
        return level

    def transmit_exit_at_level(self, node, level):
        return level

    def set_enter_at_level(self, node_index, level):
        node = self.nodes[node_index]
        node.enter_at_level = level
        enter, exit_ = self._thresholds.get(node_index, (None, rh_to_ddb(node.exit_at_level)))
        self._thresholds[node_index] = (rh_to_ddb(level), exit_)
        self._config_dirty = True

    def set_exit_at_level(self, node_index, level):
        node = self.nodes[node_index]
        node.exit_at_level = level
        enter, _ = self._thresholds.get(node_index, (rh_to_ddb(node.enter_at_level), None))
        self._thresholds[node_index] = (enter, rh_to_ddb(level))
        self._config_dirty = True

    def force_end_crossing(self, node_index):
        self.log("force_end_crossing is not supported by OFT yet (seat %d)" % node_index)

    def jump_to_bootloader(self):
        pass

    def send_status_message(self, *_a):
        return None

    def send_shutdown_button_state(self, *_a):
        return None

    def send_shutdown_started_message(self):
        return None

    def send_server_idle_message(self):
        return None

    # -- diagnostics ---------------------------------------------------------------------

    def get_heartbeat_json(self):
        hb = BaseHardwareInterface.get_heartbeat_json(self)
        return hb

    def oft_summary(self):
        return {
            "url": self.url,
            "connected": self.connected,
            "timer": (self.timer_info or {}).get("id"),
            "clock_synced": self.clock.synced,
            "clock_error_ms": None if self.clock.error_estimate_s() is None else round(self.clock.error_estimate_s() * 1000, 2),
            "last_seq": self.last_seq,
        }
