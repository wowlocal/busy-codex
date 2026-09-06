import datetime
import threading
import unittest
from unittest import mock

import ai_status


def checked_at(timestamp):
    return datetime.datetime.fromtimestamp(
        timestamp, datetime.timezone.utc,
    ).isoformat().replace("+00:00", "Z")


class ProviderAlertsTest(unittest.TestCase):
    def test_groups_surfaces_and_sorts_down_first(self):
        now = 1_800_000_000.0
        payload = {"services": [
            {"id": "openai", "status": "operational", "lastChecked": checked_at(now)},
            {"id": "chatgpt", "status": "degraded", "lastChecked": checked_at(now)},
            {"id": "codex", "status": "down", "lastChecked": checked_at(now)},
            {"id": "claude", "status": "degraded", "lastChecked": checked_at(now)},
        ]}

        alerts = ai_status.provider_alerts(payload, now=now)

        self.assertEqual(["OPENAI", "ANTHROPIC"], [a["provider"] for a in alerts])
        self.assertEqual("down", alerts[0]["status"])
        self.assertEqual(["CHAT", "CODE"], alerts[0]["surfaces"])
        self.assertEqual("degraded", alerts[1]["status"])

    def test_healthy_anthropic_is_available_as_a_pinned_status(self):
        now = 1_800_000_000.0
        payload = {"services": [
            {"id": "claude", "status": "operational",
             "lastChecked": checked_at(now)},
            {"id": "claudeai", "status": "operational",
             "lastChecked": checked_at(now)},
            {"id": "claudecode", "status": "operational",
             "lastChecked": checked_at(now)},
        ]}

        item = ai_status.provider_status_item(payload, "ANTHROPIC", now=now)

        self.assertEqual("operational", item["status"])
        self.assertEqual(["ALL"], item["surfaces"])

    def test_stale_and_unknown_observations_do_not_alert(self):
        now = 1_800_000_000.0
        payload = {"services": [
            {"id": "openai", "status": "down",
             "lastChecked": checked_at(now - ai_status.SOURCE_MAX_AGE_S - 1)},
            {"id": "claude", "status": "unknown", "lastChecked": checked_at(now)},
            {"id": "gemini", "status": "operational", "lastChecked": checked_at(now)},
        ]}

        self.assertEqual([], ai_status.provider_alerts(payload, now=now))

    def test_xai_and_copilot_are_excluded(self):
        now = 1_800_000_000.0
        payload = {"services": [
            {"id": "xai", "status": "down", "lastChecked": checked_at(now)},
            {"id": "grok", "status": "down", "lastChecked": checked_at(now)},
            {"id": "copilot", "status": "down", "lastChecked": checked_at(now)},
        ]}

        self.assertEqual([], ai_status.provider_alerts(payload, now=now))

    def test_invalid_payload_is_not_treated_as_healthy(self):
        with self.assertRaises(ValueError):
            ai_status.provider_alerts({})

    def test_x_status_supports_probe_failures_and_report_surges(self):
        now = 1_800_000_000.0
        down = {"updatedAt": now * 1000, "services": [
            {"id": "x", "status": "down", "surge": False},
        ]}
        reported = {"updatedAt": now * 1000, "services": [
            {"id": "x", "status": "up", "surge": True},
        ]}

        self.assertEqual(
            [{"provider": "X.COM", "status": "down", "surfaces": ["WEB"],
              "order": len(ai_status.PROVIDERS)}],
            ai_status.x_status_alerts(down, now=now),
        )
        self.assertEqual("degraded", ai_status.x_status_alerts(reported, now=now)[0]["status"])
        self.assertEqual(["REPORTS"], ai_status.x_status_alerts(reported, now=now)[0]["surfaces"])

    def test_healthy_x_is_available_as_a_pinned_status(self):
        now = 1_800_000_000.0
        payload = {"updatedAt": now * 1000, "services": [
            {"id": "x", "status": "up", "surge": False},
        ]}

        self.assertEqual([], ai_status.x_status_alerts(payload, now=now))
        item = ai_status.x_status_item(payload, now=now)
        self.assertEqual("X.COM", item["provider"])
        self.assertEqual("operational", item["status"])

    def test_stale_x_status_does_not_alert(self):
        now = 1_800_000_000.0
        payload = {"updatedAt": (now - ai_status.SOURCE_MAX_AGE_S - 1) * 1000,
                   "services": [{"id": "x", "status": "down"}]}
        self.assertEqual([], ai_status.x_status_alerts(payload, now=now))

    def test_google_needs_two_consecutive_failures(self):
        self.assertEqual([], ai_status.google_status_alerts(1))
        self.assertEqual("operational", ai_status.google_status_item(1)["status"])
        alert = ai_status.google_status_alerts(2)[0]
        self.assertEqual("GOOGLE", alert["provider"])
        self.assertEqual("down", alert["status"])
        self.assertEqual(["WEB"], alert["surfaces"])

    def test_carousel_puts_incidents_first_without_duplicate_pinned_items(self):
        alerts = [
            {"provider": "OPENAI", "status": "down", "surfaces": ["CHAT"]},
            {"provider": "X.COM", "status": "degraded", "surfaces": ["REPORTS"]},
        ]
        pinned = [
            {"provider": "X.COM", "status": "degraded", "surfaces": ["REPORTS"]},
            {"provider": "GOOGLE", "status": "operational", "surfaces": ["WEB"]},
        ]

        items = ai_status.carousel_items(alerts, pinned)

        self.assertEqual(["OPENAI", "X.COM", "GOOGLE"],
                         [item["provider"] for item in items])


