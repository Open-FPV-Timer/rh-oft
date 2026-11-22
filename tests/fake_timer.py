"""A fake OFT Timer: just enough of the HTTP side of the OFT Protocol for the plugin tests.

Runs ``http.server`` in a thread. Its clock is ``monotonic()`` shifted by ``offset_s``
and expressed in µs, so a test knows the exact local time of every crossing it injects.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic
from urllib.parse import parse_qs, urlparse

from rh_oft.stream import OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, FrameReader, accept_key, encode_frame


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
        self.nodes = [{"id": "c5c5c5c5c5c5", "chip": "esp32c5", "fw": "0.1.0", "max_seats": 8, "level": {"unit": "dB", "scale": 0.1}}]
        self.seat_thresholds = True
        self.status_seats = [{"seat": 0, "node": "c5c5c5c5c5c5", "rssi": -915, "floor": -920, "enter": -770, "exit": -820, "crossing": False, "rate_hz": 1000}]
        # Control (spec 6.6). Off unless a test turns it on, like a Timer without the capability.
        self.control = False
        self.claims = []  # oldest first: {"id", "name", "send": callable or None}
        self.holder = None  # the id of the claim holding control
        self.key = None
        self.config_rev = 1
        self._next_claim = 1
        self.grace = False  # True: a claim outlives its stream until expire(), as for 10 s on a Timer
        self.away = set()  # claims whose stream dropped
        self.streams = []  # open sockets
        self._listeners = {}  # stream id -> send(message)
        self.stream_messages = []  # what clients sent on their streams
        self.puts = []
        self.refused = []  # writes answered 423
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

    # -- control ------------------------------------------------------------------------

    def _tell(self):
        """Tell every stream where control stands. Call with the lock held."""
        holder = next(({"id": c["id"], "name": c["name"]} for c in self.claims if c["id"] == self.holder), None)
        for send, claim_id in list(self._listeners.values()):
            mine = claim_id if any(c["id"] == claim_id for c in self.claims) else None
            message = {"type": "control", "holder": holder, "claim": mine}
            if holder and mine == holder["id"]:
                message["key"] = self.key
            send(message)

    def _hold(self, claim_id):
        self.holder = claim_id
        self.key = None if claim_id is None else os.urandom(4).hex()

    def add_claim(self, name, claim_id=None):
        """A claim, as if another client made it on its own stream. Returns its id."""
        with self.lock:
            if claim_id is None:
                claim_id = self._next_claim
                self._next_claim += 1
            self.claims.append({"id": claim_id, "name": name})
            if self.holder is None:  # the first claim holds control; later ones wait
                self._hold(claim_id)
            self._tell()
            return claim_id

    def end_claim(self, claim_id):
        with self.lock:
            self.claims = [c for c in self.claims if c["id"] != claim_id]
            if self.holder == claim_id:
                self._hold(self.claims[0]["id"] if self.claims else None)
            self._tell()

    def expire(self):
        """The 10 s are over: claims without a stream end."""
        for claim_id in list(self.away):
            self.away.discard(claim_id)
            self.end_claim(claim_id)

    def grant(self, claim_id):
        """POST /control {"grant": id or None}, which only the Timer's own page may send."""
        with self.lock:
            self._hold(claim_id)
            self._tell()

    def claim_named(self, name):
        with self.lock:
            return next((c["id"] for c in self.claims if c["name"] == name), None)

    def drop_streams(self):
        for sock in list(self.streams):
            try:
                sock.shutdown(2)
            except OSError:
                pass

    def _stream(self, handler):
        """Serve one WebSocket stream until it closes."""
        sock = handler.connection
        handler.send_response(101)
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept_key(handler.headers["Sec-WebSocket-Key"]))
        handler.end_headers()
        handler.wfile.flush()
        write = threading.Lock()

        def send(message):
            try:
                with write:
                    sock.sendall(encode_frame(OP_TEXT, json.dumps(message).encode(), mask=False))
            except OSError:
                pass

        with self.lock:
            stream_id = self._next_claim  # also the id of the claim this stream may make
            self._next_claim += 1
            self._listeners[stream_id] = [send, stream_id]
            self.streams.append(sock)
        send({"type": "hello", "proto": 1, "timer": "oft-fake", "boot_id": self.boot_id})
        with self.lock:
            self._tell()
        reader = FrameReader()
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                for opcode, payload in reader.feed(data):
                    if opcode == OP_PING:
                        with write:
                            sock.sendall(encode_frame(OP_PONG, payload, mask=False))
                    elif opcode == OP_CLOSE:
                        return
                    elif opcode == OP_TEXT:
                        message = json.loads(payload)
                        self.stream_messages.append(message)
                        mine = self._listeners[stream_id][1]
                        if message.get("type") == "control" and "claim" in message:
                            if message.get("key") and message["key"] == self.key and self.holder is not None:
                                with self.lock:  # the holder is back on a new stream
                                    self._listeners[stream_id][1] = self.holder
                                    self.away.discard(self.holder)
                                    self._tell()
                            elif not any(c["id"] == mine for c in self.claims):
                                self.add_claim(message["claim"], claim_id=mine)
                        elif message.get("type") == "control" and message.get("release"):
                            self.end_claim(mine)
        except OSError:
            pass
        finally:
            with self.lock:
                mine = self._listeners.pop(stream_id)[1]
                if sock in self.streams:
                    self.streams.remove(sock)
                still_used = any(claim == mine for _, claim in self._listeners.values())
            if self.grace and not still_used:
                self.away.add(mine)  # kept for a while: its client may come back
            elif not still_used:
                self.end_claim(mine)
            handler.close_connection = True

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
                if u.path == "/oft/v1/stream" and fake.control:
                    fake._stream(self)
                    return
                with fake.lock:
                    if u.path == "/oft/v1/info":
                        self._send(200, {"id": "oft-fake", "name": "Fake", "boot_id": fake.boot_id, "proto": 1, "level": "L3", "fw": "0.1.0", "nodes": fake.nodes, "capabilities": dict({"crossings": {}, "config": {"seat_thresholds": fake.seat_thresholds}}, **({"control": {}} if fake.control else {}))})
                    elif u.path == "/oft/v1/time":
                        self._send(200, {"time_us": fake.timer_us(), "boot_id": fake.boot_id})
                    elif u.path == "/oft/v1/status":
                        self._send(200, {"type": "status", "seats": fake.status_seats, "clock_synced": True, "config_rev": fake.config_rev,
                                         "control": next(({"id": c["id"], "name": c["name"]} for c in fake.claims if c["id"] == fake.holder), None)})
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
                    holder = next((c for c in fake.claims if c["id"] == fake.holder), None)
                    if holder and self.headers.get("OFT-Control") != fake.key:
                        fake.refused.append(body)
                        self._send(423, {"error": "controlled by %s" % holder["name"]})
                    elif urlparse(self.path).path == "/oft/v1/config":
                        fake.config = body
                        fake.config_rev += 1
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
