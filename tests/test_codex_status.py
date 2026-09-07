import json
import pathlib
import tempfile
import unittest
from unittest import mock

from adapters import codex_status
import codex_usage
import daemon


class RolloutSnapshotTest(unittest.TestCase):
    def setUp(self):
        codex_status._ROLLOUT_CACHE.update({
            "path": None,
            "offset": 0,
            "snapshot": codex_status._empty_snapshot(),
        })

    @staticmethod
    def append(path: pathlib.Path, *events: dict) -> None:
        with path.open("a") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def test_lifecycle_and_model_come_only_from_structured_events(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = pathlib.Path(directory) / "rollout-test.jsonl"
            self.append(
                rollout,
                {"type": "turn_context", "payload": {
                    "model": "gpt-5.6-sol", "effort": "xhigh",
                }},
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "response_item", "payload": {
                    "type": "message",
                    "content": '{"model":"gpt-5.4","reasoning_effort":"medium"}',
                }},
                {"type": "event_msg", "payload": {
                    "type": "token_count",
                    "info": {"model_context_window": 1000,
                             "last_token_usage": {"total_tokens": 200}},
                    "rate_limits": {"primary": {
                        "used_percent": 17, "window_minutes": 10080,
                    }},
                }},
            )

            snapshot = codex_status._rollout_snapshot(rollout)
            self.assertEqual("gpt-5.6-sol", snapshot["model"])
            self.assertEqual("xhigh", snapshot["effort"])
            self.assertEqual("WORKING", snapshot["state"])
            self.assertNotIn("limits", snapshot)  # task history is not account usage

            self.append(
                rollout,
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            )
            self.assertEqual("COMPLETE", codex_status._rollout_snapshot(rollout)["state"])

            self.append(
                rollout,
                {"type": "event_msg", "payload": {"type": "task_started"}},
            )
            self.assertEqual("WORKING", codex_status._rollout_snapshot(rollout)["state"])

    def test_task_reports_use_account_snapshot_and_hooks_do_not_overwrite_it(self):
        usage = {'quotas': [{'name': '7d', 'left_pct': 62}],
                 'quota_status': {'source': 'codex-account'}}
        with mock.patch.object(codex_status, 'newest_rollout', return_value=None), \
             mock.patch.object(codex_status, 'config_defaults', return_value={'model': 'gpt-test'}):
            self.assertEqual(usage['quotas'], codex_status.probe(usage)['quotas'])
            self.assertNotIn('quotas', codex_status.probe())

    def test_new_account_usage_is_reported_without_rollout_activity(self):
        values = iter((38, 45))
        monitor = codex_usage.Monitor(fetch=lambda _: {'rateLimits': {'primary': {
            'usedPercent': next(values), 'windowDurationMins': 10080}}})
        monitor.refresh()
        stop = mock.Mock()
        stop.is_set.side_effect = (False, False, True)
        waits = []
        def wait(_):
            if not waits:
                monitor.refresh()
            waits.append(True)
        stop.wait.side_effect = wait
        with mock.patch.object(codex_status, 'newest_rollout', return_value=None), \
             mock.patch.object(codex_status, '_emit') as emit:
            codex_status.watch(monitor, stop, False)
        self.assertEqual([62, 55], [call.args[1]['quotas'][0]['left_pct']
                                   for call in emit.call_args_list])


class SelectionHandoffTest(unittest.TestCase):
    def watch_selection(self, *, fail_new=False, fail_cleanup=False):
        store = daemon.Store()
        target = [{'kind': 'cli', 'thread_id': 'first', 'model': 'gpt-test',
                   'effort': 'high', 'state': 'WORKING', 'ready': True}]
        stop = mock.Mock()
        stop.is_set.side_effect = (False, False, False, True)
        stop.wait.side_effect = lambda _: target[0].update(thread_id='second', state='IDLE')
        monitor = codex_usage.Monitor(fetch=lambda _: {})
        attempts = []
        active = []

        def send(payload):
            attempts.append(dict(payload))
            if fail_new and payload['session_id'] == 'second' and not payload.get('ended'):
                return False
            if fail_cleanup and payload.get('ended') and len([p for p in attempts if p.get('ended')]) == 1:
                return False
            store.report(payload['source'], payload['session_id'], payload)
            active.append(store.active_session())
            return True

        with mock.patch.object(codex_status, 'selected_target', side_effect=lambda: dict(target[0])), \
             mock.patch.object(codex_status, 'newest_rollout', return_value=None), \
             mock.patch.object(codex_status, 'config_defaults', return_value={}), \
             mock.patch.object(codex_status, 'post', side_effect=send):
            codex_status.watch(monitor, stop, False)
        return store, attempts, active

    def test_new_session_arrives_before_old_one_is_retired(self):
        store, _, active = self.watch_selection()
        self.assertTrue(all(active), 'An empty store exposes the firmware menu')
        self.assertEqual({'codex:second'}, set(store.sessions))

    def test_failed_new_report_preserves_display_and_retries(self):
        store, attempts, active = self.watch_selection(fail_new=True)
        self.assertTrue(all(active))
        self.assertEqual({'codex:first'}, set(store.sessions))
        self.assertEqual(2, len([p for p in attempts if p['session_id'] == 'second']))
        self.assertFalse(any(p.get('ended') for p in attempts))

    def test_failed_retirement_is_retried_without_an_empty_display(self):
        store, _, active = self.watch_selection(fail_cleanup=True)
        self.assertTrue(all(active))
        self.assertEqual({'codex:second'}, set(store.sessions))

    def test_probe_uses_one_selection_snapshot(self):
        selected = {'kind': 'cli', 'thread_id': 'selected', 'model': 'gpt-test',
                    'effort': 'high', 'ready': True}
        with mock.patch.object(codex_status, 'selected_target', side_effect=AssertionError('Selection raced')), \
             mock.patch.object(codex_status, 'newest_rollout', return_value=None), \
             mock.patch.object(codex_status, 'config_defaults', return_value={}):
            self.assertEqual('selected', codex_status.probe(selection=selected)['session_id'])


if __name__ == "__main__":
    unittest.main()
