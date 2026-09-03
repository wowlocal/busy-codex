#!/usr/bin/env python3
"""Network-only AI provider outage overlay for the BUSY Bar.

Health data comes from AIWatch's public, unauthenticated JSON API.  The
monitor keeps its snapshot in memory and owns a separate high-priority
canvas, so the normal Codex/Claude dashboard remains visible while every
provider is healthy and reappears automatically after an incident clears.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass


API_URL = "https://aiwatch-worker.p2c2kbf.workers.dev/api/v1/status"
X_STATUS_URL = "https://isupmap.com/api/status"
GOOGLE_STATUS_URL = "https://www.google.com/generate_204"
APP_NAME = "ai_provider_status"
DRAW_PRIORITY = 80
POLL_S = 60.0
ROTATE_S = 4.0
KEEPALIVE_S = 8.0
ANIM_REFRESH_S = 60.0
SOURCE_MAX_AGE_S = 20 * 60.0
DIRECT_FAILURES_TO_ALERT = 2
MANUAL_HOLD_S = ROTATE_S
INPUT_RECONNECT_S = 2.0
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_SIZE = 4 * 1024 * 1024
WS_PING_INTERVAL_S = 20.0

TEXT_TIMEOUT_S = 15
ANIM_TIMEOUT_S = 120
FONT = "small"

BLACK = "#000000FF"
WHITE = "#FFFFFFFF"
DIM = "#777777FF"
HEALTHY = "#20C040FF"
DEGRADED = "#FFB000FF"
DOWN = "#FF2020FF"

STATUS_RANK = {"degraded": 1, "down": 2}


@dataclass(frozen=True)
class Provider:
    name: str
    services: tuple[tuple[str, str], ...]


# Each provider is one alert even when several of its surfaces fail together.
# The short labels are deliberate: the physical display is only 72 pixels wide.
PROVIDERS = (
    Provider("OPENAI", (("openai", "API"), ("chatgpt", "CHAT"), ("codex", "CODE"))),
    Provider("ANTHROPIC", (("claude", "API"), ("claudeai", "CHAT"),
                           ("claudecode", "CODE"))),
    Provider("GEMINI", (("gemini", "API"),)),
    Provider("OPENRTR", (("openrouter", "API"),)),
    Provider("DEEPSEEK", (("deepseek", "API"),)),
    Provider("MISTRAL", (("mistral", "API"),)),
    Provider("PERPLEX", (("perplexity", "API"),)),
)

PINNED_AI_PROVIDER = "ANTHROPIC"


def input_stream_url(api_base: str, token: str = "") -> str:
    """Turn a local device HTTP API base into its status WebSocket URL."""
    parsed = urllib.parse.urlsplit(api_base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/") + "/status/ws"
    query = ("x-api-token=" + urllib.parse.quote(token, safe="")) if token else ""
    return urllib.parse.urlunsplit((scheme, parsed.netloc, path, query, ""))


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _protobuf_fields(data: bytes):
    """Yield the protobuf wire fields needed by the BUSY input messages."""
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("invalid protobuf field number")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire == 1:
            if offset + 8 > len(data):
                raise ValueError("truncated protobuf fixed64")
            value, offset = data[offset:offset + 8], offset + 8
        elif wire == 2:
            length, offset = _read_varint(data, offset)
            if offset + length > len(data):
                raise ValueError("truncated protobuf message")
            value, offset = data[offset:offset + length], offset + length
        elif wire == 5:
            if offset + 4 > len(data):
                raise ValueError("truncated protobuf fixed32")
            value, offset = data[offset:offset + 4], offset + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def parse_input_events(state: bytes) -> list[tuple]:
    """Decode button/encoder events from one BSB_State.State message.

    The tiny wire decoder keeps the daemon dependency-free; field numbers come
    from busy-app/busybar-protobuf's state.proto and input.proto.
    """
    events = []
    for number, wire, update in _protobuf_fields(state):
        if number != 2 or wire != 2:  # State.updates
            continue
        for update_number, update_wire, input_event in _protobuf_fields(update):
            if update_number != 11 or update_wire != 2:  # StateUpdate.input
                continue
            for event_number, event_wire, event in _protobuf_fields(input_event):
                if event_wire != 2:
                    continue
                if event_number == 1:  # InputEvent.button_event
                    button = action = 0  # proto3 enum defaults: OK + PRESS
                    for field, field_wire, value in _protobuf_fields(event):
                        if field_wire == 0 and field == 1:
                            button = value
                        elif field_wire == 0 and field == 2:
                            action = value
                    events.append(("button", button, action))
                elif event_number == 3:  # InputEvent.encoder_event
                    delta = 0
                    for field, field_wire, value in _protobuf_fields(event):
                        if field == 1 and field_wire == 0:
                            delta = (value >> 1) ^ -(value & 1)  # sint32 zigzag
                    if delta:
                        events.append(("encoder", delta))
    return events


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("WebSocket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _ws_send(sock: socket.socket, opcode: int, payload: bytes = b""):
    """Send one masked client WebSocket frame (RFC 6455)."""
    header = bytearray((0x80 | opcode,))
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    mask = os.urandom(4)
    header.extend(mask)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_recv(sock: socket.socket) -> tuple[int, bool, bytes]:
    first, second = _recv_exact(sock, 2)
    opcode, final = first & 0x0F, bool(first & 0x80)
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > WS_MAX_SIZE:
        raise ConnectionError(f"WebSocket frame exceeds {WS_MAX_SIZE} bytes")
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length)
    if masked:
        payload = bytes(byte ^ mask[index % 4]
                        for index, byte in enumerate(payload))
    return opcode, final, payload


def _ws_connect(url: str) -> socket.socket:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    sock = socket.create_connection((host, port), timeout=3)
    try:
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        lines = [
            f"GET {target} HTTP/1.1",
            f"Host: {parsed.netloc}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(_recv_exact(sock, 1))
            if len(response) > 16384:
                raise ConnectionError("oversized WebSocket handshake")
        head = bytes(response).decode("iso-8859-1")
        if not head.startswith("HTTP/1.1 101"):
            raise ConnectionError(head.split("\r\n", 1)[0])
        response_headers = {}
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                response_headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest(),
        ).decode()
        if response_headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("invalid WebSocket accept header")
        sock.settimeout(1.0)
        return sock
    except Exception:
        sock.close()
        raise


def _iso_timestamp(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # AIWatch emits UTC timestamps with a trailing Z.
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def provider_alerts(payload: dict, now: float | None = None) -> list[dict]:
    """Collapse service-level AIWatch data into provider-level alerts.

    Unknown and stale observations never become outages: loss of the monitor
    itself must not look like loss of an AI provider.
    """
    now = time.time() if now is None else now
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, list):
        raise ValueError("AIWatch response has no services list")
    by_id = {
        str(service.get("id")): service
        for service in services
        if isinstance(service, dict) and service.get("id")
    }

    alerts = []
    for order, provider in enumerate(PROVIDERS):
        affected: list[tuple[str, str]] = []
        for service_id, surface in provider.services:
            service = by_id.get(service_id)
            if not service:
                continue
            checked = _iso_timestamp(service.get("lastChecked"))
            if checked is None or abs(now - checked) > SOURCE_MAX_AGE_S:
                continue
            status = str(service.get("status") or "").lower()
            if status in STATUS_RANK:
                affected.append((status, surface))
        if not affected:
            continue
        worst = max((status for status, _ in affected), key=STATUS_RANK.__getitem__)
        surfaces = []
        for _, surface in affected:
            if surface not in surfaces:
                surfaces.append(surface)
        alerts.append({
            "provider": provider.name,
            "status": worst,
            "surfaces": surfaces,
            "order": order,
        })

    return sorted(alerts, key=lambda a: (-STATUS_RANK[a["status"]], a["order"]))


def provider_status_item(payload: dict, provider_name: str,
                         now: float | None = None) -> dict | None:
    """Return one provider's current state, including a healthy state."""
    now = time.time() if now is None else now
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, list):
        raise ValueError("AIWatch response has no services list")
    provider = next((item for item in PROVIDERS if item.name == provider_name), None)
    if provider is None:
        raise ValueError(f"unknown provider {provider_name}")
    by_id = {
        str(service.get("id")): service
        for service in services
        if isinstance(service, dict) and service.get("id")
    }

    observations: list[tuple[str, str]] = []
    for service_id, surface in provider.services:
        service = by_id.get(service_id)
        if not service:
            continue
        checked = _iso_timestamp(service.get("lastChecked"))
        if checked is None or abs(now - checked) > SOURCE_MAX_AGE_S:
            continue
        status = str(service.get("status") or "").lower()
        if status in STATUS_RANK or status in ("operational", "up"):
            observations.append((status, surface))
    if not observations:
        return None

    affected = [(status, surface) for status, surface in observations
                if status in STATUS_RANK]
    if affected:
        status = max((state for state, _ in affected), key=STATUS_RANK.__getitem__)
        surfaces = list(dict.fromkeys(surface for _, surface in affected))
    else:
        status, surfaces = "operational", ["ALL"]
    return {
        "provider": provider.name,
        "status": status,
        "surfaces": surfaces,
        "order": PROVIDERS.index(provider),
    }


