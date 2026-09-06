import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import daemon


class WeekProgressTest(unittest.TestCase):
    def test_progress_fills_toward_reset(self):
        now = 1_000_000.0
        self.assertEqual(
            0.0,
            daemon.week_progress_pct(
                {"resets_at": now + daemon.WEEK_SECONDS}, now,
            ),
        )
        self.assertEqual(
            50.0,
            daemon.week_progress_pct(
                {"resets_at": now + daemon.WEEK_SECONDS / 2}, now,
            ),
        )
        self.assertGreater(
            daemon.week_progress_pct({"resets_at": now + 1}, now),
            99.9,
        )

    def test_expired_or_missing_reset_is_unknown(self):
        now = 1_000_000.0
        self.assertIsNone(daemon.week_progress_pct({"resets_at": now}, now))
        self.assertIsNone(daemon.week_progress_pct({}, now))

    def test_short_window_is_never_selected_as_weekly(self):
        short = {'name': '5h', 'window_minutes': 300, 'left_pct': 50,
                 'resets_at': 1_010_000}
        self.assertIsNone(daemon.weekly_quota([short]))
        self.assertIsNone(daemon.week_progress_pct(short, 1_000_000))
        self.assertIsNone(daemon.weekly_quota([{**short, 'name': '7d'}]))

    def test_expired_data_never_becomes_a_full_quota(self):
        store = daemon.Store()
        store.report('test', 'a', {'state': 'WORKING', 'quotas': [
            {'name': '7d', 'left_pct': 12, 'resets_at': 99},
            {'name': '5h', 'left_pct': 30, 'resets_at': 999, 'valid_until': 99}]})
        with mock.patch.object(daemon, 'STORE', store), \
             mock.patch.object(daemon, 'EFFORT_CONTROLLER', None), \
             mock.patch.object(daemon.time, 'time', return_value=100):
            snapshot = daemon.status_snapshot()
        self.assertEqual([None, None], [q['left_pct'] for q in snapshot['quotas']])
        self.assertIsNone(snapshot['week_progress_pct'])

    def test_progress_fill_is_cleared_on_reset_missing_data_and_zero_progress(self):
        now = 1_000_000
        for style in ('minimal', 'avatar'):
            with mock.patch.object(daemon, 'STYLE', style), \
                 mock.patch.object(daemon.time, 'time', return_value=now):
                for quotas in (None,
                    [{'name': '7d', 'left_pct': 25, 'resets_at': now}],
                    [{'name': '7d', 'left_pct': None, 'resets_at': now + 500}],
                    [{'name': '7d', 'left_pct': 100, 'resets_at': now + daemon.WEEK_SECONDS}]):
                    elements = daemon.info_elements({'state': 'IDLE', 'quotas': quotas})
                    fill = next(e for e in elements if e['id'] == 'cfill')
                    self.assertEqual(['#00000000'], fill['fill_colors'])

    def test_quota_parser_preserves_freshness_and_rejects_invalid_numbers(self):
        valid = {'name': '7d', 'left_pct': 62.5, 'window_minutes': 10080,
                 'resets_at': 2000, 'observed_at': 1000, 'valid_until': 1180}
        self.assertEqual([valid], daemon.parse_quotas([valid]))
        self.assertIsNone(daemon.parse_quotas('bad'))
        for value in (float('nan'), float('inf'), True, '90', None):
            self.assertIsNone(daemon.parse_quotas([{'left_pct': value}])[0]['left_pct'])


class FastContourTest(unittest.TestCase):
    def test_fast_changes_only_the_working_contour(self):
        self.assertEqual("work.anim", daemon.anim_element("WORKING")["path"])
        self.assertEqual(
            "work_fast.anim",
            daemon.anim_element("WORKING", ["fast"])["path"],
        )
        self.assertEqual(
            "wait.anim",
            daemon.anim_element("WAIT", ["fast"])["path"],
        )

    def test_astra_availability_overrides_agent_contour(self):
        self.assertEqual(
            "astra.anim",
            daemon.anim_element("WORKING", ["fast"], "available")["path"],
        )
        self.assertEqual(
            "astra.anim",
            daemon.anim_element("COMPLETE", None, "available")["path"],
        )


