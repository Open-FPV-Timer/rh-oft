"""A fake OFT Timer: just enough of the HTTP side of the OFT Protocol for the plugin tests.

Runs ``http.server`` in a thread. Its clock is ``monotonic()`` shifted by ``offset_s``
and expressed in µs, so a test knows the exact local time of every crossing it injects.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic
from urllib.parse import parse_qs, urlparse


class FakeTimer:
    def __init__(self, offset_s=-1234.5678, boot_id="deadbeef", latency_s=0.0):
        self.offset_s = offset_s
        self.boot_id = boot_id
        self.latency_s = latency_s
        self.crossings = []  # dicts with seq, seat, t_mid...
        self.config = {
            "seats": [{"seat": 0, "freq_mhz": 5800, "enabled": True}],
            "threshold": {"mode": "auto", "snr_enter": 150, "hysteresis": 50},
            "min_lap_ms": 2000,
        }
        self.status_seats = [{"seat": 0, "rssi": -915, "floor": -920, "enter": -770, "exit": -820, "crossing": False, "rate_hz": 1000}]
        self.puts = []
        self.requests = []
        self.fail = False  # when True, refuse connections (simulate the gate unplugged)
        self._server = None
        self._thread = None
        self.lock = threading.Lock()

    # -- clock ----------------------------------------------------------------------------

    def timer_us(self, local_s=None):
        return int(((monotonic() if local_s is None else local_s) + self.offset_s) * 1e6)

    def add_crossing(self, local_mid_s, seat=0, peak=-620, width_s=0.25):
        with self.lock:
            seq = len(self.crossings) + 1
            self.crossings.append({
                "type": "crossing", "seq": seq, "seat": seat,
                "t_enter": self.timer_us(local_mid_s - width_s / 2),
                "t_exit": self.timer_us(local_mid_s + width_s / 2),
                "t_mid": self.timer_us(local_mid_s),
                "peak": peak, "snr": peak + 920, "flags": [],
            })
            return seq

    def reboot(self, new_boot_id, new_offset_s):
        with self.lock:
            self.boot_id = new_boot_id
            self.offset_s = new_offset_s
            self.crossings = []

    # -- server ------------------------------------------------------------------------

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if fake.fail:
                    self.close_connection = True
                    return
                if fake.latency_s:
                    import time
                    time.sleep(fake.latency_s)
                u = urlparse(self.path)
                fake.requests.append(("GET", u.path))
                with fake.lock:
                    if u.path == "/oft/v1/info":
                        self._send(200, {"id": "oft-fake", "name": "Fake", "boot_id": fake.boot_id, "proto": 1, "level": "L3", "fw": "0.1.0", "capabilities": {"crossings": {}}})
                    elif u.path == "/oft/v1/time":
                        self._send(200, {"time_us": fake.timer_us(), "boot_id": fake.boot_id})
                    elif u.path == "/oft/v1/status":
                        self._send(200, {"type": "status", "seats": fake.status_seats, "clock_synced": True})
                    elif u.path == "/oft/v1/config":
                        self._send(200, fake.config)
                    elif u.path == "/oft/v1/crossings":
                        since = int(parse_qs(u.query).get("since", ["0"])[0])
                        self._send(200, {"boot_id": fake.boot_id, "crossings": [c for c in fake.crossings if c["seq"] > since]})
                    else:
                        self._send(404, {"error": "no such path"})

            def do_PUT(self):
                if fake.fail:
                    self.close_connection = True
                    return
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.requests.append(("PUT", urlparse(self.path).path))
                with fake.lock:
                    if urlparse(self.path).path == "/oft/v1/config":
                        fake.config = body
                        fake.puts.append(body)
                        self._send(200, body)
                    else:
                        self._send(404, {"error": "no such path"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
