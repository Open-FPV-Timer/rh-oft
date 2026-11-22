import socket
import struct
import threading
import unittest

from rh_oft import mdns

INSTANCE = "oft-3fa2._oft._tcp.local."
HOST = "oft-3fa2.local."


def record(name, rtype, data):
    return name + struct.pack("!HHIH", rtype, 0x8001, 120, len(data)) + data


def response(with_details):
    """What a Timer answers: its name, and (maybe only when asked again) the rest."""
    service = mdns._encode_name(mdns.SERVICE)
    # The service name sits at offset 12; later names point back at it, as real answers do.
    pointer = b"\xc0\x0c"
    instance = b"\x08oft-3fa2" + pointer
    records = [record(pointer, mdns.TYPE_PTR, instance)]
    if with_details:
        txt = b"".join(bytes([len(kv)]) + kv for kv in (b"id=oft-3fa2", b"seats=2", b"proto=1", b"flag"))
        records += [
            record(instance, mdns.TYPE_SRV, struct.pack("!HHH", 0, 0, 7270) + mdns._encode_name(HOST)),
            record(instance, mdns.TYPE_TXT, txt),
            record(mdns._encode_name(HOST), mdns.TYPE_A, socket.inet_aton("192.168.1.50")),
            record(mdns._encode_name(HOST), 28, b"\x00" * 16),  # AAAA: skipped
        ]
    header = struct.pack("!6H", 0, 0x8400, 1, 1, 0, len(records) - 1)
    return header + service + struct.pack("!HH", mdns.TYPE_PTR, 1) + b"".join(records)


class FakeResponder:
    """A Timer's mDNS side on a loopback port: answers each query it gets."""

    def __init__(self, lazy=False):
        self.lazy = lazy  # answer the first question with the name only
        self.queries = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.address = self.sock.getsockname()
        self.running = True
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while self.running:
            try:
                data, sender = self.sock.recvfrom(9000)
            except OSError:
                continue
            self.queries.append(data)
            self.sock.sendto(response(with_details=not self.lazy or len(self.queries) > 1), sender)

    def stop(self):
        self.running = False
        self.thread.join()
        self.sock.close()


class MdnsTest(unittest.TestCase):
    def browse(self, responder):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            return mdns.discover(timeout=0.6, target=responder.address, sockets=[sock])
        finally:
            sock.close()
            responder.stop()

    def test_query_asks_for_the_oft_service(self):
        query = mdns.build_query([(mdns.SERVICE, mdns.TYPE_PTR)])
        self.assertEqual(query[12:], b"\x04_oft\x04_tcp\x05local\x00\x00\x0c\x00\x01")

    def test_finds_a_timer(self):
        found = self.browse(FakeResponder())
        self.assertEqual(len(found), 1)
        timer = found[0]
        self.assertEqual((timer.id, timer.url), ("oft-3fa2", "http://192.168.1.50:7270"))
        self.assertEqual(timer.txt, {"id": "oft-3fa2", "seats": "2", "proto": "1", "flag": ""})
        self.assertEqual(timer.describe(), "oft-3fa2 at http://192.168.1.50:7270 (2 seats)")

    def test_asks_again_for_what_the_first_answer_left_out(self):
        responder = FakeResponder(lazy=True)
        found = self.browse(responder)
        self.assertEqual([t.url for t in found], ["http://192.168.1.50:7270"])
        self.assertIn(b"\x08oft-3fa2", responder.queries[1], "the second query names the Timer")

    def test_nobody_answers(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.assertEqual(mdns.discover(timeout=0.15, target=("127.0.0.1", 9), sockets=[sock]), [])
        finally:
            sock.close()

    def test_rubbish_is_ignored(self):
        # One answer whose name is a pointer to itself, then a message cut short.
        with self.assertRaises((ValueError, IndexError, struct.error)):
            mdns.parse_records(struct.pack("!6H", 0, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c")
        with self.assertRaises((ValueError, IndexError, struct.error)):
            mdns.parse_records(response(with_details=True)[:60])
        self.assertEqual(mdns.parse_records(struct.pack("!6H", 0, 0x8400, 0, 0, 0, 0)), [])


if __name__ == "__main__":
    unittest.main()
