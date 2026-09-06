"""Device HTTP transport shared by the daemon, gallery runner and installers.

No Codex state or application configuration is loaded here. Interactive writes
are attempted once; the renderer retries the latest state on its next wake-up.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request


def local_opener(source_address: str | None = None):
    """Bypass system proxies and optionally bind the Bar's USB interface."""
    handlers = [urllib.request.ProxyHandler({})]
    if source_address:
        handlers.append(SourceAddressHTTPHandler(source_address))
    return urllib.request.build_opener(*handlers)


class HttpTransport:
    """Busy Bar HTTP API over any of its three routes. Remembers whether
    the device answers (device_ok, reported by GET /hub so a standby can
    step in when the hub cannot draw) and logs only on transitions."""

    TIMEOUT_S = 2.0

    def __init__(self, base: str, headers: dict | None = None, opener=None, logger=None):
        self.base = base
        self.headers = headers or {}
        self.opener = opener or local_opener()       # cloud: proxy-aware; usb/wifi: never
        self.logger = logger or (lambda _message: None)
        self.device_ok: bool | None = None   # None until the first draw/clear
        self.last_error = ""
        self.last_http_status: int | None = None

    def _note(self, ok: bool, err: str = ""):
        if ok:
            if self.device_ok is False:
                self.logger(f"device {self.base}: reachable again")
            self.device_ok, self.last_error = True, ""
        else:
            if err != self.last_error:
                self.logger(f"device {self.base}: {err}")
            self.device_ok, self.last_error = False, err

    def _request(self, method: str, path: str, body: bytes | None = None) -> bool:
        headers = dict(self.headers)
        if body:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=headers)
        try:
            with self.opener.open(req, timeout=self.TIMEOUT_S) as response:
                self.last_http_status = response.status
                self._note(True)
                return True
        except urllib.error.HTTPError as e:
            self.last_http_status = e.code
            if e.code == 409:   # reachable; an active focus session owns the screen
                self._note(True)
            else:
                self._note(False, f"HTTP {e.code} on {method} {path}")
            return False
        except OSError as e:
            self.last_http_status = None
            self._note(False, f"{type(e).__name__}: {e}"[:120])
            return False  # unplugged / offline; retried on the normal cadence

    def draw(self, payload: dict) -> bool:
        return self._request("POST", "/display/draw", json.dumps(payload).encode())

    def clear(self, app_name: str) -> bool:
        return self._request(
            "DELETE", "/display/draw?application_name=" + urllib.parse.quote(app_name)
        )

    def get_json(self, path: str) -> dict | None:
        req = urllib.request.Request(self.base + path, headers=self.headers)
        try:
            with self.opener.open(req, timeout=self.TIMEOUT_S) as r:
                return json.loads(r.read())
        except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
            return None

    def reachable(self) -> bool:
        """Cheap liveness check that never touches the canvas."""
        return self.get_json("/version") is not None


class SourceAddressHTTPHandler(urllib.request.HTTPHandler):
    """Keep USB device traffic off VPNs that also advertise 10.0.4.0/24."""

    def __init__(self, source_address: str):
        super().__init__()
        self.source_address = source_address

    def http_open(self, request):
        def connection(host, **kwargs):
            return http.client.HTTPConnection(
                host, source_address=(self.source_address, 0), **kwargs,
            )
        return self.do_open(connection, request)
