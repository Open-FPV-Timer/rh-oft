# rh-oft — Open FPV Timer seats in RotorHazard

A RotorHazard plugin that adds an [OFT](../oft-spec/oft-spec.md) Timer (for example the B1
single-pilot PoE gate) as timing seats next to the Pi's own nodes. It follows OFT spec v0.8. RotorHazard keeps owning
races, pilots and laps; the plugin is a translator.

- Laps carry the **Timer's timestamp** (`t_mid`), aligned to RotorHazard's clock by an
  NTP-style `/time` exchange that trusts only the fastest round trips. Network delay changes
  when a lap appears, never its time.
- RotorHazard's **frequency** and **EnterAt / ExitAt** are forwarded to the Timer's
  `/config`, each seat with its own manual threshold. A Timer that does not announce
  `capabilities.config.seat_thresholds` gets the first seat's levels for every seat.
- Live signal from `/status` feeds RotorHazard's RSSI display and crossing indicator.
- Works with `oftd --fake` for development, with no hardware.

## Install

1. Copy `rh_oft/` and `manifest.json` into RotorHazard's plugin directory as
   `~/rh-data/plugins/rh_oft/` (so `~/rh-data/plugins/rh_oft/__init__.py` exists).
2. Restart RotorHazard. In **Settings → OFT Timer**, set the Timer URL
   (`http://<ip>:7270`), then restart again. **Seats** is 0 by default: the plugin adds as
   many seats as the Timer's `/config` has when RotorHazard starts (1 if the Timer cannot
   be reached). Set a number from 1 to 8 to fix it.
3. The OFT seats are RotorHazard's first nodes, ahead of the Pi's own (plugins load
   first). Set their frequencies as usual.

Requires RotorHazard 4.4+ (RHAPI 1.4). Uses only the Python standard library.

## Test

```sh
make test          # unittest against a fake OFT Timer; no RotorHazard install needed
```

The tests stub RotorHazard's `Node`, `BaseHardwareInterface` and `gevent` (in
`tests/stubs/`) and run a fake Timer on `http.server`.

```sh
make rh-test       # the plugin inside a real RotorHazard, in Docker
```

This starts `oftd --fake` (built in `../oft`) with two seats and a RotorHazard container
with the plugin mounted, then drives RotorHazard over socket.io: it sets frequencies and
EnterAt / ExitAt, runs a race and checks that the lap times equal the Timer's crossing
times. It needs a RotorHazard image with mock nodes; `RH_IMAGE` names it (default
`gridfpv-rotorhazard:4.4.0`). Last run against RotorHazard 4.4.0.

## Units

An OFT level is in the unit of the Node that measured it, which the Timer gives per Node
in `/info` (`nodes[].level`). RotorHazard's UI expects RX5808-style integers from 1 to
255, so the plugin converts per seat, by the unit of the Node serving it:

| Node unit | RotorHazard count | Example |
|---|---|---|
| `dB`, scale 0.1 (a C5 Node) | `(level + 1200) / 8` | floor −92 dB → 35, close pass −60 dB → 75 |
| `mV` (an RX5808 Node) | `mV / 6.5` | 850 mV → 131, about what a RotorHazard node shows |
| anything else | the number as given | logged once |

The inverse is used for EnterAt / ExitAt. If a seat's Node turns out to use another unit
than assumed (a Node that joins after the Timer started), its levels are sent again.

GPL-3.0-or-later.

## Acknowledgements

OFT is inspired by [FPVGate](https://github.com/LouisHitchcock/FPVGateC5MK), which showed
that an ESP32-C5 can time laps, and builds on [widscan58](https://github.com/CapnBry/widscan58),
esp-sdr, C5VRX and RotorHazard. The full list is in the
[oft README](https://github.com/Open-FPV-Timer/oft#acknowledgements).
