"""The RotorHazard hardware interface for an OFT Timer.

One OFT **seat** becomes one RotorHazard **node**. The interface polls the Timer over
HTTP (``/crossings`` for laps, ``/status`` for live signal) and pushes laps into
RotorHazard with the Timer's own timestamps, aligned to ``monotonic()`` by
:class:`~rh_oft.clock.ClockAlign`. RotorHazard's frequency and EnterAt/ExitAt settings are
forwarded to the Timer's ``/config``.

Signal units: every OFT level is in the unit of the Node that measured it (spec §6.4),
given per Node in ``/info`` as ``nodes[].level``. RotorHazard's UI expects RX5808-like
integers, 1 to 255. :class:`LevelMap` converts between the two for one Node's unit:
tenths of a dB map linearly (1 RH unit = 0.8 dB), millivolts divide by 6.5.
"""

import logging
import socket
from time import monotonic

import gevent

from BaseHardwareInterface import BaseHardwareInterface, MarshalType
from Node import Node

from .clock import ClockAlign
from .oft_client import OftClient, OftControlled, OftError
from .stream import ControlStream, StreamError

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


#: Millivolts per RotorHazard count: what an RH node shows for an RX5808's RSSI voltage.
RH_MV_PER_COUNT = 6.5
#: Look a Timer that does not answer up again by its id this often, seconds.
LOCATE_S = 10.0
#: Ask /info again this often while /status names a Node we have no unit for, seconds.
INFO_RETRY_S = 2.0
#: After a refused write, or a stream that would not open, wait this long before trying again.
CONTROL_RETRY_S = 2.0


def claim_name(host=None):
    """The name the Timer shows for this client's claim (spec §6.6): 1 to 32 printable ASCII."""
    host = socket.gethostname() if host is None else host
    name = "RotorHazard " + "".join(c for c in host if " " <= c <= "~" and c not in '"\\')
    return name.strip()[:32].strip()


def _clamp_rh(value):
    return max(1, min(255, int(round(value))))


def ddb_to_rh(ddb):
    return _clamp_rh((ddb + RH_OFFSET_DDB) / RH_DIV)


def rh_to_ddb(rh):
    return int(round(rh * RH_DIV - RH_OFFSET_DDB))


class LevelMap:
    """One Node's level numbers <-> RotorHazard's 1..255 counts.

    ``unit`` and ``scale`` are the Node's ``level`` from ``/info``; a Node without one
    reports tenths of a dB.
    """

    def __init__(self, unit="dB", scale=0.1):
        self.unit = unit
        self.scale = float(scale) or 1.0

    @classmethod
    def from_info(cls, level):
        level = level or {}
        return cls(level.get("unit", "dB"), level.get("scale", 0.1))

    @property
    def known(self):
        return self.unit in ("dB", "mV")

    def to_rh(self, level):
        value = level * self.scale
        if self.unit == "dB":
            return ddb_to_rh(value * 10)
        if self.unit == "mV":
            return _clamp_rh(value / RH_MV_PER_COUNT)
        return _clamp_rh(value)  # a unit we do not know: show it as given

    def from_rh(self, rh):
        if self.unit == "dB":
            value = rh_to_ddb(rh) / 10.0
        elif self.unit == "mV":
            value = rh * RH_MV_PER_COUNT
        else:
            value = rh
        return int(round(value / self.scale))

    def __eq__(self, other):
        return isinstance(other, LevelMap) and (self.unit, self.scale) == (other.unit, other.scale)

    def __repr__(self):
        return "LevelMap(%r, %r)" % (self.unit, self.scale)


