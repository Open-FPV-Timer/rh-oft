# rh-oft — Open FPV Timer seats in RotorHazard

A RotorHazard plugin that adds an [OFT](../oft-spec/oft-spec.md) Timer (for example the B1
single-pilot PoE gate) as timing seats next to the Pi's own nodes. RotorHazard keeps owning
races, pilots and laps; the plugin is a translator.

- Laps carry the **Timer's timestamp** (`t_mid`), aligned to RotorHazard's clock by an
  NTP-style `/time` exchange that trusts only the fastest round trips. Network delay changes
  when a lap appears, never its time.
- RotorHazard's **frequency** and **EnterAt / ExitAt** per seat are forwarded to the Timer's
  `/config` (manual thresholds, in 0.1 dB).
- Live signal from `/status` feeds RotorHazard's RSSI display and crossing indicator.
- Works with `oftd --fake` for development, with no hardware.

## Install

1. Copy `rh_oft/` and `manifest.json` into RotorHazard's plugin directory as
   `~/rh-data/plugins/rh_oft/` (so `~/rh-data/plugins/rh_oft/__init__.py` exists).
2. Restart RotorHazard. In **Settings → OFT Timer**, set the Timer URL
   (`http://<ip>:7270`) and the number of seats, then restart again.
3. The OFT seats appear after the Pi's nodes. Set their frequencies as usual.

Requires RotorHazard 4.4+ (RHAPI 1.4). Uses only the Python standard library.

## Test

```sh
make test          # unittest against a fake OFT Timer; no RotorHazard install needed
```

The tests stub RotorHazard's `Node`, `BaseHardwareInterface` and `gevent` (in
`tests/stubs/`) and run a fake Timer on `http.server`.

## Units

OFT power is 0.1 dB. RotorHazard's UI expects RX5808-style integers, so the plugin maps
linearly: `rh = (ddb + 1200) / 8`, clamped to 1–255 (noise floor −92 dB → 35, a close pass
−60 dB → 75). The inverse is used for EnterAt / ExitAt.

GPL-3.0-or-later.