class BusyInputTest(unittest.TestCase):
    def test_shared_stream_preserves_monitor_connection_state_and_usb_binding(self):
        monitor = ai_status.Monitor(object(), threading.Lock(),
                                    input_url="ws://10.0.4.20/api/status/ws",
                                    input_source_address="10.0.4.21")
        stop = threading.Event()

        def observe_state(_stop):
            options = stream.call_args.kwargs
            options["on_state"](True, "")
            self.assertTrue(monitor.input_connected)
            options["on_state"](False, "device unplugged")
            self.assertFalse(monitor.input_connected)
            self.assertEqual("device unplugged", monitor.input_error)

        with mock.patch.object(ai_status, "InputStream") as stream:
            stream.return_value.run.side_effect = observe_state
            monitor._input_loop(stop)
        self.assertEqual("10.0.4.21", stream.call_args.kwargs["source_address"])
        self.assertEqual(monitor.handle_input_event, stream.call_args.args[1])

    def test_input_stream_url_matches_local_sdk_contract(self):
        self.assertEqual(
            "ws://10.0.4.20/api/status/ws",
            ai_status.input_stream_url("http://10.0.4.20/api"),
        )
        self.assertEqual(
            "wss://bar.local/api/status/ws?x-api-token=12%2034",
            ai_status.input_stream_url("https://bar.local/api", "12 34"),
        )

    @staticmethod
    def wrap_input_event(event):
        # State.updates(2) -> StateUpdate.input(11) -> InputEvent event.
        tag = {"button": 0x0A, "switch": 0x12, "encoder": 0x1A}[event[0]]
        input_event = bytes((tag, len(event[1]))) + event[1]
        update = bytes((0x5A, len(input_event))) + input_event
        return bytes((0x12, len(update))) + update

    def test_parses_button_press_and_ignores_no_fields(self):
        # START=2; PRESS=0 is the proto3 default and is omitted.
        state = self.wrap_input_event(("button", bytes((0x08, 0x02))))
        self.assertEqual([("button", 2, 0)], ai_status.parse_input_events(state))

        # OK=0 and PRESS=0: even an empty nested message is a real event.
        state = self.wrap_input_event(("button", b""))
        self.assertEqual([("button", 0, 0)], ai_status.parse_input_events(state))

    def test_parses_signed_encoder_delta(self):
        positive = self.wrap_input_event(("encoder", bytes((0x08, 0x02))))
        negative = self.wrap_input_event(("encoder", bytes((0x08, 0x01))))
        self.assertEqual([("encoder", 1)], ai_status.parse_input_events(positive))
        self.assertEqual([("encoder", -1)], ai_status.parse_input_events(negative))

    def test_parses_switch_position(self):
        state = self.wrap_input_event(("switch", bytes((0x08, 0x03))))
        self.assertEqual([("switch", 3)], ai_status.parse_input_events(state))

    def test_buttons_select_items_and_ok_requests_refresh(self):
        class NullTransport:
            pass

        monitor = ai_status.Monitor(NullTransport(), __import__("threading").Lock())
        monitor.alerts = [
            {"provider": "OPENAI", "status": "degraded", "surfaces": ["CHAT"]},
        ]
        monitor.pinned = [
            {"provider": "ANTHROPIC", "status": "operational", "surfaces": ["ALL"]},
            {"provider": "X.COM", "status": "operational", "surfaces": ["WEB"]},
        ]

        self.assertTrue(monitor.handle_input_event(("button", 2, 0), now=0))
        self.assertEqual(1, monitor.selected_index)
        self.assertTrue(monitor.handle_input_event(("encoder", -1), now=1))
        self.assertEqual(0, monitor.selected_index)
        self.assertFalse(monitor.handle_input_event(("button", 1, 0), now=2))
        self.assertTrue(monitor.handle_input_event(("button", 0, 0), now=3))
        self.assertTrue(monitor.refresh_requested.is_set())
        self.assertTrue(monitor.redraw_requested.is_set())

    def test_carousel_shows_every_item_in_order(self):
        stop = threading.Event()

        class RecordingTransport:
            def __init__(self):
                self.providers = []

            def draw(self, payload):
                provider = next((element["text"] for element in payload["elements"]
                                 if element["id"] == "alert_provider"), None)
                if provider and (not self.providers or self.providers[-1] != provider):
                    self.providers.append(provider)
                    if len(self.providers) == 5:
                        stop.set()
                return True

            def clear(self, _app_name):
                return True

        transport = RecordingTransport()
        monitor = ai_status.Monitor(transport, threading.Lock())
        alerts = [
            {"provider": "OPENAI", "status": "degraded", "surfaces": ["CHAT"]},
        ]
        monitor.pinned = [
            {"provider": "ANTHROPIC", "status": "operational", "surfaces": ["ALL"]},
            {"provider": "X.COM", "status": "operational", "surfaces": ["WEB"]},
            {"provider": "GOOGLE", "status": "operational", "surfaces": ["WEB"]},
        ]
        monitor.fetch = lambda now=None: (alerts, [])

        with mock.patch.object(ai_status, "ROTATE_S", 0.05):
            monitor.run(stop)

        self.assertEqual(
            ["OPENAI", "ANTHROPIC", "X.COM", "GOOGLE", "OPENAI"],
            transport.providers,
        )