class OftInterface(BaseHardwareInterface):
    def __init__(self, url, seats=1, client=None, sleep=gevent.sleep, now=monotonic, timer_id=None, locate=None, on_moved=None):
        """``timer_id`` is the Timer this interface is for; ``url`` is where it was last seen.

        ``locate(timer_id)`` returns the Timer's address now (or None); it is asked when
        the Timer stops answering, or when another Timer answers at ``url``.
        ``on_moved(timer_id, url)`` is told the new address.
        """
        BaseHardwareInterface.__init__(self)
        self.url = url
        self.timer_id = timer_id
        self.locate = locate
        self.on_moved = on_moved
        self._next_locate = 0.0
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
        self._rh_levels_set = set()  # seats whose EnterAt / ExitAt RotorHazard has set
        self._node_levels = {}  # Node id -> LevelMap, from /info
        self._default_level = LevelMap()  # for a seat whose Node we do not know yet
        self._seat_levels = {}  # seat -> LevelMap, from /status seats[].node
        self._next_info = 0.0
        # Control (spec §6.6): one client at a time may change a Timer.
        self.stream = None
        self._next_stream = 0.0
        self._next_write = 0.0
        self._stream_opened = 0.0
        self._last_key = None
        self._control_state = None  # "hold", "free", "other", "wait" (claim not answered yet) or None (not known)
        self._status_holder = None  # /status "control": who holds it, for the user
        #: The client that holds control when it is not us, e.g. "Timer page"; else None.
        self.controlled_by = None
        #: ``on_control(interface, name or None)``: told when control is lost and regained.
        self.on_control = None

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
        self._drop_stream()

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
            self._poll_control(now)
            if self._config_dirty:
                self._push_config(now)
            self._poll_crossings()
            if now >= self._next_status:
                self._poll_status()
                self._next_status = now + STATUS_S
        except OftError as e:
            if self.connected:
                self.log("Lost OFT Timer at %s: %s" % (self.url, e))
            self.connected = False
            self.ready_failure_msg = str(e)
            self._drop_stream()
            self._relocate(now)

    # -- control (spec §6.6) --------------------------------------------------------

    def _has_control_capability(self):
        return "control" in ((self.timer_info or {}).get("capabilities") or {})

    def _drop_stream(self):
        if self.stream is not None:
            self.stream.close()
        self.stream = None
        # The key is kept: the Timer holds our claim for a while after a stream drops, the
        # key still works for writes in that time, and the next stream takes the claim up.
        self._control_state = None

    def _poll_control(self, now):
        """Keep our claim standing and learn whether we may write."""
        if not self._has_control_capability():
            return
        if self.stream is None and now >= self._next_stream:
            self._next_stream = now + CONTROL_RETRY_S
            stream = ControlStream(self.url, claim_name(), now=self._now, key=self._last_key)
            try:
                stream.connect()
                self.stream = stream
                self._stream_opened = now
            except StreamError as e:
                self.log("OFT Timer %s: cannot claim control (%s); writing without a claim" % (self.timer_id, e))
                self._next_stream = now + 15 * CONTROL_RETRY_S
        if self.stream is not None:
            try:
                self.stream.poll()
            except StreamError as e:
                self.log("OFT Timer %s: %s" % (self.timer_id, e))
                self._drop_stream()
        stream = self.stream
        if stream is None:
            return  # no stream: a write is still tried, and a 423 tells us to wait
        if not (stream.known and (stream.claim is not None or stream.refused)):
            # The Timer has not answered our claim yet. Writing now could be refused a
            # moment before the key arrives, so wait for it, but not for ever.
            self._control_state = "wait" if now - self._stream_opened < CONTROL_RETRY_S else None
            return
        state = "hold" if stream.key else ("other" if stream.holder else "free")
        self.client.control_key = stream.key
        if stream.key:
            self._last_key = stream.key
        if state == self._control_state:
            return
        self._control_state = state
        if state == "other":
            self._set_controlled_by(stream.holder.get("name") or "another client")
        else:
            # Control is ours, or nobody's: another client may have changed the Timer
            # meanwhile, so it gets RotorHazard's settings again.
            self._config_dirty = True
            self._next_write = 0.0
            self._set_controlled_by(None)

    def _set_controlled_by(self, name):
        if name == self.controlled_by:
            return
        self.controlled_by = name
        if name:
            self.log("OFT Timer %s is controlled by %s: RotorHazard's settings wait until it has control" % (self.timer_id, name))
        else:
            self.log("OFT Timer %s: RotorHazard may change it again" % self.timer_id)
        if callable(self.on_control):
            self.on_control(self, name)

    def _relocate(self, now):
        """The Timer does not answer where it was: find it by its id."""
        if not self.timer_id or not callable(self.locate) or now < self._next_locate:
            return
        self._next_locate = now + LOCATE_S
        url = self.locate(self.timer_id)
        if url and url != self.url:
            self.log("OFT Timer %s moved from %s to %s" % (self.timer_id, self.url, url))
            self.url = url
            self._drop_stream()
            self.client = OftClient(url)
            self.client.control_key = self._last_key
            if callable(self.on_moved):
                self.on_moved(self.timer_id, url)

    def _connect(self):
        info = self.client.info()
        if self.timer_id is None:
            self.timer_id = info.get("id")
        elif info.get("id") != self.timer_id:
            # Addresses get handed to other devices: never take another Timer's laps.
            raise OftError("%s is Timer %s, not %s" % (self.url, info.get("id"), self.timer_id))
        boot_id = info.get("boot_id")
        if boot_id != self.boot_id:
            # New Timer process: its sequence numbers and clock start over.
            self.boot_id = boot_id
            self.last_seq = 0
            self.clock.reset()
        self._seat_levels = {}
        self._drop_stream()  # a claim ends with its stream: claim again on the new one
        self._next_stream = 0.0
        self._learn_node_levels(info)
        # Which Node serves each seat, so the first config we send is in the right unit.
        self._poll_status()
        self.connected = True
        self.ready_failure_msg = None
        self._config_dirty = True  # make sure the Timer has RH's frequencies
        self.log("Connected to OFT Timer %s (%s, level %s)" % (info.get("id"), info.get("name"), info.get("level")))
        for node in self.nodes:
            node.firmware_version_str = "oft %s" % info.get("fw", "?")

    # -- level units ------------------------------------------------------------------

    def _learn_node_levels(self, info):
        self.timer_info = info
        nodes = info.get("nodes") or []
        self._node_levels = {n.get("id"): LevelMap.from_info(n.get("level")) for n in nodes}
        self._default_level = LevelMap.from_info(nodes[0].get("level")) if nodes else LevelMap()
        for level in self._node_levels.values():
            if not level.known:
                self.log("OFT Node reports levels in %r, which RotorHazard cannot show; using the raw numbers" % level.unit)

    def level_map(self, seat):
        """The unit of the Node serving ``seat``."""
        return self._seat_levels.get(seat, self._default_level)

    def _set_seat_node(self, seat, node_id):
        if node_id is not None and node_id not in self._node_levels:
            now = self._now()
            if now >= self._next_info:  # a Node that joined after we read /info
                self._next_info = now + INFO_RETRY_S
                self._learn_node_levels(self.client.info())
        level = self._node_levels.get(node_id, self._default_level)
        if self.level_map(seat) != level and seat in self._rh_levels_set:
            self._config_dirty = True  # its thresholds were sent in the wrong unit
        self._seat_levels[seat] = level

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
            if "peak" in c:
                node.pass_peak_rssi = self.level_map(seat).to_rh(int(c["peak"]))
            node.enter_at_timestamp = self.clock.to_local(int(c["t_enter"])) if "t_enter" in c else 0
            node.exit_at_timestamp = self.clock.to_local(int(c["t_exit"])) if "t_exit" in c else 0
            node.node_lap_id = seq
            if callable(self.pass_record_callback):
                self.pass_record_callback(node, lap_ts, BaseHardwareInterface.LAP_SOURCE_REALTIME, peak=node.pass_peak_rssi)

    def _poll_status(self):
        st = self.client.status()
        self._status_holder = st.get("control")
        cross_list = []
        readtime = self._now()
        for s in st.get("seats", []):
            seat = int(s["seat"])
            if seat >= len(self.nodes):
                continue
            node = self.nodes[seat]
            self._set_seat_node(seat, s.get("node"))
            if s.get("rssi") is not None:
                node.current_rssi = self.level_map(seat).to_rh(int(s["rssi"]))
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

    def _manual_threshold(self, seat):
        node, level = self.nodes[seat], self.level_map(seat)
        return {"mode": "manual", "enter": level.from_rh(node.enter_at_level), "exit": level.from_rh(node.exit_at_level)}

    def _push_config(self, now=None):
        now = self._now() if now is None else now
        if self._control_state in ("other", "wait") or now < self._next_write:
            return  # not ours to change now: what RotorHazard wants stays pending
        cfg = self.client.get_config()
        caps = ((self.timer_info or {}).get("capabilities") or {}).get("config") or {}
        per_seat = bool(caps.get("seat_thresholds"))
        seats = []
        # The seat is this interface's own node number: RotorHazard renumbers node.index
        # across every interface, so it is not the seat.
        for seat, node in enumerate(self.nodes):
            entry = {"seat": seat, "freq_mhz": int(node.frequency) if node.frequency else 5800, "enabled": bool(node.frequency)}
            if per_seat and seat in self._rh_levels_set:
                entry["threshold"] = self._manual_threshold(seat)
            seats.append(entry)
        cfg["seats"] = seats
        if not per_seat and self._rh_levels_set:
            # This Timer has one threshold for every seat: use the first seat's.
            cfg["threshold"] = self._manual_threshold(min(self._rh_levels_set))
        try:
            self.client.put_config(cfg)
        except OftControlled as e:
            # Refused, and nothing changed. The stream says when control is ours again;
            # without a stream, ask again now and then.
            self._next_write = now + CONTROL_RETRY_S
            holder = (self._status_holder or {}).get("name")
            self._set_controlled_by(holder or str(e).replace("controlled by ", "", 1))
            return
        self._config_dirty = False
        if self._control_state != "other":
            self._set_controlled_by(None)

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
        self.nodes[node_index].enter_at_level = level
        self._rh_levels_set.add(node_index)
        self._config_dirty = True

    def set_exit_at_level(self, node_index, level):
        self.nodes[node_index].exit_at_level = level
        self._rh_levels_set.add(node_index)
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

    # RotorHazard calls these on every interface, and its base class does not have them.

    def close_fwupd_serial_port(self):
        pass

    def get_intf_total_error_count(self):
        return 0

    def set_intf_error_report_percent_limit(self, _percent):
        pass

    # -- diagnostics ---------------------------------------------------------------------

    def oft_summary(self):
        return {
            "url": self.url,
            "id": self.timer_id,
            "connected": self.connected,
            "timer": (self.timer_info or {}).get("id"),
            "clock_synced": self.clock.synced,
            "clock_error_ms": None if self.clock.error_estimate_s() is None else round(self.clock.error_estimate_s() * 1000, 2),
            "last_seq": self.last_seq,
            "controlled_by": self.controlled_by,
        }
