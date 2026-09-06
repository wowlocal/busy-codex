import base64
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

import codex_cli as cli
from codex_cli_title import TitleOutput, thread_prefix
from codex_cli_client import CLIIPC
from codex_effort import Controller
import codex_target as target
from adapters import codex_status

THREAD = '11111111-2222-3333-4444-555555555555'

FAKE_SERVER = r'''#!/usr/bin/env python3
import json, sys
settings = {'model': 'test-model', 'effort': 'high', 'serviceTier': 'fast',
    'collaborationMode': {'mode': 'plan', 'settings': {'model': 'test-model',
    'reasoning_effort': 'high', 'developer_instructions': 'keep this'}}}
for line in sys.stdin:
    m = json.loads(line)
    if 'id' not in m: continue
    method = m['method']
    result = {}
    if method == 'model/list':
        result = {'data': [{'model': 'test-model', 'supportedReasoningEfforts':
            [{'reasoningEffort': effort} for effort in ['low','high','xhigh']]}]}
    elif method == 'thread/start':
        result = {'thread': {'id': '11111111-2222-3333-4444-555555555555'},
                  'model': settings['model'], 'reasoningEffort': settings['effort'], 'serviceTier': 'fast'}
    elif method == 'thread/settings/update':
        assert set(m['params']) == {'threadId', 'effort', 'collaborationMode'}
        assert m['params']['collaborationMode']['mode'] == 'plan'
        assert m['params']['collaborationMode']['settings']['developer_instructions'] == 'keep this'
        settings.update({k:v for k,v in m['params'].items() if k != 'threadId'})
    elif method == 'echo':
        result = m['params']
    print(json.dumps({'id': m['id'], 'result': result}), flush=True)
    if method in ('thread/start', 'thread/settings/update'):
        print(json.dumps({'method': 'thread/settings/updated', 'params':
            {'threadId': '11111111-2222-3333-4444-555555555555', 'threadSettings': settings}}), flush=True)
'''


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='busy-test-', dir='/tmp')
        directory = Path(self.temp.name)
        binary = directory / 'codex'
        binary.write_text(FAKE_SERVER)
        binary.chmod(0o700)
        self.bridge = cli.Bridge(str(binary), directory,
            {'pid': os.getpid(), 'terminal': 'ghostty', 'focused': True})
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(3)
        self.sock.connect(str(self.bridge.socket_path))
        key = base64.b64encode(os.urandom(16))
        self.sock.sendall(b'GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          b'Sec-WebSocket-Key: ' + key + b'\r\n\r\n')
        headers = b''
        while not headers.endswith(b'\r\n\r\n'):
            headers += cli.read_exact(self.sock, 1)
        self.assertIn(b'101 Switching Protocols', headers)
        self.sequence = 0
        self.events = []
        self.rpc('initialize', {'clientInfo': {'name': 'test', 'version': '1'}})
        self.rpc('model/list', {})
        self.rpc('thread/start', {})
        # Read the full initial settings notification before changing effort.
        notification = json.loads(cli.read_frame(self.sock)[2])
        self.assertEqual('thread/settings/updated', notification['method'])
        self.bridge.observe_title(THREAD[:29] + '... | test-model | high')

    def tearDown(self):
        self.sock.close()
        self.bridge.close()
        self.assertIsNotNone(self.bridge.proc.poll())
        self.assertFalse(self.bridge.reader.is_alive())
        self.temp.cleanup()

    def rpc(self, method, params):
        self.sequence += 1
        rid = self.sequence
        payload = json.dumps({'id': rid, 'method': method, 'params': params}).encode()
        # Real WebSocket clients mask their frames, including extended lengths.
        mask = b'abcd'
        size = len(payload)
        frame = bytes((129, 128 | (size if size < 126 else 126)))
        if size >= 126:
            frame += struct.pack('!H', size)
        frame += mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        self.sock.sendall(frame)
        while True:
            message = json.loads(cli.read_frame(self.sock)[2])
            if message.get('id') == rid:
                return message['result']
            self.events.append(message)

    def test_transparent_request_ids_and_no_conversation_in_registry(self):
        private = {'input': 'private prompt' * 500}
        self.assertEqual(private, self.rpc('echo', private))
        self.assertNotIn('private prompt', self.bridge.record_path.read_text())
        self.assertEqual({}, self.bridge.pending)

    def test_dial_changes_live_cli_and_preserves_other_settings(self):
        self.exercise_dial()

    def exercise_dial(self):
        info = {'thread_id': THREAD, 'kind': 'cli', 'socket': str(self.bridge.control_path)}
        controller = Controller(lambda: THREAD, lambda: None, home=self.temp.name,
                                target_info=lambda: info)
        stop = threading.Event()
        worker = threading.Thread(target=controller.run, args=(stop,))
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while not controller.status()['connected'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(controller.rotate(1))
            deadline = time.monotonic() + 3
            while controller.status()['feedback'] != 'XHIGH' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual('xhigh', self.bridge.snapshot()['effort'])
            self.assertEqual('XHIGH', controller.status()['feedback'])
            settings = json.loads(cli.read_frame(self.sock)[2])['params']['threadSettings']
            self.assertEqual('xhigh', settings['effort'])
            self.assertEqual('fast', settings['serviceTier'])
            # The same UUID in a different app is a different control target.
            info.update(kind='desktop', socket=None)
            self.assertFalse(controller.rotate(-1))
        finally:
            stop.set(); worker.join(4)
            self.assertFalse(worker.is_alive())

    def test_stale_control_request_is_rejected_without_mutation(self):
        client = CLIIPC(self.bridge.control_path, lambda _: None)
        try:
            client.connect(THREAD)
            client.state['effort'] = 'low'
            with self.assertRaisesRegex(ValueError, 'settings changed'):
                client.request('thread-follower-update-thread-settings', {'threadSettings': {'effort': 'xhigh'}})
            self.assertEqual('high', self.bridge.snapshot()['effort'])
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                client.rpc({'method': 'turn/start', 'input': 'no'})
        finally:
            client.close()

    def test_missing_ack_is_reconciled_only_from_confirmed_live_settings(self):
        client = CLIIPC(self.bridge.control_path, lambda _: None)
        client.connect(THREAD)
        real_request = self.bridge.request_backend
        def lost_ack(method, params):
            real_request(method, params)
            with self.bridge.condition:
                self.assertTrue(self.bridge.condition.wait_for(
                    lambda: self.bridge.state['effort'] == 'xhigh', timeout=2))
            raise TimeoutError('CLI settings request timed out')
        try:
            with mock.patch.object(self.bridge, 'request_backend', side_effect=lost_ack) as write:
                result = client.request('thread-follower-update-thread-settings',
                                        {'threadSettings': {'effort': 'xhigh'}})
            self.assertEqual('xhigh', result['result']['effort'])
            self.assertEqual(1, write.call_count)
        finally:
            client.close()

    def test_timeout_without_matching_live_state_is_still_an_error(self):
        client = CLIIPC(self.bridge.control_path, lambda _: None)
        client.connect(THREAD)
        try:
            with mock.patch.object(self.bridge, 'request_backend',
                                   side_effect=TimeoutError('CLI settings request timed out')) as write:
                with self.assertRaisesRegex(ValueError, 'timed out'):
                    client.request('thread-follower-update-thread-settings',
                                   {'threadSettings': {'effort': 'xhigh'}})
            self.assertEqual('high', self.bridge.snapshot()['effort'])
            self.assertEqual(1, write.call_count)
        finally:
            client.close()

    def test_timeout_cannot_confirm_a_different_visible_task(self):
        client = CLIIPC(self.bridge.control_path, lambda _: None)
        client.connect(THREAD)
        def changed_selection(method, params):
            self.bridge.observe_title('no selected task')
            raise TimeoutError('CLI settings request timed out')
        try:
            with mock.patch.object(self.bridge, 'request_backend', side_effect=changed_selection):
                with self.assertRaisesRegex(ValueError, 'timed out'):
                    client.request('thread-follower-update-thread-settings',
                                   {'threadSettings': {'effort': 'xhigh'}})
        finally:
            client.close()

    def test_rpc_error_diagnostics_do_not_expose_server_payload(self):
        message = cli.settings_error({'code': -32600,
            'message': 'thread not found: private-content', 'data': 'private-content'})
        self.assertIn('RPC -32600; task is not loaded', message)
        self.assertNotIn('private-content', message)
        unknown = cli.settings_error({'message': 'private-content'})
        self.assertIn('unclassified', unknown)
        self.assertNotIn('private-content', unknown)

    def test_background_history_reads_leave_visible_task_controllable(self):
        self.rpc('thread/read', {'threadId': 'other-view'})
        self.assertTrue(self.bridge.snapshot()['ready'])
        self.assertEqual(THREAD, self.bridge.snapshot()['thread_id'])
        self.exercise_dial()

    def test_cached_view_switch_uses_title_not_last_resumed_thread(self):
        other = 'aaaaaaaa-2222-3333-4444-555555555555'
        self.bridge.observe({'result': {'thread': {'id': other}, 'model': 'other-model',
                                      'reasoningEffort': 'low'}}, {'method': 'thread/resume'})
        self.assertEqual(THREAD, self.bridge.snapshot()['thread_id'])
        self.bridge.observe_title(other[:29] + '... | other-model')
        self.assertEqual(other, self.bridge.snapshot()['thread_id'])
        self.assertEqual('low', self.bridge.snapshot()['effort'])
        self.bridge.observe({'method': 'thread/settings/updated', 'params': {
            'threadId': THREAD, 'threadSettings': {'effort': 'xhigh',
                'collaborationMode': None}}})
        self.assertEqual('low', self.bridge.snapshot()['effort'])
        self.bridge.observe_title(THREAD[:29] + '... | test-model')
        self.assertEqual('xhigh', self.bridge.snapshot()['effort'])
        self.assertTrue(self.bridge.snapshot()['ready'])

    def test_unknown_title_blocks_writes_and_recovers_without_resume(self):
        request = {'expected_thread_id': THREAD, 'expected_model': 'test-model',
                   'expected_effort': 'high', 'effort': 'xhigh'}
        self.bridge.observe_title('other application')
        with self.assertRaisesRegex(ValueError, 'not ready'):
            self.bridge.set_effort(request)
        self.bridge.observe_title(THREAD)
        self.assertTrue(self.bridge.snapshot()['ready'])
        # Never guess if two loaded IDs happen to share the truncated prefix.
        other = THREAD[:-1] + '6'
        self.bridge.observe({'result': {'thread': {'id': other}}}, {'method': 'thread/start'})
        self.bridge.observe_title(THREAD[:29] + '...')
        self.assertFalse(self.bridge.snapshot()['ready'])
        self.bridge.observe_title(THREAD)
        self.assertTrue(self.bridge.snapshot()['ready'])

    def test_title_may_arrive_before_load_response(self):
        other = 'aaaaaaaa-2222-3333-4444-555555555555'
        self.bridge.observe_title(other)
        self.assertFalse(self.bridge.snapshot()['ready'])
        self.bridge.observe({'result': {'thread': {'id': other}, 'model': 'test-model',
                                      'reasoningEffort': 'low'}}, {'method': 'thread/start'})
        self.assertEqual(other, self.bridge.snapshot()['thread_id'])
        self.assertTrue(self.bridge.snapshot()['ready'])

    def test_closed_thread_cannot_be_selected_by_old_title(self):
        self.bridge.observe({'method': 'thread/closed', 'params': {'threadId': THREAD}})
        self.assertFalse(self.bridge.snapshot()['ready'])
        self.bridge.observe_title(THREAD)
        self.assertFalse(self.bridge.snapshot()['ready'])


class TitleTest(unittest.TestCase):
    def test_split_title_sequences_and_unrelated_output(self):
        titles = []
        parser = TitleOutput(titles.append)
        stream = (b'conversation ' + THREAD.encode() + b'\x1b[2J\x1b]0;'
                  + THREAD[:29].encode() + b'... | model\x07'
                  + b'\x1b]52;c;unrelated\x07\x1b]2;\x1b\\')
        for byte in stream:
            parser.feed(bytes([byte]))
        self.assertEqual([THREAD[:29] + '... | model', ''], titles)
        self.assertEqual(THREAD[:29], thread_prefix(titles[0]))
        self.assertIsNone(thread_prefix('text ' + THREAD + ' | ' + THREAD))
        self.assertIsNone(thread_prefix(THREAD[:20]))
        self.assertEqual(bytearray(), parser.payload)

    def test_oversized_title_disables_selection_and_parser_recovers(self):
        titles = []
        parser = TitleOutput(titles.append)
        parser.feed(b'\x1b]0;' + b'x' * 5000 + b'\x1b\\')
        self.assertEqual([''], titles)
        self.assertEqual(bytearray(), parser.payload)
        parser.feed(b'\x1b]0;' + THREAD.encode() + b'\x07')
        self.assertEqual(THREAD, titles[-1])


class ServicesTest(unittest.TestCase):
    def test_display_workers_are_rechecked_while_cli_is_idle(self):
        stop = threading.Event()
        checks = []
        def ensure_adapter():
            checks.append('adapter')
            if len(checks) == 2:
                stop.set()
        with mock.patch('report.ensure_daemon') as daemon, \
             mock.patch('adapters.codex_notify.ensure_adapter', side_effect=ensure_adapter):
            cli.keep_services_alive(stop, interval=0)
        self.assertEqual(2, daemon.call_count)

    def test_display_failure_does_not_end_cli_or_prevent_retry(self):
        stop = threading.Event()
        with mock.patch('report.ensure_daemon', side_effect=[OSError(), None]) as daemon, \
             mock.patch('adapters.codex_notify.ensure_adapter', side_effect=stop.set):
            cli.keep_services_alive(stop, interval=0)
        self.assertEqual(2, daemon.call_count)


class FocusTest(unittest.TestCase):
    def test_headless_foreground_reads_pid_again_after_app_switch(self):
        api = mock.Mock()
        front_pid = [101]
        def get_front(serial):
            serial._obj.low = front_pid[0]
            return 0
        def get_pid(serial, pid):
            pid._obj.value = serial._obj.low
            return 0
        api.GetFrontProcess.side_effect = get_front
        api.GetProcessPID.side_effect = get_pid
        self.assertEqual(101, target.frontmost_pid(api))
        front_pid[0] = 202
        self.assertEqual(202, target.frontmost_pid(api))
        api.GetFrontProcess.side_effect = lambda _: -600
        with self.assertRaisesRegex(OSError, 'foreground process unavailable'):
            target.frontmost_pid(api)

    def test_force_refresh_does_not_reuse_lock_screen_or_previous_app(self):
        foreground = ['com.apple.loginwindow']
        selector = target.Target(foreground=lambda: foreground[0])
        self.assertIsNone(selector.current())
        self.assertEqual('com.apple.loginwindow', selector.status()['foreground_bundle'])
        foreground[0] = 'com.openai.codex'
        with mock.patch.object(target.codex_focus.FOCUS, 'current', return_value=THREAD):
            self.assertEqual(THREAD, selector.current(force=True))
        foreground[0] = 'com.google.Chrome'
        self.assertIsNone(selector.current(force=True))
        self.assertEqual('com.google.Chrome', selector.status()['foreground_bundle'])

    def test_split_focus_sequences_and_paste_are_distinguished(self):
        changes = []
        reader = cli.FocusInput(changes.append)
        for data in (b'abc\x1b[', b'O', b'\x1b[200~', b'paste\x1b[I', b'\x1b[201~', b'\x1b[I'):
            reader.feed(data)
        self.assertEqual([False, True], changes)

    def test_foreground_selects_only_unique_focused_terminal(self):
        records = [{'thread_id': THREAD, 'terminal': 'ghostty', 'focused': True, 'ready': True, 'socket': '/tmp/a'},
                   {'thread_id': 'other', 'terminal': 'iTerm.app', 'focused': True, 'ready': True, 'socket': '/tmp/b'}]
        foreground = ['com.mitchellh.ghostty']
        selector = target.Target(foreground=lambda: foreground[0], sessions=lambda _: records)
        self.assertEqual(THREAD, selector.current())
        foreground[0] = 'com.google.Chrome'; selector.next_poll = 0
        self.assertIsNone(selector.current())
        self.assertEqual(THREAD, selector.display()['thread_id'])
        foreground[0] = 'com.openai.codex'; selector.next_poll = 0
        with mock.patch.object(target.codex_focus.FOCUS, 'current', return_value='desktop-task'):
            self.assertEqual('desktop-task', selector.current())
        records.append(dict(records[0], thread_id='ambiguous'))
        self.assertIsNone(target.choose_cli(records, 'ghostty'))
        records[-1]['focused'] = False
        self.assertEqual(THREAD, target.choose_cli(records, 'ghostty')['thread_id'])
        records[-1].update(focused=True, ready=False)
        self.assertIsNone(target.choose_cli(records, 'ghostty'))

    def test_registry_rejects_dead_pid_and_other_socket(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d, 'busybar-cli', 'test'); folder.mkdir(parents=True)
            record = {'pid': os.getpid(), 'thread_id': THREAD, 'socket': str(folder / 'control.sock')}
            path = folder / 'session.json'
            path.write_text(json.dumps(record))
            self.assertEqual([record], target.cli_sessions(d))
            record['socket'] = '/tmp/unrelated.sock'; path.write_text(json.dumps(record))
            self.assertEqual([], target.cli_sessions(d))
            record.update(socket=str(folder / 'control.sock'), pid=0); path.write_text(json.dumps(record))
            self.assertEqual([], target.cli_sessions(d))

    def test_cli_adapter_works_before_any_rollout_or_global_defaults(self):
        descriptor = {'thread_id': THREAD, 'kind': 'cli', 'ready': True,
                      'model': 'gpt-test', 'effort': 'high', 'state': 'IDLE'}
        with mock.patch.object(codex_status, 'selected_target', return_value=descriptor), \
             mock.patch.object(codex_status, 'newest_rollout', return_value=None), \
             mock.patch.object(codex_status, 'config_defaults', return_value={}):
            report = codex_status.probe({'quotas': [{'name': '7d', 'left_pct': 62}]})
        self.assertEqual(THREAD, report['control_thread_id'])
        self.assertEqual('Test high', report['label'])
        self.assertEqual(62, report['quotas'][0]['left_pct'])

    def test_backend_overrides_preserve_values_and_stop_at_prompt_separator(self):
        self.assertEqual(['-c', 'model="test"', '--disable=feature'], cli.backend_args(
            ['resume', '--last', '-c', 'model="test"', '--disable=feature', '--', '--config=no']))


if __name__ == '__main__':
    unittest.main()
