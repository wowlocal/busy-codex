"""Small, dependency-free BUSY Bar input client shared by dashboard consumers.

The device sends BSB_State.State protobuf messages over its local WebSocket.
Input is delivered in order; unlike visual frames, dial steps must never be
coalesced or discarded. Rendering and event ownership stay with each caller.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from collections.abc import Callable

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 1.0
PING_INTERVAL_S = 20.0
PONG_TIMEOUT_S = 10.0
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 30.0


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
        if shift == 63 and byte > 1:
            raise ValueError("protobuf varint exceeds 64 bits")
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
                elif event_number == 2:  # InputEvent.switch_event
                    position = 0  # proto3 enum default: BUSY
                    for field, field_wire, value in _protobuf_fields(event):
                        if field == 1 and field_wire == 0:
                            position = value
                    events.append(("switch", position))
                elif event_number == 3:  # InputEvent.encoder_event
                    delta = 0
                    for field, field_wire, value in _protobuf_fields(event):
                        if field == 1 and field_wire == 0:
                            delta = (value >> 1) ^ -(value & 1)  # sint32 zigzag
                    if delta:
                        events.append(("encoder", delta))
    return events


def send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    """Send a masked client frame; control frames cannot be fragmented."""
    if opcode not in (0, 1, 2, 8, 9, 10):
        raise ValueError("invalid WebSocket opcode")
    length = len(payload)
    if length > MAX_MESSAGE_BYTES or (opcode >= 8 and length > 125):
        raise ValueError("WebSocket payload too large")
    header = bytearray((0x80 | opcode,))
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


class FrameReader:
    """Keep partial bytes across timeouts, including partial frame headers.

    A timeout is a chance to check shutdown/keepalive, not a message boundary.
    Reading each part with a fresh recv_exact would lose those bytes and turn
    the rest of that frame into a corrupt header on the next call.
    """

    def __init__(self, sock: socket.socket, max_bytes: int = MAX_MESSAGE_BYTES):
        self.sock = sock
        self.max_bytes = max_bytes
        self.buffer = bytearray()

    def _fill(self, length: int) -> None:
        while len(self.buffer) < length:
            chunk = self.sock.recv(min(65536, length - len(self.buffer)))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            self.buffer.extend(chunk)

    def read(self) -> tuple[int, bool, bytes]:
        self._fill(2)
        first, second = self.buffer[:2]
        opcode, final = first & 0x0F, bool(first & 0x80)
        if first & 0x70 or opcode not in (0, 1, 2, 8, 9, 10):
            raise ConnectionError("invalid WebSocket frame flags")
        if second & 0x80:
            raise ConnectionError("server WebSocket frame must not be masked")
        length = second & 0x7F
        offset = 2
        if length == 126:
            self._fill(4)
            length = struct.unpack("!H", self.buffer[2:4])[0]
            offset = 4
            if length < 126:
                raise ConnectionError("non-minimal WebSocket length")
        elif length == 127:
            self._fill(10)
            length = struct.unpack("!Q", self.buffer[2:10])[0]
            offset = 10
            if length < 65536 or length >= 1 << 63:
                raise ConnectionError("invalid WebSocket length")
        if length > self.max_bytes:
            raise ConnectionError(f"WebSocket frame exceeds {self.max_bytes} bytes")
        if opcode >= 8 and (not final or length > 125):
            raise ConnectionError("invalid WebSocket control frame")
        self._fill(offset + length)
        payload = bytes(self.buffer[offset:offset + length])
        del self.buffer[:offset + length]
        return opcode, final, payload


def connect_websocket(url: str, source_address: str | None = None) -> socket.socket:
    """Connect directly to the device, optionally binding its USB interface."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
        raise ValueError("expected an absolute ws:// or wss:// device URL")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    source = (source_address, 0) if source_address else None
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S,
                                    source_address=source)
    try:
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        lines = [
            f"GET {target} HTTP/1.1", f"Host: {parsed.netloc}",
            "Upgrade: websocket", "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
        ]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        response = bytearray()
        # Do not consume bytes from the first data frame after the HTTP headers.
        while not response.endswith(b"\r\n\r\n"):
            byte = sock.recv(1)
            if not byte:
                raise ConnectionError("WebSocket closed during handshake")
            response.extend(byte)
            if len(response) > 16384:
                raise ConnectionError("oversized WebSocket handshake")
        head = bytes(response).decode("iso-8859-1")
        status = head.split("\r\n", 1)[0].split(" ")
        if len(status) < 2 or status[:2] != ["HTTP/1.1", "101"]:
            raise ConnectionError(head.split("\r\n", 1)[0])
        headers = {}
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("invalid WebSocket accept header")
        if (headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in {part.strip().lower() for part in
                                     headers.get("connection", "").split(",")}):
            raise ConnectionError("invalid WebSocket upgrade headers")
        sock.settimeout(READ_TIMEOUT_S)
        return sock
    except Exception:
        sock.close()
        raise


