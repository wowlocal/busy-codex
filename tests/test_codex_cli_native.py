import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from codex_cli_native import NativeCLIIPC, normalize
from codex_effort import Controller
import codex_target

THREAD = '11111111-2222-3333-4444-555555555555'


class NativeClientTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='busy-native-', dir='/tmp')
        self.path = Path(self.temp.name) / 'control.sock'
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.path))
        self.server.listen()
        self.server.settimeout(.1)
        self.stop = threading.Event()
        self.writes = []
        self.results = {}
        self.drop_ack = False
        self.state = {'protocolVersion': 1, 'instanceId': 'instance', 'threadId': THREAD,
            'model': 'test-model', 'effort': 'high', 'supportedEfforts': ['low','high','xhigh'],
            'revision': 1, 'focused': True, 'ready': True}
        self.worker = threading.Thread(target=self.serve, daemon=True)
        self.worker.start()
        self.client = NativeCLIIPC(self.path, lambda _: None)
        self.client.connect(THREAD)

    def tearDown(self):
        self.client.close()
        self.stop.set()
        self.worker.join(2)
        self.server.close()
        self.temp.cleanup()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            with connection, connection.makefile('rb') as stream:
                for line in stream:
                    value = json.loads(line)
                    if value['method'] == 'status/read':
                        result = self.state
                    elif value['method'] == 'effort/set':
                        self.writes.append(value)
                        if value['expectedRevision'] != self.state['revision']:
                            result = {'requestId': value['requestId'], 'status': 'rejected',
                                      'outcome': {'error': 'selection or settings changed'}}
                        else:
                            self.state.update(effort=value['effort'], revision=self.state['revision'] + 1)
                            result = {'requestId': value['requestId'], 'status': 'applied', 'outcome': {
                                'threadId': THREAD, 'model': 'test-model', 'effort': value['effort']}}
                        self.results[value['requestId']] = result
                        if self.drop_ack:
                            break
                    else:
                        result = self.results[value['requestId']]
                    connection.sendall(json.dumps({'result': result}).encode() + b'\n')

    def test_lost_reply_queries_original_request_without_repeating_write(self):
        self.drop_ack = True
        result = self.client.request('thread-follower-update-thread-settings',
                                     {'threadSettings': {'effort': 'xhigh'}})
        self.assertEqual('xhigh', result['result']['effort'])
        self.assertEqual(1, len(self.writes))
        self.assertEqual(1, self.writes[0]['expectedRevision'])

    def test_stale_revision_is_rejected_without_changing_native_settings(self):
        self.state.update(revision=2, effort='low')
        with self.assertRaisesRegex(ValueError, 'selection or settings changed'):
            self.client.request('thread-follower-update-thread-settings',
                                {'threadSettings': {'effort': 'xhigh'}})
        self.assertEqual('low', self.state['effort'])

    def test_dial_controller_routes_to_native_tui(self):
        self.client.close()
        info = {'kind':'cli', 'native_control':True, 'thread_id':THREAD, 'socket':str(self.path)}
        controller = Controller(lambda: THREAD, lambda: None, target_info=lambda: info)
        stop = threading.Event()
        worker = threading.Thread(target=controller.run, args=(stop,))
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while not controller.status()['connected'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(controller.rotate(1))
            while controller.status()['feedback'] != 'XHIGH' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual('XHIGH', controller.status()['feedback'])
            self.assertEqual('xhigh', self.state['effort'])
            self.assertEqual(1, len(self.writes))
        finally:
            stop.set()
            worker.join(2)

    def test_discovery_uses_native_full_task_identity(self):
        home = Path(self.temp.name)
        directory = home / 'tui-control' / 'test'
        directory.mkdir(parents=True)
        state = {**self.state, 'pid': os.getpid(), 'socket': str(directory / 'control.sock'),
                 'terminal': 'ghostty'}
        (directory / 'session.json').write_text(json.dumps(state))
        self.assertEqual([normalize(state)], codex_target.cli_sessions(home))
        target = codex_target.Target(home, foreground=lambda: 'com.mitchellh.ghostty')
        self.assertEqual(THREAD, target.status()['thread_id'])
        state['focused'] = False
        (directory / 'session.json').write_text(json.dumps(state))
        self.assertIsNone(target.status(force=True)['thread_id'])


if __name__ == '__main__':
    unittest.main()
