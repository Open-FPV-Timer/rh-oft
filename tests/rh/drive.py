"""Runs inside the RotorHazard container: tune the OFT seats, fly a race, check the laps.

RotorHazard is driven over socket.io like its own web page would. The Timer is
two ``oftd --fake``, each with one simulated pilot per seat. Exits non-zero on the first failure.
"""
import json
import sys
import time
import urllib.request

import socketio

RACE_S = 30
#: RotorHazard node -> (Timer, seat on it): the Timers in URL order, two seats then one.
NODES = [("http://oft.local:7270", 0), ("http://oft.local:7270", 1), ("http://oft.local:7271", 0)]
SEATS = len(NODES)
TIMERS = sorted({timer for timer, _ in NODES})


def oft(timer, path):
    return json.loads(urllib.request.urlopen(timer + "/oft/v1" + path, timeout=3).read())


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        sys.exit(1)


sio = socketio.Client()
seen = {}
for event in ("heartbeat", "current_laps", "node_crossing_change"):
    sio.on(event, lambda data, event=event: seen.setdefault(event, []).append(data))
sio.connect("http://localhost:5000", wait_timeout=10)  # the first client starts RH's interfaces
time.sleep(3)

# The plugin's seats come first: plugins load before RotorHazard's own nodes.
freqs = seen["heartbeat"][-1]["frequency"]
check(len(freqs) > SEATS, "RotorHazard has %d nodes: %d OFT seats from %d Timers beside its own" % (len(freqs), SEATS, len(TIMERS)))

levels = {0: (60, 50), 1: (64, 52), 2: (62, 51)}  # RH counts; (count * 8 - 1200) tenths of a dB on the Timer
sio.emit("set_frequency", {"node": 0, "frequency": 5695})
for node, (enter, exit_) in levels.items():
    sio.emit("set_enter_at_level", {"node": node, "enter_at_level": enter})
    sio.emit("set_exit_at_level", {"node": node, "exit_at_level": exit_})
time.sleep(2)
configs = {timer: oft(timer, "/config") for timer in TIMERS}
check(configs[NODES[0][0]]["seats"][0]["freq_mhz"] == 5695, "frequency reaches the Timer")
for node, (enter, exit_) in levels.items():
    timer, seat = NODES[node]
    want = {"mode": "manual", "enter": enter * 8 - 1200, "exit": exit_ * 8 - 1200}
    check(configs[timer]["seats"][seat].get("threshold") == want, "node %d (%s seat %d) has its own EnterAt / ExitAt: %s" % (node + 1, timer, seat, want))
rssi = seen["heartbeat"][-1]["current_rssi"]
check(all(20 < rssi[seat] < 120 for seat in range(SEATS)), "live RSSI shows on the OFT seats: %s" % rssi[:SEATS])

first_seq = {timer: max([c["seq"] for c in oft(timer, "/crossings?since=0")["crossings"]] or [0]) for timer in TIMERS}
sio.emit("discard_laps")
time.sleep(1)
sio.emit("stage_race")
time.sleep(RACE_S)
sio.emit("stop_race")
time.sleep(2)
sio.emit("load_data", {"load_types": ["current_laps"]})
time.sleep(1)
laps = seen["current_laps"][-1]["current"]["node_index"]
crossings = {timer: oft(timer, "/crossings?since=%d" % first_seq[timer])["crossings"] for timer in TIMERS}

check(len(seen.get("node_crossing_change", [])) > 0, "crossing indicator fires")
for node, (timer, seat) in enumerate(NODES):
    t_mid = [c["t_mid"] for c in crossings[timer] if c["seat"] == seat]
    gaps = [(b - a) / 1000 for a, b in zip(t_mid, t_mid[1:])]
    rh = [lap["lap_time"] for lap in laps[node]["laps"] if not lap.get("deleted")][1:]  # drop the holeshot
    check(len(rh) >= 2, "node %d: RotorHazard recorded %d laps" % (node + 1, len(rh)))
    # RotorHazard's laps are a run of consecutive Timer crossings: find where it starts.
    start = min(range(len(gaps) - len(rh) + 1), key=lambda k: sum(abs(g - r) for g, r in zip(gaps[k:], rh)))
    worst = max(abs(g - r) for g, r in zip(gaps[start:], rh))
    check(worst < 1.0, "node %d: lap times equal its Timer's crossing times (worst %.3f ms)" % (node + 1, worst))
for node in range(SEATS, len(laps)):
    check(not laps[node]["laps"], "RotorHazard's own node %d got no OFT laps" % (node + 1))
sio.disconnect()
print("PASS")