def x_status_item(payload: dict, now: float | None = None) -> dict | None:
    """Read X.com health plus its Downdetector-style community surge signal."""
    now = time.time() if now is None else now
    services = payload.get("services") if isinstance(payload, dict) else None
    updated_ms = payload.get("updatedAt") if isinstance(payload, dict) else None
    if not isinstance(services, list) or not isinstance(updated_ms, (int, float)):
        raise ValueError("isUpMap response has no services list or update time")
    if abs(now - updated_ms / 1000.0) > SOURCE_MAX_AGE_S:
        return None
    service = next((item for item in services
                    if isinstance(item, dict) and item.get("id") == "x"), None)
    if not service:
        raise ValueError("isUpMap response has no X service")

    status = str(service.get("status") or "").lower()
    surge = service.get("surge") is True
    # A community-report spike while the HTTP probe still passes is degraded,
    # not down. Confirmed probe failures retain isUpMap's severity.
    if status in STATUS_RANK:
        display_status = status
        surfaces = ["WEB"]
    elif surge:
        display_status = "degraded"
        surfaces = ["REPORTS"]
    elif status in ("up", "operational"):
        display_status = "operational"
        surfaces = ["WEB"]
    else:
        return None
    return {
        "provider": "X.COM",
        "status": display_status,
        "surfaces": surfaces,
        "order": len(PROVIDERS),
    }


