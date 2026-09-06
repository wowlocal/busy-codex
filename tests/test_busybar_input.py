import base64
import hashlib
import socket
import struct
import threading
import unittest
from unittest import mock

import busybar_input as busy


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    return varint(number << 3 | 2) + varint(len(value)) + value


def encoder(delta):
    value = (delta << 1) ^ (delta >> 31)
    return field(2, field(11, field(3, b"\x08" + varint(value))))


def frame(opcode, payload=b"", final=True):
    header = bytes(((0x80 if final else 0) | opcode,))
    if len(payload) < 126:
        header += bytes((len(payload),))
    elif len(payload) <= 65535:
        header += b"\x7e" + struct.pack("!H", len(payload))
    else:
        header += b"\x7f" + struct.pack("!Q", len(payload))
    return header + payload


class ScriptedSocket:
    def __init__(self, *reads):
        self.reads = list(reads)
        self.sent = []
        self.closed = False
        self.timeout = None

    def recv(self, size):
        if not self.reads:
            return b""
        item = self.reads.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        if len(item) > size:
            self.reads.insert(0, item[size:])
        return item[:size]

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class StopAfterWaits:
    def __init__(self, count):
        self.count = count
        self.waits = []
        self.stopped = False

    def set(self):
        self.stopped = True

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        if len(self.waits) == self.count:
            self.set()
        return self.stopped