class MonitorShutdownTest(unittest.TestCase):
    def test_stop_while_waiting_for_render_lock_cannot_repaint_cleared_canvas(self):
        stop = threading.Event()
        waiting = threading.Event()
        render_lock = threading.Lock()
        failures = []

        class ObservedLock:
            def __enter__(self):
                waiting.set()
                if not render_lock.acquire(timeout=1):
                    raise TimeoutError("test renderer did not release its lock")

            def __exit__(self, *_):
                render_lock.release()

        transport = mock.Mock()
        monitor = ai_status.Monitor(transport, ObservedLock())
        monitor.drawn = True
        monitor.fetch = lambda now=None: ([{
            "provider": "OPENAI", "status": "down", "surfaces": ["API"],
        }], [])

        def run():
            try:
                monitor.run(stop)
            except Exception as exc:
                failures.append(exc)

        # Hold the device lock as shutdown does, then let a pending render
        # catch up only after the canvas has been cleared and stop was set.
        render_lock.acquire()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(waiting.wait(timeout=1), "monitor never reached rendering")
            stop.set()
            transport.clear(ai_status.APP_NAME)
            monitor.drawn = False
        finally:
            stop.set()
            render_lock.release()
            worker.join(timeout=1)
        self.assertFalse(worker.is_alive(), "monitor did not stop promptly")
        self.assertEqual([], failures)
        transport.draw.assert_not_called()
        transport.clear.assert_called_once_with(ai_status.APP_NAME)


class AlertLayoutTest(unittest.TestCase):
    def test_down_alert_uses_outage_contour_and_compact_surface_count(self):
        alert = {"provider": "OPENAI", "status": "down",
                 "surfaces": ["API", "CHAT", "CODE"]}

        animation = ai_status.alert_animation("down")
        elements = ai_status.alert_elements(alert, 1, 3)

        self.assertEqual("outage.anim", animation[1]["path"])
        self.assertEqual("#000000FF", animation[0]["fill_colors"][0])
        by_id = {element["id"]: element for element in elements}
        self.assertEqual("2/3", by_id["alert_index"]["text"])
        self.assertEqual("DOWN", by_id["alert_state"]["text"])
        self.assertEqual("API+2", by_id["alert_surface"]["text"])

    def test_degraded_label_leaves_room_for_surface(self):
        alert = {"provider": "ANTHROPIC", "status": "degraded",
                 "surfaces": ["API", "CHAT", "CODE"]}
        by_id = {
            element["id"]: element
            for element in ai_status.alert_elements(alert, 0, 1)
        }
        self.assertEqual("DEGR", by_id["alert_state"]["text"])
        self.assertEqual("API+2", by_id["alert_surface"]["text"])

    def test_operational_item_uses_green_ok_treatment(self):
        item = {"provider": "GOOGLE", "status": "operational",
                "surfaces": ["WEB"]}
        animation = ai_status.alert_animation("operational")
        by_id = {
            element["id"]: element
            for element in ai_status.alert_elements(item, 3, 5)
        }

        self.assertEqual("healthy.anim", animation[1]["path"])
        self.assertEqual("+", by_id["alert_mark"]["text"])
        self.assertEqual("OK", by_id["alert_state"]["text"])
        self.assertEqual(ai_status.HEALTHY, by_id["alert_state"]["color"])


if __name__ == "__main__":
    unittest.main()