def x_status_alerts(payload: dict, now: float | None = None) -> list[dict]:
    item = x_status_item(payload, now=now)
    return [item] if item and item["status"] in STATUS_RANK else []


def google_status_item(consecutive_failures: int) -> dict:
    """Represent Google's debounced direct-probe state for the carousel."""
    return {
        "provider": "GOOGLE",
        "status": ("down" if consecutive_failures >= DIRECT_FAILURES_TO_ALERT
                   else "operational"),
        "surfaces": ["WEB"],
        "order": len(PROVIDERS) + 1,
    }


def google_status_alerts(consecutive_failures: int) -> list[dict]:
    """Alert only after repeated direct failures; one timeout is just noise."""
    item = google_status_item(consecutive_failures)
    return [item] if item["status"] in STATUS_RANK else []


def carousel_items(alerts: list[dict], pinned: list[dict]) -> list[dict]:
    """Put incidents first, followed by pinned healthy services once each."""
    active = {item["provider"] for item in alerts}
    return [*alerts, *(item for item in pinned if item["provider"] not in active)]


def surface_label(surfaces: list[str]) -> str:
    if not surfaces:
        return ""
    if len(surfaces) == 1:
        return surfaces[0]
    return f"{surfaces[0]}+{len(surfaces) - 1}"


