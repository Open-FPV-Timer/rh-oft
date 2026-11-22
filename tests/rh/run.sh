#!/bin/sh
# Run the plugin inside a real RotorHazard (Docker) against two `oftd --fake` Timers.
#
#   RH_IMAGE  RotorHazard image with mock nodes  (default gridfpv-rotorhazard:4.4.0)
#   OFTD      path to the oftd binary            (default ../oft/target/debug/oftd)
#
# The Timers listen on ports 7270 (two seats) and 7271 (one seat) of this machine; the
# container reaches them as oft.local, through RH_OFT_URL. A second run on the host's
# network works the settings panel: find both by mDNS, add them, restart, then move one to
# another address (skipped if port 5000 is taken).
set -eu
cd "$(dirname "$0")/../.."
RH_IMAGE=${RH_IMAGE:-gridfpv-rotorhazard:4.4.0}
OFTD=${OFTD:-../oft/target/debug/oftd}
NAME=rh-oft-test
WORK=$(mktemp -d)

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    [ -n "${OFTD_PIDS:-}" ] && kill $OFTD_PIDS 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

[ -x "$OFTD" ] || { echo "no oftd at $OFTD: run 'cargo build -p oftd' in ../oft, or set OFTD"; exit 2; }
mkdir -p "$WORK/plugins/rh_oft"
cp rh_oft/*.py manifest.json "$WORK/plugins/rh_oft/"
chmod -R a+rX "$WORK"

"$OFTD" --fake --fake-lap-s 5 --id oft-rhtest-a >"$WORK/oftd-a.log" 2>&1 &
OFTD_PIDS=$!
"$OFTD" --fake --fake-lap-s 6 --id oft-rhtest-b --listen 0.0.0.0:7271 >"$WORK/oftd-b.log" 2>&1 &
OFTD_PIDS="$OFTD_PIDS $!"
sleep 1
# Two seats on the first Timer, so the plugin (Seats = 0) adds two for it and one for the other.
curl -fsS -o /dev/null -X PUT http://127.0.0.1:7270/oft/v1/config -H 'Content-Type: application/json' -d '{
  "seats": [{"seat": 0, "freq_mhz": 5658, "enabled": true}, {"seat": 1, "freq_mhz": 5732, "enabled": true}],
  "threshold": {"mode": "auto", "snr_enter": 150, "hysteresis": 50}, "min_lap_ms": 2000}'

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --add-host oft.local:host-gateway -e RH_NODES=2 \
    -e RH_OFT_URL=http://oft.local:7270,http://oft.local:7271 \
    -v "$WORK/plugins:/opt/RotorHazard/src/server/plugins:ro" "$RH_IMAGE" >/dev/null
echo "waiting for RotorHazard ($RH_IMAGE)..."
for _ in $(seq 60); do
    docker exec "$NAME" python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/', timeout=2)" 2>/dev/null && break
    sleep 1
done
docker logs "$NAME" 2>&1 | grep -q "Loaded external plugin 'rh_oft'" || { docker logs "$NAME" 2>&1 | tail -30; echo "FAIL  plugin did not load"; exit 1; }

docker cp tests/rh/drive.py "$NAME:/tmp/drive.py" >/dev/null
docker exec "$NAME" python3 /tmp/drive.py
if docker logs "$NAME" 2>&1 | grep -E "Traceback|OFT update failed"; then
    echo "FAIL  errors in the RotorHazard log"; exit 1
fi

# -- the settings panel: find, add, restart, and a Timer that changes address ----------
# Needs the host's network: multicast stops at a Docker bridge.
docker rm -f "$NAME" >/dev/null 2>&1
# Another RotorHazard on this machine would be the one the panel test talks to.
if python3 -c "import socket; socket.create_connection(('127.0.0.1', 5000), 1)" 2>/dev/null; then
    echo "skip  settings panel and mDNS: port 5000 is in use on this machine"
    exit 0
fi
wait_for_rh() {
    sleep 3
    for _ in $(seq 60); do
        docker exec "$NAME" python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/', timeout=2)" 2>/dev/null && return
        sleep 1
    done
    echo "FAIL  RotorHazard did not start"; exit 1
}
in_log() {
    if docker logs "$NAME" 2>&1 | grep -q "$1"; then echo "ok    log: $1"; else echo "FAIL  log: $1"; docker logs "$NAME" 2>&1 | grep -i oft | tail; exit 1; fi
}
docker run -d --name "$NAME" --network host -e RH_NODES=2 \
    -v "$WORK/plugins:/opt/RotorHazard/src/server/plugins:ro" "$RH_IMAGE" >/dev/null
wait_for_rh
in_log "no Timers added yet"
docker cp tests/rh/panel.py "$NAME:/tmp/panel.py" >/dev/null
docker exec "$NAME" python3 /tmp/panel.py add

docker restart "$NAME" >/dev/null
wait_for_rh
in_log "added 2 OFT seat(s) from oft-rhtest-a"
in_log "added 1 OFT seat(s) from oft-rhtest-b"
docker exec "$NAME" python3 /tmp/panel.py expect "**oft-rhtest-a** at" ":7270: connected, 2 seat(s)" ":7271: connected, 1 seat(s)"

# The second Timer comes back somewhere else (a new port here, a new IP on a real network).
kill "${OFTD_PIDS##* }"
"$OFTD" --fake --fake-lap-s 6 --id oft-rhtest-b --listen 0.0.0.0:7273 >"$WORK/oftd-b2.log" 2>&1 &
OFTD_PIDS="$OFTD_PIDS $!"
sleep 20
in_log "OFT Timer oft-rhtest-b moved from .*:7271 to .*:7273"
docker exec "$NAME" python3 /tmp/panel.py expect ":7273: connected, 1 seat(s)"
if docker logs "$NAME" 2>&1 | grep -E "Traceback|OFT update failed"; then
    echo "FAIL  errors in the RotorHazard log"; exit 1
fi
echo "PASS"
