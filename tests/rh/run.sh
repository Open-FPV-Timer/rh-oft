#!/bin/sh
# Run the plugin inside a real RotorHazard (Docker) against `oftd --fake`.
#
#   RH_IMAGE  RotorHazard image with mock nodes  (default gridfpv-rotorhazard:4.4.0)
#   OFTD      path to the oftd binary            (default ../oft/target/debug/oftd)
#
# The Timer listens on port 7270 of this machine; the container reaches it as oft.local,
# the plugin's default address.
set -eu
cd "$(dirname "$0")/../.."
RH_IMAGE=${RH_IMAGE:-gridfpv-rotorhazard:4.4.0}
OFTD=${OFTD:-../oft/target/debug/oftd}
NAME=rh-oft-test
WORK=$(mktemp -d)

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    [ -n "${OFTD_PID:-}" ] && kill "$OFTD_PID" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

[ -x "$OFTD" ] || { echo "no oftd at $OFTD: run 'cargo build -p oftd' in ../oft, or set OFTD"; exit 2; }
mkdir -p "$WORK/plugins/rh_oft"
cp rh_oft/*.py manifest.json "$WORK/plugins/rh_oft/"
chmod -R a+rX "$WORK"

"$OFTD" --fake --no-mdns --fake-lap-s 5 --id oft-rhtest >"$WORK/oftd.log" 2>&1 &
OFTD_PID=$!
sleep 1
# Two seats, so the plugin (Seats = 0) adds two.
curl -fsS -o /dev/null -X PUT http://127.0.0.1:7270/oft/v1/config -H 'Content-Type: application/json' -d '{
  "seats": [{"seat": 0, "freq_mhz": 5658, "enabled": true}, {"seat": 1, "freq_mhz": 5732, "enabled": true}],
  "threshold": {"mode": "auto", "snr_enter": 150, "hysteresis": 50}, "min_lap_ms": 2000}'

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --add-host oft.local:host-gateway -e RH_NODES=2 \
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
