#!/usr/bin/env python3
"""Launch Codex CLI with a local BUSY Bar connection: python3 codex_cli.py [args].

The TUI and dial share one app-server. Only settings/focus metadata is retained;
conversation messages pass through unchanged and are never written by this bridge.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

from codex_effort import LEVELS, ModelCatalog
from codex_cli_title import TITLE_CONFIG, TitleOutput, thread_prefix

MAX_MESSAGE = 256 * 1024 * 1024


def settings_error(error):
    """Retain actionable RPC diagnostics without logging arbitrary server data."""
    error = error if isinstance(error, dict) else {}
    code = error.get('code')
    code = str(code) if type(code) is int else 'unknown'
    message = error.get('message')
    reason = 'unclassified'
    if isinstance(message, str):
        for prefix, category in (
            ('direct app-server input is not allowed for multi-agent v2 sub-agents', 'parent-owned task'),
            ('thread not found:', 'task is not loaded'),
            ('invalid thread id:', 'invalid task ID'),
            ('invalid thread settings override:', 'invalid settings'),
            ('failed to update thread settings:', 'settings submission failed'),
            ('thread listener is not running', 'task listener stopped'),
        ):
            if message.startswith(prefix):
                reason = category
                break
    return f'CLI rejected the settings change (RPC {code}; {reason})'


def read_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise ConnectionError('CLI disconnected')
        result.extend(part)
    return bytes(result)


def read_frame(sock):
    first, second = read_exact(sock, 2)
    size = second & 127
    if size in (126, 127):
        size = struct.unpack('!H' if size == 126 else '!Q', read_exact(sock, 2 if size == 126 else 8))[0]
    if size > MAX_MESSAGE:
        raise ValueError('CLI frame too large')
    mask = read_exact(sock, 4) if second & 128 else None
    payload = read_exact(sock, size)
    if mask:
        payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
    return first & 15, bool(first & 128), payload


def send_frame(sock, payload, opcode=1):
    size = len(payload)
    header = bytes((128 | opcode, size if size < 126 else 126 if size < 65536 else 127))
    if size >= 126:
        header += struct.pack('!H' if size < 65536 else '!Q', size)
    sock.sendall(header + payload)


class Bridge:
    def __init__(self, binary, directory, metadata, server_args=(), env=None):
        self.directory = Path(directory)
        self.socket_path = self.directory / 'cli.sock'
        self.control_path = self.directory / 'control.sock'
        self.record_path = self.directory / 'session.json'
        self.metadata = metadata
        self.state = {'thread_id': None, 'model': None, 'effort': None,
                      'state': 'IDLE', 'context_pct': None, 'ready': False}
        self.settings = {}
        self.threads = {}
        self.visible_prefix = None
        self.metadata['selection_source'] = 'terminal-title'
        self.models = {}
        self.catalog = ModelCatalog((env or os.environ).get('CODEX_HOME', Path.home() / '.codex'))
        self.condition = threading.Condition(threading.RLock())
        self.input_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.pending = {}
        self.sequence = 0
        self.client = None
        self.stop = threading.Event()
        self.reader = None
        self.proc = subprocess.Popen([binary, 'app-server', '--listen', 'stdio://', *server_args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, bufsize=0)
        self.listeners = []
        for path, handler in ((self.socket_path, self.accept_tui), (self.control_path, self.accept_control)):
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(path)); listener.listen(2); listener.settimeout(.2)
            self.listeners.append(listener)
            threading.Thread(target=self.accept, args=(listener, handler), daemon=True).start()
        self.reader = threading.Thread(target=self.read_backend, daemon=True)
        self.reader.start()
        self.publish()

    def snapshot(self):
        with self.condition:
            result = copy.deepcopy(self.state)
            result['supported_efforts'] = self.models.get(self.state['model'])
            return result

    def publish(self):
        # The directory is private; atomic replacement prevents partial focus reads.
        with self.condition:
            record = {**self.metadata, **self.state, 'socket': str(self.control_path)}
            temp = self.record_path.with_suffix('.tmp')
            temp.write_text(json.dumps(record))
            temp.replace(self.record_path)
            self.condition.notify_all()

    def focus(self, focused):
        with self.condition:
            self.metadata['focused'] = focused
            self.publish()

    def send_backend(self, message):
        data = json.dumps(message, separators=(',', ':')).encode() + b'\n'
        with self.input_lock:
            view = memoryview(data)
            while view:
                count = self.proc.stdin.write(view)
                if not count:
                    raise ConnectionError('CLI server closed its input')
                view = view[count:]

    def request_backend(self, method, params):
        response = queue.Queue(maxsize=1)
        with self.condition:
            self.sequence += 1
            rid = 'busy-control-' + str(self.sequence)
            self.pending[rid] = {'queue': response}
        try:
            self.send_backend({'id': rid, 'method': method, 'params': params})
            result = response.get(timeout=3)
        except queue.Empty:
            raise TimeoutError('CLI settings request timed out')
        finally:
            with self.condition:
                self.pending.pop(rid, None)
        if 'error' in result:
            raise RuntimeError(settings_error(result['error']))
        return result.get('result')

    def forward_client(self, message):
        if 'method' in message and 'id' in message:
            with self.condition:
                self.sequence += 1
                rid = 'busy-tui-' + str(self.sequence)
                params = message.get('params') or {}
                self.pending[rid] = {'id': message['id'], 'method': message['method'],
                                     'thread_id': params.get('threadId'),
                                     'collaborationMode': params.get('collaborationMode')}
                message = {**message, 'id': rid}
        self.send_backend(message)

    def observe_title(self, title):
        with self.condition:
            self.visible_prefix = thread_prefix(title)
            self.refresh_selection()

    def refresh_selection(self):
        # Reads/resumes may hydrate background views. Only the native title
        # identifies the displayed widget; no title text is kept in the registry.
        matches = [tid for tid in self.threads
                   if self.visible_prefix and tid.startswith(self.visible_prefix)]
        before = self.state
        if len(matches) == 1 and not self.stop.is_set():
            entry = self.threads[matches[0]]
            self.state = {**copy.deepcopy(entry['state']), 'ready': True, 'error': ''}
            self.settings = copy.deepcopy(entry['settings'])
        else:
            self.state = {**self.state, 'ready': False,
                          'error': 'CLI terminal title does not identify one live task'}
            self.settings = {}
        if self.state != before:
            self.publish()

    @staticmethod
    def apply_settings(entry, settings):
        entry['settings'].update(copy.deepcopy(settings))
        current = entry['settings']
        mode = (current.get('collaborationMode') or {}).get('settings') or {}
        entry['state'].update(model=current.get('model'),
                             effort=mode.get('reasoning_effort') or current.get('effort'))
        tier = current.get('serviceTier')
        entry['state']['badges'] = [tier] if tier and tier not in ('standard', 'default') else None

    def observe(self, message, pending=None):
        with self.condition:
            result = message.get('result') or {}
            method = (pending or {}).get('method')
            if method in ('thread/start', 'thread/resume', 'thread/fork') and 'thread' in result:
                thread = result['thread']
                state = dict(thread_id=thread['id'], model=result.get('model'),
                    effort=result.get('reasoningEffort'),
                    state='WORKING' if thread.get('status', {}).get('type') == 'active' else 'IDLE',
                    context_pct=None)
                settings = {'model': state['model'], 'effort': state['effort'],
                            'serviceTier': result.get('serviceTier')}
                mode = pending.get('collaborationMode')
                if mode:
                    settings['collaborationMode'] = copy.deepcopy(mode)
                self.threads[thread['id']] = {'state': state, 'settings': settings}
            elif method == 'model/list':
                for model in result.get('data', []):
                    advertised = {level['reasoningEffort'] for level
                                  in model.get('supportedReasoningEfforts', [])}
                    self.models[model['model']] = [level for level in LEVELS if level in advertised]
            params = message.get('params') or {}
            if message.get('method') == 'thread/closed':
                self.threads.pop(params.get('threadId'), None)
            entry = self.threads.get(params.get('threadId'))
            if entry:
                event = message.get('method')
                if event == 'thread/settings/updated':
                    self.apply_settings(entry, params['threadSettings'])
                elif event in ('turn/started', 'turn/completed'):
                    entry['state']['state'] = 'WORKING' if event == 'turn/started' else 'COMPLETE'
                elif event == 'thread/tokenUsage/updated':
                    usage = params.get('tokenUsage') or {}
                    total = (usage.get('last') or {}).get('totalTokens')
                    window = usage.get('modelContextWindow')
                    if isinstance(total, (int, float)) and isinstance(window, (int, float)) and window > 0:
                        entry['state']['context_pct'] = min(100., total * 100 / window)
            self.refresh_selection()

    def read_backend(self):
        try:
            for line in self.proc.stdout:
                message = json.loads(line)
                with self.condition:
                    pending = self.pending.pop(message.get('id'), None) if 'method' not in message else None
                if pending and 'queue' in pending:
                    pending['queue'].put(message)
                    continue
                self.observe(message, pending)
                if pending:
                    message['id'] = pending['id']
                elif isinstance(message.get('id'), str) and message['id'].startswith('busy-control-'):
                    continue  # a late control response never reaches the TUI
                with self.output_lock:
                    if self.client:
                        send_frame(self.client, json.dumps(message, separators=(',', ':')).encode())
        except (OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            self.stop.set()
            with self.condition:
                self.state['ready'] = False
                self.publish()
            client = self.client
            if client:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def accept(self, listener, handler):
        while not self.stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=handler, args=(client,), daemon=True).start()

    def accept_tui(self, client):
        try:
            client.settimeout(5)
            headers = bytearray()
            while not headers.endswith(b'\r\n\r\n') and len(headers) < 16384:
                headers.extend(read_exact(client, 1))
            lines = headers.decode('ascii').split('\r\n')
            fields = dict(line.lower().split(':', 1) for line in lines[1:] if ':' in line)
            # Preserve the case-sensitive WebSocket key.
            key = next(line.split(':', 1)[1].strip() for line in lines if line.lower().startswith('sec-websocket-key:'))
            if not lines[0].startswith('GET ') or 'origin' in fields or fields.get('upgrade', '').strip() != 'websocket':
                raise ValueError('Invalid local CLI handshake')
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            client.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
            with self.output_lock:
                if self.client:
                    raise ValueError('Only one TUI may use this CLI bridge')
                self.client = client
            client.settimeout(None)
            fragments = bytearray()
            while not self.stop.is_set():
                opcode, final, payload = read_frame(client)
                if opcode == 8:
                    break
                if opcode == 9:
                    with self.output_lock:
                        send_frame(client, payload, 10)
                    continue
                if opcode == 10:
                    continue
                if opcode == 1:
                    fragments = bytearray(payload)
                elif opcode == 0:
                    fragments.extend(payload)
                else:
                    raise ValueError('Expected a text CLI frame')
                if len(fragments) > MAX_MESSAGE:
                    raise ValueError('CLI message too large')
                if final:
                    self.forward_client(json.loads(fragments))
        except (OSError, ValueError, StopIteration):
            pass
        finally:
            with self.output_lock:
                if self.client is client:
                    self.client = None
                    self.stop.set()
            client.close()

    def set_effort(self, request):
        with self.condition:
            for key in ('thread_id', 'model', 'effort'):
                if request.get('expected_' + key) != self.state[key]:
                    raise ValueError('CLI selection/settings changed; turn the dial again')
            if not self.state['ready']:
                raise ValueError('CLI session is not ready')
            effort = request['effort']
            levels = self.models.get(self.state['model'])
            if levels is None:
                levels = self.catalog.levels_for(self.state['model'])
            if effort not in levels:
                raise ValueError('Effort is not supported by the CLI model')
            thread_id = self.state['thread_id']
            params = {'threadId': thread_id, 'effort': effort}
            if self.settings.get('collaborationMode'):
                params['collaborationMode'] = copy.deepcopy(self.settings['collaborationMode'])
                params['collaborationMode']['settings']['reasoning_effort'] = effort
        self.request_backend('thread/settings/update', params)
        with self.condition:
            confirmed = self.condition.wait_for(lambda: self.state['thread_id'] != thread_id
                or self.state['effort'] == effort or self.stop.is_set(), timeout=2)
            if not confirmed or self.state['thread_id'] != thread_id or self.state['effort'] != effort:
                raise ValueError('CLI did not confirm effort change')
            return self.snapshot()

    def accept_control(self, client):
        try:
            with client, client.makefile('rb') as stream:
                while not self.stop.is_set():
                    line = stream.readline(16385)
                    if not line or len(line) > 16384:
                        break
                    try:
                        request = json.loads(line)
                        if request.get('method') == 'status':
                            result = self.snapshot()
                        elif request.get('method') == 'set_effort':
                            result = self.set_effort(request)
                        else:
                            raise ValueError('Unsupported CLI control request')
                        response = {'result': result}
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                        response = {'error': str(error)[:180]}
                    client.sendall(json.dumps(response).encode() + b'\n')
        except OSError:
            pass

    def close(self):
        self.stop.set()
        for listener in self.listeners:
            listener.close()
        client = self.client
        if client:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait()
        if self.reader:
            self.reader.join(timeout=2)
        self.proc.stdout.close()


class FocusInput:
    """Observe terminal focus reports, excluding pasted text; forward every byte."""
    def __init__(self, changed):
        self.changed = changed
        self.pending = b''
        self.pasting = False

    def feed(self, data):
        self.pending += data
        tokens = {b'\x1b[I': True, b'\x1b[O': False,
                  b'\x1b[200~': 'paste', b'\x1b[201~': 'end'}
        while self.pending:
            matched = next((token for token in tokens if self.pending.startswith(token)), None)
            if matched:
                value = tokens[matched]
                if value == 'paste':
                    self.pasting = True
                elif value == 'end':
                    self.pasting = False
                elif not self.pasting:
                    self.changed(value)
                self.pending = self.pending[len(matched):]
            elif any(token.startswith(self.pending) for token in tokens):
                break
            else:
                following = self.pending.find(b'\x1b', 1)
                self.pending = self.pending[following:] if following >= 0 else b''


def run_tui(argv, bridge):
    import fcntl
    import pty
    import termios
    import tty
    stdin = sys.stdin.fileno()
    original = termios.tcgetattr(stdin)
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvpe(argv[0], argv, os.environ)
        except OSError:
            os._exit(127)
    def resize(*_):
        size = fcntl.ioctl(stdin, termios.TIOCGWINSZ, b'\0' * 8)
        fcntl.ioctl(master, termios.TIOCSWINSZ, size)
    previous_resize = signal.signal(signal.SIGWINCH, resize)
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    previous_signals = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGHUP)}
    focus = FocusInput(bridge.focus)
    title = TitleOutput(bridge.observe_title)
    reaped = False
    try:
        resize()
        tty.setraw(stdin)
        while True:
            readable, _, _ = select.select([stdin, master], [], [], .2)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                title.feed(data)
                sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
            if stdin in readable:
                data = os.read(stdin, 65536)
                if not data:
                    break
                focus.feed(data)
                view = memoryview(data)
                while view:
                    view = view[os.write(master, view):]
        _, status = os.waitpid(pid, 0)
        reaped = True
        return os.waitstatus_to_exitcode(status)
    finally:
        bridge.focus(False)
        termios.tcsetattr(stdin, termios.TCSADRAIN, original)
        signal.signal(signal.SIGWINCH, previous_resize)
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
        os.close(master)
        if not reaped:
            try:
                os.killpg(pid, signal.SIGHUP)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        break
                    time.sleep(.05)
                else:
                    os.killpg(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass


def backend_args(args):
    """The server shares CLI config overrides; prompt text is never parsed."""
    result = []
    iterator = iter(args)
    for arg in iterator:
        if arg == '--':
            break
        if arg in ('-c', '--config', '--enable', '--disable'):
            value = next(iterator, None)
            if value is None:
                raise ValueError(arg + ' requires a value')
            result.extend((arg, value))
        elif any(arg.startswith(flag + '=') for flag in ('--config', '--enable', '--disable')):
            result.append(arg)
        elif arg == '--strict-config':
            result.append(arg)
    return result


def keep_services_alive(stop, interval=10):
    """Recover display workers during an idle CLI, without waiting for a turn."""
    import report
    from adapters.codex_notify import ensure_adapter
    while not stop.is_set():
        try:
            report.ensure_daemon()
            ensure_adapter()
        except OSError:
            pass  # a display failure must not terminate the user's TUI
        stop.wait(interval)


def main():
    binary = os.environ.get('BUSYBAR_CODEX_CLI_BIN') or shutil.which('codex')
    if not binary:
        raise SystemExit('Codex CLI not found; set BUSYBAR_CODEX_CLI_BIN.')
    args = sys.argv[1:]
    if args in (['-h'], ['--help']):
        print('BUSY Bar CLI: busy-codex-cli [Codex options] [resume [SESSION_ID | --last]]\n'
              'The dial follows this terminal when it is focused.\n'
              'Requires Codex CLI with --remote unix:// and thread/settings/update.\n', flush=True)
        return subprocess.call([binary, '--help'])
    if args in (['-V'], ['--version']):
        return subprocess.call([binary, *args])
    if os.name != 'posix' or not sys.stdin.isatty():
        raise SystemExit('Run this launcher in an interactive macOS/Linux terminal.')
    if any(arg == '--remote' or arg.startswith('--remote=') for arg in args):
        raise SystemExit('The BUSY Bar launcher manages its local --remote connection.')
    server_args = backend_args(args)
    root = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'busybar-cli'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / (str(os.getpid()) + '-' + uuid.uuid4().hex[:6])
    directory.mkdir(mode=0o700)
    metadata = {'pid': os.getpid(), 'tty': os.ttyname(sys.stdin.fileno()),
                'terminal': os.environ.get('TERM_PROGRAM', ''), 'focused': True}
    bridge = None
    try:
        # A fresh interactive CLI is independent of the shell/tool that launched
        # it. Explicit resume/fork arguments still go through unchanged.
        for key in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
            os.environ.pop(key, None)
        bridge = Bridge(binary, directory, metadata, server_args, dict(os.environ))
        threading.Thread(target=keep_services_alive, args=(bridge.stop,), daemon=True).start()
        return run_tui([binary, '--remote', 'unix://' + str(bridge.socket_path),
                        '-c', TITLE_CONFIG, *args], bridge)
    finally:
        if bridge:
            bridge.close()
        shutil.rmtree(directory)


if __name__ == '__main__':
    sys.exit(main())
