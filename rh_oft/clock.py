"""Align the Timer clock (µs since the Timer booted) with RotorHazard's ``monotonic()``.

NTP-style: for each ``/time`` exchange we assume the Timer read its clock at the midpoint
of the round trip. Only the fastest recent round trips are trusted, because a slow one means the
LAN or the server was busy, and its midpoint is anyone's guess.

The two clocks also run at slightly different speeds (a real Timer measured 70 ppm, which
is 70 µs every second), so the fit is a line, offset and rate, not one offset. Without
the rate a lap time could be wrong by a millisecond.

Lap times come out as ``monotonic()`` seconds, which is what RotorHazard's
``pass_record_callback`` wants. Network delay therefore only affects *when* a lap shows
up, never *what time* it is given.
"""

from collections import deque


class ClockAlign:
    #: Observations kept; roughly the last minute at one per second.
    WINDOW = 64
    #: A rate is fitted only from this many trusted observations spread over this long.
    RATE_MIN_OBS = 5
    RATE_MIN_SPAN_S = 10.0
    RATE_MAX = 500e-6

    def __init__(self):
        self._obs = deque(maxlen=self.WINDOW)  # (timer_us, local_mid_s, rtt_s)
        self.offset_s = None  # local = timer_s + offset_s + rate * (timer_s - _ref_s)
        self.rate = 0.0  # how much faster the local clock runs than the Timer's
        self._ref_s = 0.0
        self.best_rtt_s = None
        self.boot_id = None

    @property
    def synced(self):
        return self.offset_s is not None

    def reset(self):
        self._obs.clear()
        self.offset_s = None
        self.rate = 0.0
        self._ref_s = 0.0
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
        self._obs.append((timer_us, t_send + rtt / 2.0, rtt))
        self._refit()
        return rtt <= self._trusted_rtt()

    def _trusted_rtt(self):
        """The slowest round trip still believed: judged against the fastest *recent* one.

        Judged against the fastest ever, one lucky reply on Wi-Fi would shut out every
        later one, and the alignment would stop following the Timer's clock.
        """
        return 2 * self.best_rtt_s + 0.002

    def _refit(self):
        self.best_rtt_s = min(o[2] for o in self._obs)
        limit = self._trusted_rtt()
        # A line through (Timer time, local - Timer) for the fastest half, less any stalls.
        best = sorted(self._obs, key=lambda o: o[2])
        keep = [(us / 1e6, mid - us / 1e6) for us, mid, rtt in best[: max(1, len(best) // 2)] if rtt <= limit]
        self._ref_s = sum(t for t, _ in keep) / len(keep)
        self.offset_s = sum(d for _, d in keep) / len(keep)
        spread = sum((t - self._ref_s) ** 2 for t, _ in keep)
        span = max(t for t, _ in keep) - min(t for t, _ in keep)
        if len(keep) >= self.RATE_MIN_OBS and span >= self.RATE_MIN_SPAN_S:
            rate = sum((t - self._ref_s) * (d - self.offset_s) for t, d in keep) / spread
            # No crystal is off by more than this: a wilder fit is network noise.
            self.rate = max(-self.RATE_MAX, min(self.RATE_MAX, rate))
        else:
            self.rate = 0.0  # too little to tell a rate from jitter yet

    def to_local(self, timer_us):
        """Timer µs -> ``monotonic()`` seconds. Requires ``synced``."""
        if self.offset_s is None:
            raise ValueError("clock not aligned yet")
        timer_s = timer_us / 1e6
        return timer_s + self.offset_s + self.rate * (timer_s - self._ref_s)

    def error_estimate_s(self):
        """Half the best round trip: the largest alignment error we could have."""
        return None if self.best_rtt_s is None else self.best_rtt_s / 2.0