def _rect(eid, x, y, w, h, color, timeout=TEXT_TIMEOUT_S):
    return {"id": eid, "type": "rectangle", "display": "front",
            "x": x, "y": y, "width": w, "height": h,
            "border_width": 0, "fill": "solid", "fill_colors": [color],
            "timeout": timeout}


def _text(eid, x, y, align, text, color):
    return {"id": eid, "type": "text", "display": "front",
            "x": x, "y": y, "align": align, "text": text,
            "font": FONT, "color": color, "timeout": TEXT_TIMEOUT_S}


def animation_path(status: str) -> str:
    if status == "down":
        return "outage.anim"
    if status == "degraded":
        return "degraded.anim"
    return "healthy.anim"


def alert_animation(status: str) -> list[dict]:
    path = animation_path(status)
    # The opaque background is created before the transparent contour and
    # masks the lower-priority Codex canvas without repainting it.
    return [
        _rect("alert_bg", 0, 0, 72, 16, BLACK, ANIM_TIMEOUT_S),
        {"id": "alert_ring", "type": "animation", "display": "front",
         "x": 0, "y": 0, "path": path, "loop": True,
         "timeout": ANIM_TIMEOUT_S},
    ]


def alert_elements(alert: dict, index: int, total: int) -> list[dict]:
    status = alert["status"]
    if status == "down":
        color, marker, word = DOWN, "!", "DOWN"
    elif status == "degraded":
        color, marker, word = DEGRADED, "!", "DEGR"
    else:
        color, marker, word = HEALTHY, "+", "OK"
    # Keep the warning word compact so the affected surface has a clean gap.
    return [
        # A colored marker reads more cleanly than a tiny raster lightning bolt.
        _text("alert_mark", 3, 0, "top_left", marker, color),
        _text("alert_provider", 9, 0, "top_left", alert["provider"], WHITE),
        _text("alert_index", 69, 0, "top_right", f"{index + 1}/{total}", DIM),
        _text("alert_state", 3, 15, "bottom_left", word, color),
        _text("alert_surface", 69, 15, "bottom_right",
              surface_label(alert.get("surfaces") or []), DIM),
    ]


