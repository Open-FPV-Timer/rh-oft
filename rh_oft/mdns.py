"""Find OFT Timers on the LAN: a small DNS-SD browser for ``_oft._tcp`` (spec §6.1).

Standard library only, since RotorHazard does not ship an mDNS package. It sends its
questions from an ordinary port, which makes every answer come straight back to that port
(a "legacy unicast" query, RFC 6762 §6.7), so nothing has to join the multicast group or
own port 5353.

Multicast does not cross a Docker bridge network: in a container this finds Timers only
with ``--network host``.
"""

import fcntl
import logging
import socket
import struct
from time import monotonic

logger = logging.getLogger(__name__)

SERVICE = "_oft._tcp.local."
MDNS_GROUP = ("224.0.0.251", 5353)

TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV = 1, 12, 16, 33
CLASS_IN = 1
SIOCGIFADDR = 0x8915  # Linux: the IPv4 address of a network interface


class FoundTimer:
    """One Timer that answered: where it is and what its TXT record says."""

    def __init__(self, instance, address, port, txt):
        self.instance = instance
        self.address = address
        self.port = port
        self.txt = txt

    @property
    def id(self):
        return self.txt.get("id") or self.instance.split(".")[0]

    @property
    def url(self):
        return "http://%s:%d" % (self.address, self.port)

    def describe(self):
        seats = self.txt.get("seats")
        return "%s at %s" % (self.id, self.url) + (" (%s seats)" % seats if seats else "")

    def __repr__(self):
        return "FoundTimer(%s)" % self.describe()


# -- DNS messages ---------------------------------------------------------------------


def _encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(questions):
    """A DNS query for ``[(name, type)]``."""
    msg = struct.pack("!6H", 0, 0, len(questions), 0, 0, 0)
    for name, qtype in questions:
        msg += _encode_name(name) + struct.pack("!2H", qtype, CLASS_IN)
    return msg


def _read_name(msg, pos):
    """The name at ``pos`` and the position after it, following compression pointers."""
    labels = []
    end = None
    for _ in range(64):  # a loop of pointers must not hang us
        length = msg[pos]
        if length & 0xC0 == 0xC0:
            if end is None:
                end = pos + 2
            pos = ((length & 0x3F) << 8) | msg[pos + 1]
        elif length == 0:
            return ".".join(labels) + ".", (pos + 1 if end is None else end)
        else:
            labels.append(msg[pos + 1:pos + 1 + length].decode("utf-8", "replace"))
            pos += 1 + length
    raise ValueError("name too long")


def parse_records(msg):
    """Every record in a DNS message as ``(name, type, value)``; unknown types are skipped.

    Values: PTR a name, SRV ``(target, port)``, TXT a dict, A a dotted address.
    """
    _, flags, questions, answers, authority, additional = struct.unpack("!6H", msg[:12])
    pos = 12
    for _ in range(questions):
        _, pos = _read_name(msg, pos)
        pos += 4
    records = []
    for _ in range(answers + authority + additional):
        name, pos = _read_name(msg, pos)
        rtype, _, _, length = struct.unpack("!HHIH", msg[pos:pos + 10])
        pos += 10
        data_end = pos + length
        if rtype == TYPE_PTR:
            records.append((name, rtype, _read_name(msg, pos)[0]))
        elif rtype == TYPE_SRV:
            port = struct.unpack("!H", msg[pos + 4:pos + 6])[0]
            records.append((name, rtype, (_read_name(msg, pos + 6)[0], port)))
        elif rtype == TYPE_TXT:
            txt = {}
            at = pos
            while at < data_end:
                size = msg[at]
                key, _, value = msg[at + 1:at + 1 + size].decode("utf-8", "replace").partition("=")
                if key:
                    txt[key] = value
                at += 1 + size
            records.append((name, rtype, txt))
        elif rtype == TYPE_A and length == 4:
            records.append((name, rtype, socket.inet_ntoa(msg[pos:data_end])))
        pos = data_end
    return records


# -- browsing -------------------------------------------------------------------------


def _interface_addresses():
    """The IPv4 address of every network interface but loopback (Linux)."""
    addresses = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(probe.fileno(), SIOCGIFADDR, struct.pack("256s", name.encode()[:15]))
            except OSError:
                continue  # no IPv4 address on it
            address = socket.inet_ntoa(packed[20:24])
            if not address.startswith("127."):
                addresses.append(address)
    except OSError:
        pass
    finally:
        probe.close()
    return addresses


def _open_sockets():
    """One socket per network interface, so the question goes out on all of them."""
    sockets = []
    for address in _interface_addresses():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            sock.bind((address, 0))
        except OSError:
            sock.close()
            continue
        sockets.append(sock)
    if not sockets:  # could not list interfaces: let the system pick one
        sockets.append(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    return sockets


def discover(timeout=2.0, target=MDNS_GROUP, sockets=None, service=SERVICE):
    """Timers that answer within ``timeout`` seconds, sorted by id.

    Asks three times, each time also for whatever is still missing (a Timer may answer
    the first question with its name only).
    """
    own = sockets is None
    sockets = _open_sockets() if own else sockets
    instances = set()
    srv, txt, addresses, heard_from = {}, {}, {}, {}
    rounds = 3
    try:
        for attempt in range(rounds):
            questions = [(service, TYPE_PTR)]
            for instance in sorted(instances):
                if instance not in srv:
                    questions.append((instance, TYPE_SRV))
                if instance not in txt:
                    questions.append((instance, TYPE_TXT))
            for host, _ in srv.values():
                if host not in addresses:
                    questions.append((host, TYPE_A))
            query = build_query(questions)
            for sock in sockets:
                try:
                    sock.sendto(query, target)
                except OSError as e:
                    logger.debug("mDNS query not sent: %s", e)
            round_end = monotonic() + timeout / rounds
            while True:
                left = round_end - monotonic()
                if left <= 0:
                    break
                for sock in sockets:
                    sock.settimeout(max(0.01, left / len(sockets)))
                    try:
                        data, sender = sock.recvfrom(9000)
                    except OSError:  # timed out
                        continue
                    try:
                        records = parse_records(data)
                    except (ValueError, IndexError, struct.error):
                        continue  # not a DNS message we can read
                    for name, rtype, value in records:
                        if rtype == TYPE_PTR and name.lower() == service.lower():
                            instances.add(value)
                            heard_from.setdefault(value, sender[0])
                        elif rtype == TYPE_SRV:
                            srv[name] = value
                            heard_from.setdefault(name, sender[0])
                        elif rtype == TYPE_TXT:
                            txt[name] = value
                        elif rtype == TYPE_A:
                            # A Timer may list several addresses: prefer the one it answered from.
                            if name not in addresses or value == sender[0]:
                                addresses[name] = value
    finally:
        if own:
            for sock in sockets:
                sock.close()

    found = []
    for instance in instances:
        if instance not in srv:
            continue  # never told us its port
        host, port = srv[instance]
        address = addresses.get(host) or heard_from.get(instance)
        if address:
            found.append(FoundTimer(instance, address, port, txt.get(instance, {})))
    return sorted(found, key=lambda t: (t.id, t.url))