class InputStream:
    """Reconnectable local device input stream with ordered callback delivery.

    The caller supplies routing and connection-state callbacks. No worker or
    event queue is created here: handlers should be quick and signal their own
    workers when an action needs network I/O or rendering.
    """

    def __init__(self, url: str, on_event: Callable[[tuple], object], *,
                 source_address: str | None = None,
                 on_state: Callable[[bool, str], object] = lambda _ok, _err: None,
                 logger: Callable[[str], object] = lambda _msg: None,
                 label: str = "BUSY controls", connect=None, clock=time.monotonic):
        self.url = url
        self.on_event = on_event
        self.source_address = source_address
        self.on_state = on_state
        self.logger = logger
        self.label = label
        self.connect = connect or connect_websocket
        self.clock = clock

    def _consume(self, sock: socket.socket, stop: threading.Event) -> None:
        reader = FrameReader(sock)
        fragments = bytearray()
        fragment_opcode = None
        next_ping = self.clock() + PING_INTERVAL_S
        pending_ping = None
        pong_deadline = 0.0
        while not stop.is_set():
            now = self.clock()
            if pending_ping is not None and now >= pong_deadline:
                raise ConnectionError("WebSocket pong timed out")
            if pending_ping is None and now >= next_ping:
                pending_ping = os.urandom(4)
                send_frame(sock, 9, pending_ping)
                pong_deadline = now + PONG_TIMEOUT_S
                next_ping = now + PING_INTERVAL_S
            try:
                opcode, final, payload = reader.read()
            except socket.timeout:
                continue
            if opcode == 8:
                raise ConnectionError("WebSocket closed")
            if opcode == 9:
                send_frame(sock, 10, payload)
                continue
            if opcode == 10:
                if payload == pending_ping:
                    pending_ping = None
                continue
            if opcode in (1, 2):
                if fragment_opcode is not None:
                    raise ConnectionError("new WebSocket message during fragments")
                fragment_opcode = opcode
            elif fragment_opcode is None:
                raise ConnectionError("unexpected WebSocket continuation")
            if len(fragments) + len(payload) > MAX_MESSAGE_BYTES:
                raise ConnectionError(f"WebSocket message exceeds {MAX_MESSAGE_BYTES} bytes")
            fragments.extend(payload)
            if not final:
                continue
            if fragment_opcode == 2:
                for event in parse_input_events(bytes(fragments)):
                    try:
                        self.on_event(event)
                    except Exception as exc:
                        # One failing route must not discard subsequent steps.
                        self.logger(f"{self.label} handler failed: {type(exc).__name__}: {exc}"[:240])
            fragments.clear()
            fragment_opcode = None

    def run(self, stop: threading.Event) -> None:
        delay = RECONNECT_MIN_S
        logged_error = ""
        while not stop.is_set():
            sock = None
            connected_at = None
            try:
                sock = self.connect(self.url, source_address=self.source_address)
                send_frame(sock, 1, b'{"enable":true}')
                connected_at = self.clock()
                self.on_state(True, "")
                if logged_error:
                    self.logger(f"{self.label} connected again")
                    logged_error = ""
                self._consume(sock, stop)
            except (OSError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:160]
                self.on_state(False, error)
                if error != logged_error and not stop.is_set():
                    self.logger(f"{self.label} unavailable: {error}")
                    logged_error = error
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if stop.is_set():
                break
            # A stable connection resets backoff; repeated handshake/close loops
            # continue to back off rather than hammering an unavailable device.
            if connected_at is not None and self.clock() - connected_at >= RECONNECT_MAX_S:
                delay = RECONNECT_MIN_S
            stop.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)
        self.on_state(False, "")