class Monitor:
    """Poll AIWatch and maintain the independent outage canvas."""

    def __init__(self, transport, render_lock: threading.Lock, *,
                 url: str = API_URL, poll_s: float = POLL_S,
                 x_url: str = X_STATUS_URL,
                 google_url: str = GOOGLE_STATUS_URL,
                 input_url: str = "",
                 should_render=lambda: True, logger=lambda _msg: None,
                 opener=None):
        self.transport = transport
        self.render_lock = render_lock
        self.url = url
        self.x_url = x_url
        self.google_url = google_url
        self.input_url = input_url
        self.poll_s = max(15.0, poll_s)
        self.should_render = should_render
        self.logger = logger
        # Ignore proxy environment variables just like the device/LAN transport;
        # they are commonly stale in desktop launch environments.
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.alerts: list[dict] = []
        self.pinned: list[dict] = []
        self.last_success: float | None = None
        self.last_error = ""
        self.drawn = False
        self.google_failures = 0
        self.google_last_ok: float | None = None
        self.input_connected = False
        self.input_error = ""
        self.last_input = ""
        self.selected_index: int | None = None
        self.manual_until = 0.0
        self.displayed_index: int | None = None
        self.displayed_provider = ""
        self.displayed_at: float | None = None
        self.draw_attempts = 0
        self.draw_rejections = 0
        self.last_draw_ms: float | None = None
        self.last_draw_status: int | None = None
        self.refresh_requested = threading.Event()
        self.redraw_requested = threading.Event()
        self._lock = threading.Lock()

    def handle_input_event(self, event: tuple, now: float | None = None) -> bool:
        """Apply one BUSY input event. Returns whether it changed this view."""
        now = time.time() if now is None else now
        direction = 0
        action = ""
        refresh = False
        if event[0] == "encoder":
            direction = 1 if event[1] > 0 else -1
            action = "NEXT" if direction > 0 else "PREV"
        elif event[0] == "button" and event[2] == 0:  # PRESS only
            if event[1] == 0:  # OK
                refresh, action = True, "REFRESH"
            elif event[1] == 2:  # START
                direction, action = 1, "NEXT"
        if not action:
            return False

        with self._lock:
            items = carousel_items(self.alerts, self.pinned)
            if not self.alerts or not items:
                return False
            if refresh:
                self.selected_index = 0
            else:
                if self.selected_index is not None and now < self.manual_until:
                    current = self.selected_index % len(items)
                elif self.displayed_index is not None:
                    current = self.displayed_index % len(items)
                else:
                    current = 0
                self.selected_index = (current + direction) % len(items)
            self.manual_until = now + MANUAL_HOLD_S
            self.last_input = action
        if refresh:
            self.refresh_requested.set()
        self.redraw_requested.set()
        return True

    def _input_loop(self, stop: threading.Event):
        logged_error = ""
        while not stop.is_set():
            sock = None
            try:
                sock = _ws_connect(self.input_url)
                _ws_send(sock, 1, b'{"enable":true}')
                with self._lock:
                    self.input_connected = True
                    self.input_error = ""
                if logged_error:
                    self.logger("BUSY controls connected again")
                    logged_error = ""
                fragments = bytearray()
                fragment_opcode = None
                next_ping = time.monotonic() + WS_PING_INTERVAL_S
                while not stop.is_set():
                    if time.monotonic() >= next_ping:
                        _ws_send(sock, 9, os.urandom(4))
                        next_ping = time.monotonic() + WS_PING_INTERVAL_S
                    try:
                        opcode, final, payload = _ws_recv(sock)
                    except socket.timeout:
                        if time.monotonic() >= next_ping:
                            _ws_send(sock, 9, os.urandom(4))
                            next_ping = time.monotonic() + WS_PING_INTERVAL_S
                        continue
                    if opcode == 8:
                        raise ConnectionError("WebSocket closed")
                    if opcode == 9:
                        _ws_send(sock, 10, payload)
                        continue
                    if opcode == 10:
                        continue
                    if opcode in (1, 2):
                        fragments = bytearray(payload)
                        fragment_opcode = opcode
                    elif opcode == 0 and fragment_opcode is not None:
                        fragments.extend(payload)
                    else:
                        continue
                    if not final:
                        continue
                    if fragment_opcode == 2:
                        for event in parse_input_events(bytes(fragments)):
                            self.handle_input_event(event)
                    fragments.clear()
                    fragment_opcode = None
            except (OSError, ValueError, ConnectionError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:160]
                with self._lock:
                    self.input_connected = False
                    self.input_error = error
                if error != logged_error and not stop.is_set():
                    self.logger(f"BUSY controls unavailable: {error}")
                    logged_error = error
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            stop.wait(INPUT_RECONNECT_S)

    def fetch(self, now: float | None = None) -> tuple[list[dict], list[str]]:
        def get_json(url):
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json",
                         "User-Agent": "busy-codex-ai-status/1"},
            )
            with self.opener.open(request, timeout=10) as response:
                return json.loads(response.read())

        alerts = []
        pinned = []
        errors = []
        successful_sources = 0
        try:
            ai_payload = get_json(self.url)
            alerts.extend(provider_alerts(ai_payload, now=now))
            anthropic_item = provider_status_item(
                ai_payload, PINNED_AI_PROVIDER, now=now,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"AIWatch: {type(exc).__name__}: {exc}"[:160])
        else:
            successful_sources += 1
            if anthropic_item:
                pinned.append(anthropic_item)

        try:
            x_item = x_status_item(get_json(self.x_url), now=now)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"isUpMap: {type(exc).__name__}: {exc}"[:160])
        else:
            successful_sources += 1
            if x_item:
                pinned.append(x_item)
                if x_item["status"] in STATUS_RANK:
                    alerts.append(x_item)
        if successful_sources == 0:
            raise OSError("; ".join(errors))

        google_ok = False
        try:
            request = urllib.request.Request(
                self.google_url,
                headers={"User-Agent": "busy-codex-ai-status/1"},
            )
            with self.opener.open(request, timeout=8) as response:
                google_ok = 200 <= response.status < 400
        except OSError:
            pass
        with self._lock:
            if google_ok:
                self.google_failures = 0
                self.google_last_ok = time.time() if now is None else now
            else:
                self.google_failures += 1
            google_failures = self.google_failures
            google_item = google_status_item(google_failures)
            self.pinned = [*pinned, google_item]
        if google_item["status"] in STATUS_RANK:
            alerts.append(google_item)
        return (sorted(alerts,
                       key=lambda a: (-STATUS_RANK[a["status"]], a["order"])),
                errors)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "source": self.url,
                "x_source": self.x_url,
                "google_probe": {
                    "url": self.google_url,
                    "consecutive_failures": self.google_failures,
                    "last_ok": self.google_last_ok,
                },
                "last_success": self.last_success,
                "error": self.last_error,
                "alerts": [dict(alert) for alert in self.alerts],
                "pinned": [dict(item) for item in self.pinned],
                "controls": {
                    "connected": self.input_connected,
                    "error": self.input_error,
                    "last_action": self.last_input,
                    "manual_until": self.manual_until or None,
                    "selected_index": self.selected_index,
                },
                "view": ({
                    "provider": self.displayed_provider,
                    "index": self.displayed_index,
                    "shown_at": self.displayed_at,
                } if self.displayed_index is not None else None),
                "draw": {
                    "attempts": self.draw_attempts,
                    "rejections": self.draw_rejections,
                    "last_ms": self.last_draw_ms,
                    "last_http_status": self.last_draw_status,
                },
                "rendering": self.drawn,
            }

    def _draw(self, payload: dict) -> bool:
        started = time.monotonic()
        ok = self.transport.draw(payload)
        elapsed_ms = (time.monotonic() - started) * 1000
        with self._lock:
            self.draw_attempts += 1
            if not ok:
                self.draw_rejections += 1
            self.last_draw_ms = round(elapsed_ms, 1)
            self.last_draw_status = getattr(self.transport, "last_http_status", None)
        return ok

    def _note_drawn(self, alert: dict, index: int, now: float):
        with self._lock:
            self.displayed_index = index
            self.displayed_provider = alert["provider"]
            self.displayed_at = now

    def _clear(self):
        if self.drawn:
            with self.render_lock:
                cleared = self.transport.clear(APP_NAME)
            if cleared:
                self.drawn = False
                with self._lock:
                    self.displayed_index = None
                    self.displayed_provider = ""
                    self.displayed_at = None

    def run(self, stop: threading.Event):
        if self.input_url:
            threading.Thread(target=self._input_loop, args=(stop,),
                             daemon=True).start()
        next_poll = 0.0
        last_view = None
        last_text_ts = 0.0
        last_anim = None
        last_anim_ts = 0.0
        item_signature = None
        auto_index = 0
        next_rotate_at = None
        was_manual = False
        error_logged = ""
        try:
            while not stop.is_set():
                now = time.time()
                if now >= next_poll or self.refresh_requested.is_set():
                    self.refresh_requested.clear()
                    try:
                        alerts, source_errors = self.fetch(now=now)
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        error = f"{type(exc).__name__}: {exc}"[:160]
                        with self._lock:
                            self.last_error = error
                        if error != error_logged:
                            self.logger(f"AI status source unavailable: {error}")
                            error_logged = error
                    else:
                        with self._lock:
                            previous = {(a["provider"], a["status"])
                                        for a in self.alerts}
                            self.alerts = alerts
                            self.last_success = now
                            self.last_error = "; ".join(source_errors)
                        current = {(a["provider"], a["status"]) for a in alerts}
                        if current != previous:
                            summary = ", ".join(
                                f'{a["provider"]} {a["status"]}' for a in alerts
                            ) or "all providers operational"
                            self.logger(f"AI status: {summary}")
                        partial_error = "; ".join(source_errors)
                        if partial_error and partial_error != error_logged:
                            self.logger(f"AI status source unavailable: {partial_error}")
                            error_logged = partial_error
                        elif not partial_error and error_logged:
                            self.logger("AI status source reachable again")
                            error_logged = ""
                    next_poll = now + self.poll_s

                with self._lock:
                    if (self.last_success is not None
                            and now - self.last_success > SOURCE_MAX_AGE_S):
                        self.alerts = []
                        self.pinned = []
                    alerts = [dict(alert) for alert in self.alerts]
                    pinned = [dict(item) for item in self.pinned]
                    selected_index = self.selected_index
                    manual_until = self.manual_until
                    manual_index = (selected_index
                                    if now < manual_until else None)
                force_redraw = self.redraw_requested.is_set()
                if force_redraw:
                    self.redraw_requested.clear()
                allowed = self.should_render()
                if not alerts or not allowed:
                    self._clear()
                    last_view = last_anim = None
                    item_signature = None
                    auto_index = 0
                    next_rotate_at = None
                    was_manual = False
                else:
                    items = carousel_items(alerts, pinned)
                    signature = tuple(
                        (item["provider"], item["status"],
                         tuple(item.get("surfaces") or []))
                        for item in items
                    )
                    if signature != item_signature:
                        item_signature = signature
                        auto_index = 0
                        next_rotate_at = None
                        last_view = None
                    if manual_index is not None:
                        index = manual_index % len(items)
                        was_manual = True
                    else:
                        if was_manual:
                            auto_index = ((selected_index or 0) % len(items))
                            next_rotate_at = now + ROTATE_S
                            was_manual = False
                        elif next_rotate_at is not None and now >= next_rotate_at:
                            auto_index = (auto_index + 1) % len(items)
                            # Do not start the next dwell until this item has
                            # actually been accepted by the device.
                            next_rotate_at = None
                        index = auto_index
                    alert = items[index]
                    view = (alert["provider"], alert["status"],
                            tuple(alert["surfaces"]), index, len(items))
                    anim_path = animation_path(alert["status"])
                    with self.render_lock:
                        if (anim_path != last_anim
                                or now - last_anim_ts >= ANIM_REFRESH_S):
                            ok = self._draw({
                                "application_name": APP_NAME,
                                "priority": DRAW_PRIORITY,
                                "elements": [
                                    *alert_animation(alert["status"]),
                                    *alert_elements(alert, index, len(items)),
                                ],
                            })
                            if ok:
                                rendered_at = time.time()
                                last_anim, last_anim_ts = anim_path, rendered_at
                                last_view, last_text_ts = view, rendered_at
                                self.drawn = True
                                self._note_drawn(alert, index, rendered_at)
                                if manual_index is None and next_rotate_at is None:
                                    next_rotate_at = rendered_at + ROTATE_S
                        elif (force_redraw or view != last_view
                              or now - last_text_ts >= KEEPALIVE_S):
                            ok = self._draw({
                                "application_name": APP_NAME,
                                "priority": DRAW_PRIORITY,
                                "elements": alert_elements(alert, index, len(items)),
                            })
                            if ok:
                                rendered_at = time.time()
                                last_view, last_text_ts = view, rendered_at
                                self.drawn = True
                                self._note_drawn(alert, index, rendered_at)
                                if manual_index is None and next_rotate_at is None:
                                    next_rotate_at = rendered_at + ROTATE_S
                # A physical control wakes the renderer immediately; otherwise
                # it ticks at the normal low-traffic cadence.
                wake_at = next_poll
                if next_rotate_at is not None:
                    wake_at = min(wake_at, next_rotate_at)
                if manual_index is not None:
                    wake_at = min(wake_at, manual_until)
                self.redraw_requested.wait(
                    timeout=min(0.5, max(0.05, wake_at - time.time())),
                )
        finally:
            self._clear()