class AstraIndicatorTest(unittest.TestCase):
    def test_availability_colors_and_staleness(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps({
                "last_state": "waiting",
                "last_checked_at": "2026-09-04T06:00:00+00:00",
                "client_version": "0.151.0",
                "model_count": 9,
            }))
            os.utime(state_file, (1_000_000, 1_000_000))
            waiting = daemon.astra_availability(str(state_file), 1_000_001)
            stale = daemon.astra_availability(
                str(state_file), 1_000_000 + daemon.ASTRA_STALE_S + 1,
            )
            state_file.write_text(json.dumps({"last_state": "available"}))
            available = daemon.astra_availability(str(state_file))

        self.assertEqual("waiting", waiting["state"])
        self.assertEqual(daemon.ASTRA_COLORS["waiting"], waiting["color"])
        self.assertEqual("0.151.0", waiting["client_version"])
        self.assertEqual(9, waiting["model_count"])
        self.assertEqual(1.0, waiting["age_s"])
        self.assertEqual(round(daemon.ASTRA_CHECK_INTERVAL_S - 1),
                         waiting["next_check_s"])
        self.assertEqual("stale", stale["state"])
        self.assertEqual("available", available["state"])
        self.assertEqual(daemon.ASTRA_COLORS["available"], available["color"])

    def test_missing_watcher_is_transparent(self):
        status = daemon.astra_availability("/path/that/does/not/exist")
        self.assertEqual("unknown", status["state"])
        self.assertEqual("#00000000", status["color"])

    def test_pixel_a_fits_between_quota_and_state(self):
        elements = daemon.info_elements(
            {"state": "WORKING"},
            {"state": "waiting", "color": daemon.ASTRA_COLORS["waiting"]},
        )
        glyph = {element["id"]: element for element in elements
                 if element["id"].startswith("astra_")}
        self.assertEqual(
            {"astra_top", "astra_left", "astra_right", "astra_cross"},
            set(glyph),
        )
        self.assertGreaterEqual(min(element["x"] for element in glyph.values()), 46)
        self.assertLessEqual(
            max(element["x"] + element["width"] for element in glyph.values()), 49,
        )

    def test_refresh_starts_watcher_once_within_debounce(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "astra_watch.py"
            script.write_text("# test")
            previous_script = daemon.ASTRA_WATCH_SCRIPT
            previous_refresh = daemon.ASTRA_REFRESHED_AT
            daemon.ASTRA_WATCH_SCRIPT = str(script)
            daemon.ASTRA_REFRESHED_AT = 0
            try:
                with mock.patch.object(daemon.subprocess, "Popen") as popen:
                    first = daemon.request_astra_refresh(now=100)
                    second = daemon.request_astra_refresh(now=101)
            finally:
                daemon.ASTRA_WATCH_SCRIPT = previous_script
                daemon.ASTRA_REFRESHED_AT = previous_refresh

        self.assertEqual((True, "refresh started"), first)
        self.assertEqual((True, "already refreshing"), second)
        popen.assert_called_once()
        self.assertNotIn("exec", popen.call_args.args[0])

    def test_apps_and_settings_modes_release_agent_canvas(self):
        previous_mode = daemon.DEVICE_MODE
        try:
            self.assertTrue(daemon.handle_device_input_event(("switch", 3)))
            self.assertFalse(daemon.device_canvas_allowed())
            self.assertTrue(daemon.handle_device_input_event(("switch", 4)))
            self.assertFalse(daemon.device_canvas_allowed())
            self.assertTrue(daemon.handle_device_input_event(("switch", 1)))
            self.assertTrue(daemon.device_canvas_allowed())
        finally:
            daemon.DEVICE_MODE = previous_mode

    def test_ok_refreshes_only_while_astra_app_is_polling(self):
        previous_seen = daemon.ASTRA_APP_LAST_SEEN
        previous_requests = daemon.ASTRA_APP_REQUESTS
        try:
            daemon.ASTRA_APP_LAST_SEEN = 0
            daemon.ASTRA_APP_REQUESTS = 0
            with mock.patch.object(daemon, "request_astra_refresh") as refresh:
                self.assertFalse(daemon.handle_device_input_event(("button", 0, 0)))
                daemon.handle_astra_app_poll(now=100)
                daemon.handle_astra_app_poll(now=101)
                with mock.patch.object(daemon.time, "monotonic", return_value=101):
                    self.assertFalse(daemon.device_canvas_allowed())
                    self.assertTrue(daemon.handle_device_input_event(("button", 0, 0)))
                self.assertEqual(2, refresh.call_count)
            self.assertEqual({
                "active": False,
                "last_seen_age_s": 6.0,
                "requests": 2,
            }, daemon.astra_app_status(now=107))
        finally:
            daemon.ASTRA_APP_LAST_SEEN = previous_seen
            daemon.ASTRA_APP_REQUESTS = previous_requests


class EffortRenderOrderTest(unittest.TestCase):
    def test_confirmed_detent_is_sent_before_dashboard_refreshes(self):
        import threading
        store = daemon.Store()
        store.report('codex', 'test', {'state': 'WORKING', 'control_thread_id': 'test'})
        stop = threading.Event()
        control = mock.Mock()
        control.status.return_value = {'thread_id': 'test', 'feedback': 'HIGH',
                                       'feedback_revision': 1, 'direction': 1}
        batches = []

        def draw(payload):
            batches.append(payload['elements'])
            stop.set()
            store.dirty.set()
            return True

        transport = mock.Mock()
        transport.draw.side_effect = draw
        with mock.patch.object(daemon, 'STORE', store), \
             mock.patch.object(daemon, 'EFFORT_CONTROLLER', control), \
             mock.patch.object(daemon, 'AI_MONITOR', None), \
             mock.patch.object(daemon, 'HUBLINK', None), \
             mock.patch.object(daemon, 'REDRAW', threading.Event()), \
             mock.patch.object(daemon, 'DRAWN', threading.Event()), \
             mock.patch.object(daemon, 'RENDER_MODE', 'auto'), \
             mock.patch.object(daemon, 'device_canvas_allowed', return_value=True), \
             mock.patch.object(daemon, 'status_snapshot', return_value={'state': 'WORKING'}):
            daemon.render_loop(transport, stop)
        self.assertEqual('effort_transition', batches[0][0]['id'])
        control.mark_drawn.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