class ProtobufInputTest(unittest.TestCase):
    def test_keeps_all_batched_steps_and_skips_other_state_updates(self):
        state = field(42, b"\x00") + encoder(3) + encoder(-4) + encoder(1)
        self.assertEqual([("encoder", 3), ("encoder", -4), ("encoder", 1)],
                         busy.parse_input_events(state))

    def test_rejects_truncated_or_oversized_wire_values(self):
        for data in (b"\x12\x02\x00", b"\x12\x80", b"\x00",
                     b"\x08" + b"\xff" * 9 + b"\x02"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                busy.parse_input_events(data)


class FrameReaderTest(unittest.TestCase):
    def test_timeout_mid_header_and_mid_payload_does_not_lose_frame(self):
        packet = frame(2, b"a" * 200)
        sock = ScriptedSocket(packet[:1], socket.timeout(), packet[1:3],
                              socket.timeout(), packet[3:29], socket.timeout(),
                              packet[29:] + frame(2, b"next"))
        reader = busy.FrameReader(sock)
        for _ in range(3):
            with self.assertRaises(socket.timeout):
                reader.read()
        self.assertEqual((2, True, b"a" * 200), reader.read())
        self.assertEqual((2, True, b"next"), reader.read())

    def test_rejects_invalid_and_oversized_frames_before_reading_payload(self):
        for packet in (b"\xc2\x00", b"\x83\x00", b"\x82\x80",
                       b"\x09\x00", b"\x89\x7e\x00\x7e",
                       b"\x82\x7e\x00\x01", b"\x82\x7f" + b"\xff" * 8,
                       b"\x82\x7e\x10\x00"):
            with self.subTest(packet=packet), self.assertRaises(ConnectionError):
                busy.FrameReader(ScriptedSocket(packet), max_bytes=1024).read()

    def test_client_control_frames_are_masked(self):
        sock = ScriptedSocket()
        busy.send_frame(sock, 10, b"ping")
        packet = sock.sent[0]
        self.assertEqual(b"\x8a\x84", packet[:2])
        mask = packet[2:6]
        self.assertEqual(b"ping", bytes(b ^ mask[i % 4] for i, b in enumerate(packet[6:])))


class WebSocketConnectTest(unittest.TestCase):
    def handshake(self, extra_headers="", status="101 Switching Protocols"):
        key = base64.b64encode(b"k" * 16).decode()
        accept = base64.b64encode(hashlib.sha1((key + busy.WS_GUID).encode()).digest()).decode()
        return (f"HTTP/1.1 {status}\r\nUpgrade: websocket\r\n"
                f"Connection: keep-alive,Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n"
                f"{extra_headers}\r\n").encode()

    def test_binds_usb_source_and_leaves_first_frame_unconsumed(self):
        sock = ScriptedSocket(self.handshake() + frame(2, encoder(1)))
        with mock.patch.object(busy.socket, "create_connection", return_value=sock) as create, \
                mock.patch.object(busy.os, "urandom", return_value=b"k" * 16):
            result = busy.connect_websocket("ws://10.0.4.20/api/status/ws?x-api-token=1234",
                                            source_address="10.0.4.21")
        self.assertIs(sock, result)
        create.assert_called_once_with(("10.0.4.20", 80), timeout=3.0,
                                       source_address=("10.0.4.21", 0))
        self.assertEqual(1.0, sock.timeout)
        self.assertIn(b"GET /api/status/ws?x-api-token=1234 HTTP/1.1\r\n", sock.sent[0])
        self.assertEqual((2, True, encoder(1)), busy.FrameReader(sock).read())

    def test_failed_handshake_closes_connection(self):
        sock = ScriptedSocket(self.handshake(status="403 Forbidden"))
        with mock.patch.object(busy.socket, "create_connection", return_value=sock), \
                mock.patch.object(busy.os, "urandom", return_value=b"k" * 16), \
                self.assertRaisesRegex(ConnectionError, "403"):
            busy.connect_websocket("ws://bar.local/api/status/ws")
        self.assertTrue(sock.closed)


class InputStreamTest(unittest.TestCase):
    def test_fragmented_input_with_interleaved_ping_is_delivered_once_in_order(self):
        state = encoder(1) + encoder(-2) + encoder(3)
        sock = ScriptedSocket(frame(2, state[:9], final=False), frame(9, b"hello"),
                              frame(0, state[9:]))
        stop = threading.Event()
        received = []

        def on_event(event):
            received.append(event)
            if len(received) == 3:
                stop.set()

        busy.InputStream("ws://bar", on_event)._consume(sock, stop)
        self.assertEqual([("encoder", 1), ("encoder", -2), ("encoder", 3)], received)
        self.assertEqual(10, sock.sent[0][0] & 15)

    def test_fragmented_message_has_aggregate_limit(self):
        sock = ScriptedSocket(frame(2, b"a" * 8, final=False), frame(0, b"b" * 8))
        with mock.patch.object(busy, "MAX_MESSAGE_BYTES", 10), \
                self.assertRaisesRegex(ConnectionError, "message exceeds"):
            busy.InputStream("ws://bar", lambda _: None)._consume(sock, threading.Event())

    def test_rejects_invalid_continuation_sequences(self):
        for data in (frame(0, b""), frame(2, b"", final=False) + frame(2, b"")):
            with self.subTest(data=data), self.assertRaises(ConnectionError):
                busy.InputStream("ws://bar", lambda _: None)._consume(
                    ScriptedSocket(data), threading.Event())

    def test_handler_failure_does_not_discard_remaining_steps(self):
        stop = threading.Event()
        received = []
        logs = []

        def on_event(event):
            received.append(event)
            if len(received) == 1:
                raise ValueError("route unavailable")
            stop.set()

        busy.InputStream("ws://bar", on_event, logger=logs.append)._consume(
            ScriptedSocket(frame(2, encoder(1) + encoder(-1))), stop)
        self.assertEqual([("encoder", 1), ("encoder", -1)], received)
        self.assertIn("handler failed", logs[0])

    def test_retry_backoff_is_bounded_and_stoppable(self):
        stop = StopAfterWaits(6)
        states = []
        logs = []
        connect = mock.Mock(side_effect=OSError("device absent"))
        busy.InputStream("ws://bar", lambda _: None, connect=connect,
                         on_state=lambda *s: states.append(s), logger=logs.append).run(stop)
        self.assertEqual([2, 4, 8, 16, 30, 30], stop.waits)
        self.assertEqual(6, connect.call_count)
        self.assertEqual((False, ""), states[-1])
        self.assertEqual(1, len(logs))

    def test_recovers_subscribes_and_reports_disconnected_on_shutdown(self):
        stop = StopAfterWaits(9)
        states = []
        received = []
        sock = ScriptedSocket(frame(2, encoder(1) + encoder(2)))
        connect = mock.Mock(side_effect=[OSError("device absent"), sock])

        def on_event(event):
            received.append(event)
            stop.set()

        busy.InputStream("ws://bar", on_event, source_address="10.0.4.21", connect=connect,
                         on_state=lambda *s: states.append(s)).run(stop)
        self.assertEqual([2], stop.waits)
        self.assertEqual([("encoder", 1), ("encoder", 2)], received)
        self.assertEqual([(False, "OSError: device absent"), (True, ""), (False, "")], states)
        self.assertTrue(sock.closed)
        self.assertEqual(1, sock.sent[0][0] & 15)  # subscription text frame
        connect.assert_called_with("ws://bar", source_address="10.0.4.21")

    def test_stable_connection_resets_retry_delay(self):
        stop = StopAfterWaits(4)
        now = [0.0]
        sock = ScriptedSocket()
        connect = mock.Mock(side_effect=[OSError("absent")] * 3 + [sock])
        stream = busy.InputStream("ws://bar", lambda _: None, connect=connect,
                                  clock=lambda: now[0])

        def disconnect_after_stable_connection(_sock, _stop):
            now[0] += 35
            raise ConnectionError("device disconnected")

        with mock.patch.object(stream, "_consume", side_effect=disconnect_after_stable_connection):
            stream.run(stop)
        self.assertEqual([2, 4, 8, 2], stop.waits)
        self.assertTrue(sock.closed)

    def test_malformed_message_reconnects_without_replaying_previous_input(self):
        stop = StopAfterWaits(9)
        received = []
        broken = ScriptedSocket(frame(2, encoder(1)) + frame(2, b"\x00"))
        recovered = ScriptedSocket(frame(2, encoder(2)))

        def on_event(event):
            received.append(event)
            if len(received) == 2:
                stop.set()

        busy.InputStream("ws://bar", on_event,
                         connect=mock.Mock(side_effect=[broken, recovered])).run(stop)
        self.assertEqual([("encoder", 1), ("encoder", 2)], received)
        self.assertEqual([2], stop.waits)
        self.assertTrue(broken.closed)
        self.assertTrue(recovered.closed)

    def test_unresponsive_connection_requires_matching_pong(self):
        now = [0.0]

        def advance(seconds):
            def timeout():
                now[0] += seconds
                return socket.timeout()
            return timeout

        sock = ScriptedSocket(advance(20), frame(10, b"wrong"), advance(11))
        with self.assertRaisesRegex(ConnectionError, "pong timed out"):
            busy.InputStream("ws://bar", lambda _: None, clock=lambda: now[0])._consume(
                sock, threading.Event())
        self.assertEqual(9, sock.sent[0][0] & 15)

    def test_matching_pong_allows_later_input(self):
        now = [0.0]
        stop = threading.Event()
        received = []

        def timeout():
            now[0] += 20
            return socket.timeout()

        sock = ScriptedSocket(timeout, frame(10, b"pong"), timeout,
                              frame(10, b"pong"), frame(2, encoder(4)))

        def on_event(event):
            received.append(event)
            stop.set()

        with mock.patch.object(busy.os, "urandom", return_value=b"pong"):
            busy.InputStream("ws://bar", on_event, clock=lambda: now[0])._consume(sock, stop)
        self.assertEqual([("encoder", 4)], received)
        self.assertEqual(2, len(sock.sent))


if __name__ == "__main__":
    unittest.main()
