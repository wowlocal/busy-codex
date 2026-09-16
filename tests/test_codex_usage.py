import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock

import codex_usage as usage


def bucket(used=38, minutes=10080, reset=2000):
    return {'limitId': 'codex', 'primary': {
        'usedPercent': used, 'windowDurationMins': minutes, 'resetsAt': reset}}


class AccountUsageTest(unittest.TestCase):
    def test_account_bucket_wins_over_legacy_and_model_specific_buckets(self):
        result = {'rateLimits': bucket(0), 'rateLimitsByLimitId': {
            'codex': bucket(), 'codex_other': bucket(99)}}
        quotas = usage.account_quotas(result, 1000)
        self.assertEqual([{'name': '7d', 'left_pct': 62, 'window_minutes': 10080,
                           'resets_at': 2000, 'observed_at': 1000, 'valid_until': 1180}], quotas)
        del result['rateLimitsByLimitId']['codex']
        with self.assertRaises(ValueError):
            usage.account_quotas(result, 1000)

    def test_week_can_be_primary_or_secondary_and_null_is_not_zero(self):
        value = bucket(minutes=300)
        value['secondary'] = bucket(used=10)['primary']
        self.assertEqual(['5h', '7d'], [q['name'] for q in
            usage.account_quotas({'rateLimits': value}, 1000)])
        for invalid in (None, float('nan'), float('inf'), True, '38'):
            with self.assertRaises(ValueError):
                usage.account_quotas({'rateLimits': bucket(invalid)}, 1000)
        legacy_other = bucket()
        legacy_other['limitId'] = 'other'
        with self.assertRaises(ValueError):
            usage.account_quotas({'rateLimits': legacy_other}, 1000)

    def test_failed_refresh_retains_known_window_until_reset_and_recovers(self):
        now = [1000]
        messages = []
        results = mock.Mock(side_effect=[{'rateLimits': bucket()},
                                       OSError('offline'), {'rateLimits': bucket(42)}])
        monitor = usage.Monitor(fetch=results, clock=lambda: now[0], logger=messages.append)
        monitor.refresh()
        self.assertEqual('fresh', monitor.snapshot()['quota_status']['state'])
        now[0] += 60
        monitor.refresh()
        self.assertEqual('cached', monitor.snapshot()['quota_status']['state'])
        self.assertEqual(1000, monitor.snapshot()['quotas'][0]['observed_at'])
        self.assertEqual(15, monitor.next_delay())
        now[0] = 1180
        self.assertEqual('cached', monitor.snapshot()['quota_status']['state'])
        self.assertEqual(62, monitor.snapshot()['quotas'][0]['left_pct'])
        monitor.refresh()
        self.assertEqual('fresh', monitor.snapshot()['quota_status']['state'])
        self.assertEqual(58, monitor.snapshot()['quotas'][0]['left_pct'])
        self.assertIn('Codex quota refresh failed: offline', messages[0])
        self.assertEqual('Codex quota refresh recovered', messages[1])

    def test_cached_window_becomes_unavailable_only_at_reset(self):
        now = [1000]
        monitor = usage.Monitor(fetch=mock.Mock(side_effect=[
            {'rateLimits': bucket(reset=1200)}, OSError('offline')]),
            clock=lambda: now[0])
        monitor.refresh()
        monitor.refresh()
        now[0] = 1199
        self.assertEqual('cached', monitor.snapshot()['quota_status']['state'])
        now[0] = 1200
        self.assertEqual('unavailable', monitor.snapshot()['quota_status']['state'])

    def test_window_without_reset_still_uses_bounded_freshness(self):
        now = [1000]
        monitor = usage.Monitor(fetch=lambda _: {'rateLimits': bucket(reset=None)},
                                clock=lambda: now[0])
        monitor.refresh()
        now[0] = 1180
        self.assertEqual('unavailable', monitor.snapshot()['quota_status']['state'])

    def test_reset_schedules_early_refresh_without_inventing_new_window(self):
        now = [1000]
        monitor = usage.Monitor(fetch=lambda _: {'rateLimits': bucket(reset=1004)},
                                clock=lambda: now[0])
        monitor.refresh()
        self.assertEqual(5, monitor.next_delay())
        now[0] = 1004
        self.assertEqual('unavailable', monitor.snapshot()['quota_status']['state'])
        self.assertEqual(1004, monitor.snapshot()['quotas'][0]['resets_at'])
        self.assertEqual(15, monitor.next_delay())

    def test_stdio_reader_only_initializes_and_reads_limits_then_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, 'fake-codex')
            calls = Path(directory, 'calls.jsonl')
            script.write_text('#!/usr/bin/env python3\n' +
                'import sys,json\n' +
                f'with open({str(calls)!r}, "w") as out:\n' +
                ' for line in sys.stdin:\n' +
                '  msg=json.loads(line);out.write(line);out.flush()\n' +
                '  if "id" in msg:\n' +
                '   print(json.dumps({"id":msg["id"],"result":{}}),flush=True)\n')
            script.chmod(0o755)
            env = dict(os.environ, BUSYBAR_CODEX_BIN=str(script))
            self.assertEqual({}, usage.read_rate_limits(env))
            self.assertEqual(['initialize', 'initialized', 'account/rateLimits/read'],
                             [json.loads(line)['method'] for line in calls.read_text().splitlines()])
            stop = threading.Event()
            stop.set()
            with self.assertRaises(InterruptedError):
                usage.read_rate_limits(env, stop)

    def test_each_stdio_request_gets_its_own_timeout_budget(self):
        replies = queue.Queue()
        replies.put({'id': 1, 'result': {}})
        replies.put({'id': 2, 'result': {}})
        sent = []
        # The second request starts after the first request's original
        # deadline. It still succeeds because it receives a new budget.
        clock = mock.Mock(side_effect=[0, 7, 10, 17])
        self.assertEqual({}, usage.request(replies, sent.append, 1, 'initialize',
                                           timeout=8, clock=clock))
        self.assertEqual({}, usage.request(replies, sent.append, 2, 'account/read',
                                           timeout=8, clock=clock))
        self.assertEqual(['initialize', 'account/read'], [item['method'] for item in sent])


if __name__ == '__main__':
    unittest.main()
