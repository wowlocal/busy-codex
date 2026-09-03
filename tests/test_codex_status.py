import json
import pathlib
import tempfile
import unittest

from adapters import codex_status


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
            self.assertEqual(17, snapshot["limits"]["primary"]["used_percent"])

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


if __name__ == "__main__":
    unittest.main()
