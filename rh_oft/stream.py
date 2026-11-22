"""The Timer's WebSocket stream, used only to claim control (spec §6.6).

A Timer lets one client at a time change it. A client says it means to write by claiming
on its stream, and the claim lasts as long as the stream stays open. Everything else the
plugin needs it still reads over HTTP, so this stream never subscribes to anything.

Standard library only. Nothing here blocks: :meth:`ControlStream.poll` reads whatever has
arrived and returns, so the interface calls it from its ordinary poll loop.
"""

import base64
import hashlib
import json
import os
import socket
import struct
from time import monotonic
from urllib.parse import urlparse

API_STREAM = "/oft/v1/stream"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 8, 9, 10

#: A ping goes out this often; this long with nothing heard means the stream is dead.
PING_S = 5.0
DEAD_S = 15.0


class StreamError(Exception):
    """The stream could not be opened or has ended."""


def accept_key(key):
    """The ``Sec-WebSocket-Accept`` value a server must answer ``key`` with."""
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def encode_frame(opcode, payload=b"", mask=True):
    """One whole WebSocket frame. Clients mask what they send; servers do not."""
    head = bytes([0x80 | opcode])
    size = len(payload)
    flag = 0x80 if mask else 0
    if size < 126:
        head += bytes([flag | size])
    elif size < 65536:
        head += bytes([flag | 126]) + struct.pack("!H", size)
    else:
        head += bytes([flag | 127]) + struct.pack("!Q", size)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class FrameReader:
    """Turns the bytes of a WebSocket connection into ``(opcode, payload)`` messages."""

    def __init__(self):
        self._buf = b""
        self._parts = None  # (opcode, [payload...]) of a message still arriving in pieces

    def feed(self, data):
        self._buf += data
        messages = []
        while True:
            frame = self._take()
            if frame is None:
                return messages
            fin, opcode, payload = frame
            if opcode >= 8:  # control frames are never split
                messages.append((opcode, payload))
            elif opcode != OP_CONT:
                self._parts = (opcode, [payload])
            elif self._parts:
                self._parts[1].append(payload)
            if fin and opcode < 8 and self._parts:
                messages.append((self._parts[0], b"".join(self._parts[1])))
                self._parts = None

    def _take(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        size = buf[1] & 0x7F
        pos = 2
        if size == 126:
            if len(buf) < 4:
                return None
            size, pos = struct.unpack("!H", buf[2:4])[0], 4
        elif size == 127:
            if len(buf) < 10:
                return None
            size, pos = struct.unpack("!Q", buf[2:10])[0], 10
        key = None
        if buf[1] & 0x80:
            if len(buf) < pos + 4:
                return None
            key, pos = buf[pos:pos + 4], pos + 4
        if len(buf) < pos + size:
            return None
        payload = buf[pos:pos + size]
        if key:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        self._buf = buf[pos + size:]
        return bool(buf[0] & 0x80), buf[0] & 0x0F, payload


class ControlStream:
    """One stream to a Timer, holding this client's claim.

    After :meth:`connect`, call :meth:`poll` often. Then:

    - ``known`` is True once the Timer has said where control stands;
    - ``holder`` is the claim holding control, ``{"id", "name"}``, or None;
    - ``key`` is set while *this* stream holds control: send it with every write.
    """

    def __init__(self, base_url, name="RotorHazard", now=monotonic, timeout=1.0, key=None):
        self.base_url = base_url.rstrip("/")
        self.name = name
        #: The key this client held on its last stream. Sent with the claim, it takes the
        #: same claim up again if the Timer still keeps it (10 s after a stream drops).
        self.last_key = key
        self._now = now
        self.timeout = timeout
        self._sock = None
        self._reader = FrameReader()
        self._next_ping = 0.0
        self._last_heard = 0.0
        self.known = False
        self.holder = None
        self.claim = None
        self.key = None
        self.refused = None  # why the Timer would not take the claim, if it would not

    def connect(self):
        url = urlparse(self.base_url)
        host, port = url.hostname, url.port or 80
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (API_STREAM, host, port, key)
        )
        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
            sock.sendall(request.encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk:
                    raise StreamError("the Timer closed the stream while opening it")
                head += chunk
                if len(head) > 16384:
                    raise StreamError("the Timer's answer was not a WebSocket one")
        except OSError as e:
            raise StreamError("stream not opened: %s" % e) from e
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        if " 101" not in lines[0] or headers.get("sec-websocket-accept") != accept_key(key):
            sock.close()
            raise StreamError("the Timer did not open a stream: %s" % lines[0])
        self._sock = sock
        self._last_heard = self._now()
        self._next_ping = self._last_heard + PING_S
        self._handle(self._reader.feed(rest))

    @property
    def open(self):
        return self._sock is not None

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.known = False
        self.holder = self.claim = self.key = None

    def _send(self, opcode, payload=b""):
        try:
            self._sock.settimeout(self.timeout)
            self._sock.sendall(encode_frame(opcode, payload))
        except OSError as e:
            self.close()
            raise StreamError("stream lost: %s" % e) from e

    def send(self, message):
        self._send(OP_TEXT, json.dumps(message).encode())

    def poll(self):
        """Take in everything the Timer has sent. Raises :class:`StreamError` when the stream has ended."""
        if self._sock is None:
            raise StreamError("stream not open")
        now = self._now()
        while True:
            try:
                self._sock.settimeout(0)
                data = self._sock.recv(65536)
            except (BlockingIOError, InterruptedError, socket.timeout):
                break
            except OSError as e:
                self.close()
                raise StreamError("stream lost: %s" % e) from e
            if not data:
                self.close()
                raise StreamError("the Timer closed the stream")
            self._last_heard = now
            self._handle(self._reader.feed(data))
            if self._sock is None:
                raise StreamError("the Timer closed the stream")
        if now - self._last_heard > DEAD_S:
            self.close()
            raise StreamError("nothing heard on the stream for %d s" % DEAD_S)
        if now >= self._next_ping:
            self._next_ping = now + PING_S
            self._send(OP_PING)

    def _handle(self, messages):
        for opcode, payload in messages:
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
            elif opcode == OP_CLOSE:
                self.close()
                return
            elif opcode == OP_TEXT:
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._message(message)

    def _message(self, message):
        kind = message.get("type")
        if kind == "hello":
            # Claims end with their stream, so every new stream claims again.
            claim = {"type": "control", "claim": self.name}
            if self.last_key:
                claim["key"] = self.last_key  # one that matches nothing is ignored
            self.send(claim)
        elif kind == "control" and "holder" in message:
            self.holder = message.get("holder") or None
            self.claim = message.get("claim")
            self.key = message.get("key") or None
            self.known = True
            if self.claim is not None:
                self.refused = None
        elif kind == "error" and message.get("code") == "control_full":
            self.refused = message.get("msg") or "the Timer has too many claims"
