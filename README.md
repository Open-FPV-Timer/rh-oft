# rh-oft — Open FPV Timer seats in RotorHazard

A RotorHazard plugin that adds [OFT](../oft-spec/oft-spec.md) Timers (for example the B1
single-pilot PoE gate) as timing seats next to the Pi's own nodes. It follows OFT spec v0.9. RotorHazard keeps owning
races, pilots and laps; the plugin is a translator.

- Laps carry the **Timer's timestamp** (`t_mid`), aligned to RotorHazard's clock by an
  NTP-style `/time` exchange that trusts only the fastest round trips. Network delay changes
  when a lap appears, never its time.
- RotorHazard's **frequency** and **EnterAt / ExitAt** are forwarded to the Timer's
  `/config`, each seat with its own manual threshold. A Timer that does not announce
  `capabilities.config.seat_thresholds` gets the first seat's levels for every seat.
- **Several Timers** at once: each Timer's seats become RotorHazard nodes, and each Timer
  keeps its own clock alignment.
- **Find Timers** lists the Timers on the network (mDNS, `_oft._tcp`); you add the ones
  you want. A Timer is remembered by its **id**, so it is found again when its address
  changes.
- Live signal from `/status` feeds RotorHazard's RSSI display and crossing indicator.
- Works with `oftd --fake` for development, with no hardware.

## Install

1. Copy `rh_oft/` and `manifest.json` into RotorHazard's plugin directory as
   `~/rh-data/plugins/rh_oft/` (so `~/rh-data/plugins/rh_oft/__init__.py` exists).
2. Restart RotorHazard. Nothing is timed yet: add your Timers first (below).

### Adding Timers (Settings → OFT Timers)

1. Press **Find Timers**. The panel lists the Timers on the network with their ids,
   addresses and seat counts.
2. Pick one under **Timers found** and press **Add the Timer found**. Repeat for each
   Timer you want. For a Timer that cannot be found (another network, or RotorHazard in a
   container), type its address and press **Add the Timer at the address**.
3. Restart RotorHazard. It fixes its nodes when it starts, so added and removed Timers
   take effect then; the panel says which are waiting for a restart.

The OFT seats are RotorHazard's first nodes, ahead of the Pi's own (plugins load first),
in the order the Timers were added. Each Timer gets as many nodes as its `/config` has
seats when RotorHazard starts, so set the seat count on the Timer. A Timer that is off at
startup keeps the seats it had last time, and connects when it comes back.

**A Timer is known by its id, not its address.** When RotorHazard starts, and whenever a
Timer stops answering, the plugin looks it up again by id and follows it to its new
address. If another Timer answers at the old address, it is refused. The list is kept in
RotorHazard's config file (`RH_OFT` → `TIMERS`).

In a container, finding Timers needs `--network host`: mDNS does not cross Docker's
bridge network. Without it, add Timers by address; they are then not followed when their
address changes. `RH_OFT_URL` (addresses separated by commas) adds Timers without the
panel, for tests.

Requires RotorHazard 4.4+ (RHAPI 1.4). Uses only the Python standard library.

## Who may change a Timer (control)

A Timer lets one client at a time change it (OFT spec §6.6), so RotorHazard and the
Timer's own page cannot undo each other's settings. The plugin handles this by itself:

- It claims control when it connects, under the name `RotorHazard <hostname>`.
- If someone takes control on the Timer's page, RotorHazard stops writing and says so,
  in a message and in the OFT Timers panel. Frequencies and EnterAt / ExitAt levels you
  set meanwhile are kept, not lost.
- When control comes back, the plugin sends the Timer all of RotorHazard's settings
  again, since the other client may have changed them.
- It never takes control back by itself. Give it back on the Timer's page.
- A short network drop does not lose control: the Timer keeps the claim for 10 s.

Laps, RSSI and the crossing indicator keep working whoever holds control. A Timer whose
firmware has no control behaves as before.

## Test

```sh
make test          # unittest against a fake OFT Timer; no RotorHazard install needed
```

The tests stub RotorHazard's `Node`, `BaseHardwareInterface` and `gevent` (in
`tests/stubs/`) and run a fake Timer on `http.server`.

```sh
make rh-test       # the plugin inside a real RotorHazard, in Docker
```

This starts two `oftd --fake` Timers (built in `../oft`), one with two seats and one with
one, and a RotorHazard container with the plugin mounted. It then drives RotorHazard over
socket.io: it sets frequencies and EnterAt / ExitAt, runs a race and checks that every
node's lap times equal its Timer's crossing times. A second run on the host's network
works the settings panel: it finds both Timers, adds them, restarts RotorHazard, then moves
one Timer to another address and checks that it is followed. It needs a RotorHazard image with mock nodes; `RH_IMAGE` names it (default
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
