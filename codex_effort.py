"""BUSY dial -> existing Codex Desktop task settings (no new turns).

Desktop IPC is private/versioned. Fail closed if its owner or snapshot protocol
changes. Never resume a task, edit config.toml, or send a prompt as a fallback.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import threading
import time
import uuid

LEVELS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
STATE_KEYS = {'latestThreadSettings', 'latestModel', 'latestReasoningEffort',
              'latestCollaborationMode'}
MAX_FRAME = 256 * 1024 * 1024
CATALOG_GRACE_S = 300


class CatalogError(ValueError):
    """A local catalog problem does not mean the Desktop connection failed."""


class ModelCatalog:
    def __init__(self, home, clock=time.monotonic):
        self.path = Path(home) / 'models_cache.json'
        self.clock = clock
        self.known = {}
        self.current = {}
        self.read_error = ''
        self.refresh()  # Warm before the first dial event, including every model.

    def refresh(self):
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or not isinstance(data.get('models'), list):
                raise ValueError('models must be an array')
            current = {}
            for entry in data['models']:
                if not isinstance(entry, dict) or not isinstance(entry.get('slug'), str):
                    continue
                supported = {r.get('effort') for r in entry.get('supported_reasoning_levels', [])
                             if isinstance(r, dict)}
                current[entry['slug']] = [level for level in LEVELS if level in supported]
            self.current = current
            now = self.clock()
            for model, levels in current.items():
                self.known[model] = (levels, now)
            self.read_error = ''
        except (OSError, ValueError, TypeError) as error:
            self.current = {}
            self.read_error = type(error).__name__

    def levels_for(self, model):
        self.refresh()
        if model in self.current:
            levels = self.current[model]
        else:
            levels, seen_at = self.known.get(model, (None, float('-inf')))
            if levels is None or self.clock() - seen_at > CATALOG_GRACE_S:
                reason = self.read_error or 'model absent'
                raise CatalogError(f'Codex catalog unavailable for model={model!r}: {reason}; '
                                   f'catalog_models={",".join(self.current)}')
        if not levels:
            raise CatalogError(f'No supported effort levels for model={model!r}')
        return list(levels)


def supported_efforts(model, home):
    return ModelCatalog(home).levels_for(model)


def model_effort(state):
    settings = state.get('latestThreadSettings') or {}
    collab = settings.get('collaborationMode') or state.get('latestCollaborationMode') or {}
    mode_settings = collab.get('settings') or {}
    return (settings.get('model') or state.get('latestModel'),
            mode_settings.get('reasoning_effort') or settings.get('effort')
            or state.get('latestReasoningEffort'))


def effort_settings(state, effort):
    result = {'effort': effort}
    collab = ((state.get('latestThreadSettings') or {}).get('collaborationMode')
              or state.get('latestCollaborationMode'))
    if collab:
        result['collaborationMode'] = copy.deepcopy(collab)
        result['collaborationMode']['settings']['reasoning_effort'] = effort
    return result


def apply_change(state, revision, change):
    if change['type'] == 'snapshot':
        return ({k: copy.deepcopy(v) for k, v in change['conversationState'].items()
                 if k in STATE_KEYS}, change['revision'])
    if change['type'] != 'patches' or change['baseRevision'] != revision:
        raise ValueError('Codex snapshot revision mismatch')
    state = copy.deepcopy(state)
    for patch in change['patches']:
        path = patch['path']
        if not isinstance(path, list):
            raise ValueError('Unsupported Codex patch format')
        if not path or path[0] not in STATE_KEYS:
            continue
        parent = state
        for key in path[:-1]:
            parent = parent[key]
        if patch['op'] == 'remove':
            parent.pop(path[-1], None)
        elif patch['op'] in ('add', 'replace'):
            parent[path[-1]] = copy.deepcopy(patch['value'])
        else:
            raise ValueError('Unsupported Codex patch operation')
    return state, change['revision']


class DesktopIPC:
    def __init__(self, path, on_change):
        self.path = Path(path)
        self.on_change = on_change
        self.sock = None
        self.client_id = None
        self.thread_id = None
        self.owner = None

    def connect(self, thread_id):
        info = self.path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Codex IPC socket is not owned by this user')
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(str(self.path))
        self.client_id = self.request('initialize', {'clientType': 'busybar'})['result']['clientId']
        self.thread_id = thread_id
        response = self.request('thread-owner-discovery',
                                {'hostId': 'local', 'conversationId': thread_id})
        self.owner = response['handledByClientId']
        self.send({'type': 'broadcast', 'method': 'thread-stream-following-changed',
                   'version': 1, 'sourceClientId': self.client_id,
                   'targetClientIds': [self.owner],
                   'params': {'hostId': 'local', 'conversationId': thread_id, 'following': True}})

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def send(self, message):
        data = json.dumps(message).encode()
        self.sock.sendall(struct.pack('<I', len(data)) + data)

    def receive(self):
        def exact(length):
            data = bytearray()
            while len(data) < length:
                part = self.sock.recv(length - len(data))
                if not part:
                    raise ConnectionError('Codex Desktop disconnected')
                data.extend(part)
            return data
        size = struct.unpack('<I', exact(4))[0]
        if not 0 < size <= MAX_FRAME:
            raise ValueError('Invalid Codex IPC frame length')
        message = json.loads(exact(size))
        if message['type'] == 'client-discovery-request':
            self.send({'type': 'client-discovery-response', 'requestId': message['requestId'],
                       'response': {'canHandle': False}})
        if message.get('method') == 'thread-stream-state-changed':
            params = message['params']
            if (params.get('conversationId') == self.thread_id
                    and params.get('hostId') == 'local'
                    and message.get('sourceClientId') == self.owner):
                if message.get('version') != 11:
                    raise ValueError('Unsupported Codex Desktop snapshot protocol')
                self.on_change(params['change'])
        if (message.get('method') == 'client-status-changed'
                and message.get('params', {}).get('clientId') == self.owner
                and message['params'].get('status') == 'disconnected'):
            raise ConnectionError('Codex task owner disconnected')
        return message

    def request(self, method, params, target=None):
        rid = str(uuid.uuid4())
        message = {'type': 'request', 'requestId': rid, 'version': 1,
                   'sourceClientId': self.client_id, 'method': method,
                   'params': params, 'timeoutMs': 2500}
        if target:
            message['targetClientId'] = target
        self.send(message)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.receive()
            if response.get('requestId') == rid and response['type'] == 'response':
                if response.get('resultType') != 'success':
                    raise RuntimeError(response.get('error', 'Codex rejected settings'))
                return response
        raise TimeoutError('Codex settings request timed out')


class Controller:
    def __init__(self, target, changed, home=None, logger=lambda _: None, allowed=lambda: True):
        self.target = target
        self.changed = changed
        self.allowed = allowed
        self.home = Path(home or os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        self.catalog = ModelCatalog(self.home)
        self.socket_path = os.environ.get('BUSYBAR_CODEX_IPC', str(self.home / 'ipc/ipc.sock'))
        self.logger = logger
        self.lock = threading.Lock()
        self.thread_id = None
        self.state = {}
        self.revision = None
        self.pending = 0
        self.due = 0
        self.error = ''
        self.feedback = None
        self.feedback_revision = 0
        self.feedback_until = 0
        self.direction = 1
        self.connected = False

    def status(self):
        with self.lock:
            model, effort = model_effort(self.state)
            return {'enabled': True, 'connected': self.connected, 'thread_id': self.thread_id,
                    'model': model, 'effort': effort, 'error': self.error,
                    'direction': self.direction,
                    'feedback_revision': self.feedback_revision,
                    'feedback': self.feedback if time.monotonic() < self.feedback_until else None}

    def rotate(self, delta):
        if not self.allowed():
            return False
        target = self.target()
        with self.lock:
            if not target or target != self.thread_id or not self.connected:
                return False
            self.pending = max(-32, min(32, self.pending + int(delta)))
            self.due = time.monotonic() + 0.12
        return True

    def on_change(self, change):
        with self.lock:
            self.state, self.revision = apply_change(self.state, self.revision, change)
            self.connected = True
        self.changed()

    def run(self, stop):
        ipc = None
        retry_at = 0
        try:
            while not stop.is_set():
                target = self.target()
                if target != self.thread_id:
                    if ipc:
                        ipc.close()
                        ipc = None
                    with self.lock:
                        self.thread_id = target
                        self.state, self.revision, self.pending = {}, None, 0
                        self.connected, self.feedback, self.error = False, None, ''
                    retry_at = 0
                    self.changed()
                if not target or time.monotonic() < retry_at:
                    stop.wait(0.1)
                    continue
                delta = 0
                try:
                    if ipc is None:
                        with self.lock:
                            self.state, self.revision, self.connected = {}, None, False
                        ipc = DesktopIPC(self.socket_path, self.on_change)
                        ipc.connect(target)
                        deadline = time.monotonic() + 3
                        while self.revision is None and time.monotonic() < deadline:
                            ipc.receive()
                        if self.revision is None:
                            raise TimeoutError('No Codex task snapshot')
                        with self.lock:
                            self.error = ''
                    if select.select([ipc.sock], [], [], 0.05)[0]:
                        ipc.receive()
                        continue
                    with self.lock:
                        delta = self.pending if time.monotonic() >= self.due else 0
                        if delta:
                            self.pending = 0
                        state = copy.deepcopy(self.state)
                    if not delta or self.target() != target or not self.allowed():
                        continue
                    model, current = model_effort(state)
                    levels = self.catalog.levels_for(model)
                    if current not in levels:
                        raise CatalogError(f'Current effort={current!r} absent from catalog '
                                           f'for model={model!r}; levels={levels}')
                    effort = levels[max(0, min(len(levels) - 1, levels.index(current) + delta))]
                    if effort != current:
                        ipc.request('thread-follower-update-thread-settings',
                                    {'conversationId': target, 'threadSettings': effort_settings(state, effort)},
                                    target=ipc.owner)
                        deadline = time.monotonic() + 2
                        while model_effort(self.state) != (model, effort) and time.monotonic() < deadline:
                            ipc.receive()
                        if model_effort(self.state) != (model, effort):
                            raise ValueError('Codex did not confirm effort change')
                    with self.lock:
                        self.feedback = effort.upper()
                        self.feedback_revision += 1
                        self.direction = 1 if delta > 0 else -1
                        self.feedback_until = time.monotonic() + 2.6
                        self.error = ''
                    self.logger(f'Codex effort: {target} -> {effort}')
                    self.changed()
                except CatalogError as error:
                    # Keep the valid subscription and any newly queued dial steps.
                    # A missing/partially rewritten shared cache is not an IPC error.
                    with self.lock:
                        self.error = str(error)
                        self.feedback = 'ERR'
                        self.feedback_revision += 1
                        self.feedback_until = time.monotonic() + 2.5
                    self.logger(f'Codex effort catalog: thread={target} {error}')
                    self.changed()
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                    if ipc:
                        ipc.close()
                        ipc = None
                    with self.lock:
                        self.error = str(error)
                        self.connected = False
                        self.pending = 0
                        self.feedback = 'ERR' if delta else None
                        self.feedback_revision += 1
                        self.feedback_until = time.monotonic() + 2.5
                    self.logger(f'Codex effort unavailable: {error}')
                    self.changed()
                    retry_at = time.monotonic() + 3
        finally:
            if ipc:
                ipc.close()
