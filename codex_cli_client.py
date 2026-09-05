"""Small settings-only client for a CLI launched through codex_cli.py."""
import json
import os
from pathlib import Path
import socket
import stat
import time

from codex_effort import CatalogError


class CLIIPC:
    def __init__(self, path, on_change):
        self.path = Path(path)
        self.on_change = on_change
        self.sock = None
        self.stream = None
        self.thread_id = None
        self.owner = 'cli'
        self.state = None
        self.revision = 0
        self.poll_after = 0

    def connect(self, thread_id):
        info = self.path.stat()
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            raise ValueError('CLI socket is not owned by this user')
        self.thread_id = thread_id
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(6)
        self.sock.connect(str(self.path))
        self.stream = self.sock.makefile('rb')
        self.receive()

    def close(self):
        if self.stream:
            self.stream.close()
        if self.sock:
            self.sock.close()
        self.stream = self.sock = None

    def rpc(self, value):
        self.sock.sendall(json.dumps(value).encode() + b'\n')
        line = self.stream.readline(16385)
        if not line or len(line) > 16384:
            raise ConnectionError('Invalid CLI control response')
        result = json.loads(line)
        if 'error' in result:
            raise ValueError(result['error'])
        state = result['result']
        if state.get('thread_id') != self.thread_id or not state.get('ready'):
            raise ValueError('CLI selected task changed')
        if self.state != state:
            self.state = state
            self.revision += 1
            self.on_change({'type': 'snapshot', 'revision': self.revision,
                'conversationState': {'latestThreadSettings': {
                    'model': state['model'], 'effort': state['effort']}}})
        return result

    def receive(self):
        self.poll_after = time.monotonic() + .2
        return self.rpc({'method': 'status'})

    def poll(self):
        if time.monotonic() >= self.poll_after:
            self.receive()

    def levels_for(self, model):
        levels = (self.state or {}).get('supported_efforts')
        if (self.state or {}).get('model') != model or not levels:
            raise CatalogError('CLI has not advertised effort levels for its current model')
        return levels

    def request(self, method, params, target=None):
        if method != 'thread-follower-update-thread-settings':
            raise ValueError('Unsupported CLI control operation')
        return self.rpc({'method': 'set_effort', 'effort': params['threadSettings']['effort'],
                         'expected_thread_id': self.thread_id, 'expected_model': self.state['model'],
                         'expected_effort': self.state['effort']})
