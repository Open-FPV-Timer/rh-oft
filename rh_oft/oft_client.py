"""A small, dependency-free client for the OFT Protocol (spec §6), HTTP only.

RotorHazard monkey-patches sockets with gevent at startup, so the blocking ``urllib`` calls
here cooperate with its greenlets. Outside RotorHazard they are just blocking calls.
"""

import json
import urllib.error
import urllib.request
from time import monotonic

API = "/oft/v1"


class OftError(Exception):
    """The Timer could not be reached or answered with an error."""


class OftControlled(OftError):
    """Another client holds control of the Timer, so it refused this write (423, spec §6.6)."""


class OftClient:
    def __init__(self, base_url, timeout=0.5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        #: The key of the claim that holds control, sent with every write (spec §6.6).
        self.control_key = None

    # -- raw -------------------------------------------------------------------------

    def _request(self, method, path, body=None):
        url = self.base_url + API + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method != "GET" and self.control_key:
            headers["OFT-Control"] = self.control_key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 423:
                try:
                    why = json.loads(e.read()).get("error")
                except (ValueError, AttributeError, OSError):
                    why = None
                e.close()
                raise OftControlled(why or "controlled by another client") from e
            raise OftError("%s %s -> HTTP %s" % (method, path, e.code)) from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise OftError("%s %s -> %s" % (method, path, e)) from e
        try:
            return json.loads(raw) if raw else None
        except ValueError as e:
            raise OftError("%s %s -> bad JSON" % (method, path)) from e

    # -- endpoints ---------------------------------------------------------------------

    def info(self):
        return self._request("GET", "/info")

    def status(self):
        return self._request("GET", "/status")

    def get_config(self):
        return self._request("GET", "/config")

    def put_config(self, config):
        return self._request("PUT", "/config", config)

    def crossings(self, since):
        return self._request("GET", "/crossings?since=%d" % since)

    def time(self):
        """One clock observation: (local send time, local receive time, timer µs, boot_id).

        Local times are ``monotonic()`` seconds, the clock RotorHazard stamps laps with.
        """
        t_send = monotonic()
        body = self._request("GET", "/time")
        t_recv = monotonic()
        return t_send, t_recv, int(body["time_us"]), body.get("boot_id")
