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

    def test_multiple_cli_views_disable_control_until_explicit_resume(self):
        self.rpc('thread/read', {'threadId': 'other-view'})
        self.assertFalse(self.bridge.snapshot()['ready'])
        self.assertIn('multiple task views', self.bridge.snapshot()['error'])
        self.rpc('thread/start', {})
        self.assertTrue(self.bridge.snapshot()['ready'])


class FocusTest(unittest.TestCase):
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
