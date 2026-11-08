"""Align the Timer clock (µs since the Timer booted) with RotorHazard's ``monotonic()``.

NTP-style: for each ``/time`` exchange we assume the Timer read its clock at the midpoint
of the round trip. Only the fastest round trips are trusted, because a slow one means the
LAN or the server was busy, and its midpoint is anyone's guess.

Lap times come out as ``monotonic()`` seconds, which is what RotorHazard's
``pass_record_callback`` wants. Network delay therefore only affects *when* a lap shows
up, never *what time* it is given.
"""

from collections import deque


class ClockAlign:
    #: Observations kept; roughly the last minute at one per second.
    WINDOW = 64

    def __init__(self):
        self._obs = deque(maxlen=self.WINDOW)  # (timer_us, local_mid_s, rtt_s)
        self.offset_s = None  # local = timer_us / 1e6 + offset_s
        self.best_rtt_s = None
        self.boot_id = None

    @property
    def synced(self):
        return self.offset_s is not None

    def reset(self):
        self._obs.clear()
        self.offset_s = None
        self.best_rtt_s = None

    def add(self, t_send, t_recv, timer_us, boot_id=None):
        """Fold one ``/time`` exchange in. Returns True if it was used."""
        if boot_id is not None and boot_id != self.boot_id:
            # The Timer rebooted: its clock restarted, so every old observation is wrong.
            self.reset()
            self.boot_id = boot_id
        rtt = t_recv - t_send
        if rtt < 0:
            return False
        if self.best_rtt_s is None or rtt < self.best_rtt_s:
            self.best_rtt_s = rtt
        elif rtt > 2 * self.best_rtt_s + 0.002:
            # Much slower than the best we've seen: a stall, not a measurement.
            return False
        self._obs.append((timer_us, t_send + rtt / 2.0, rtt))
        self._refit()
        return True

    def _refit(self):
        # Weight each observation by how fast it was; the best quarter dominates.
        best = sorted(self._obs, key=lambda o: o[2])
        keep = best[: max(1, len(best) // 4)]
        self.offset_s = sum(mid - us / 1e6 for us, mid, _ in keep) / len(keep)

    def to_local(self, timer_us):
        """Timer µs -> ``monotonic()`` seconds. Requires ``synced``."""
        if self.offset_s is None:
            raise ValueError("clock not aligned yet")
        return timer_us / 1e6 + self.offset_s

    def error_estimate_s(self):
        """Half the best round trip: the largest alignment error we could have."""
        return None if self.best_rtt_s is None else self.best_rtt_s / 2.0
