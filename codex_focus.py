"""Read Codex Desktop's task-view lifecycle log, without UI automation.

`thread_stream_view_activity_changed` comes from setActive() on the task view,
not from model activity. Only primary windows count; a task's background output
cannot select it. An inactive event clears the selection even on Settings/Home.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import threading
import time

UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
LOG_NAME = re.compile(r'(codex-desktop-(' + UUID + r')-(\d+)-t0)-.*\.log$')
EVENT = re.compile(r'^\S+ info \[electron-message-handler\] thread_stream_view_activity_changed ')
FIELDS = re.compile(r'\b(\w+)=([^\s]+)')


class FocusState:
    def __init__(self):
        self.windows = {}
        self.last_focused = None
        self.sequence = 0

    def feed(self, line):
        # Other logger messages can provide window focus/visibility evidence,
        # but never change that window's selected task.
        if ' [electron-message-handler] ' not in line:
            return
        fields = dict(FIELDS.findall(line))
        if fields.get('rendererWindowAppearance') != 'primary':
            return
        window_id = fields.get('rendererWindowId')
        if window_id is None:
            return
        window = self.windows.setdefault(window_id, {'thread_id': None, 'visible': False, 'seq': 0})
        if 'rendererWindowVisible' in fields:
            window['visible'] = fields['rendererWindowVisible'] == 'true'
        if fields.get('rendererWindowFocused') == 'true':
            self.last_focused = window_id
        if not EVENT.match(line):
            return
        thread_id = fields.get('conversationId', '')
        if not re.fullmatch(UUID, thread_id):
            return
        self.sequence += 1
        if fields.get('active') == 'true':
            window['thread_id'], window['seq'] = thread_id, self.sequence
        elif fields.get('active') == 'false' and window['thread_id'] == thread_id:
            window['thread_id'] = None

    def current(self):
        candidates = [w['thread_id'] for w in self.windows.values()
                      if w['visible'] and w['thread_id']]
        if len(candidates) > 1:
            # Window focus can change without a new log record. Never guess
            # between multiple task windows; the usual one-window tabs work.
            return None
        if self.last_focused in self.windows:
            window = self.windows[self.last_focused]
            return window['thread_id'] if window['visible'] else None
        # Startup before focus metadata: only an unambiguous visible task.
        return candidates[0] if len(candidates) == 1 else None


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class DesktopFocus:
    def __init__(self, log_root=None, alive=process_alive):
        self.log_root = Path(log_root or os.environ.get('BUSYBAR_CODEX_LOG_DIR',
                         Path.home() / 'Library/Logs/com.openai.codex'))
        self.alive = alive
        self.lock = threading.RLock()
        self.state = FocusState()
        self.run_id = None
        self.pid = None
        self.files = {}
        self.scan_after = 0
        self.poll_after = 0
        self.error = 'Waiting for a Codex Desktop task view'

    def _discover(self, now):
        self.scan_after = now + 2
        candidates = []
        for path in self.log_root.glob('*/*/*/codex-desktop-*-t0-*.log'):
            match = LOG_NAME.fullmatch(path.name)
            if match:
                try:
                    candidates.append((path.stat().st_mtime_ns, path, match.group(1), int(match.group(3))))
                except OSError:
                    pass
        candidates.sort(reverse=True)
        live = {}
        selected = next((item for item in candidates
                         if live.setdefault(item[3], self.alive(item[3]))), None)
        if selected is None:
            self.state, self.files, self.pid, self.run_id = FocusState(), {}, None, None
            self.error = 'No running Codex Desktop log'
            return
        _, _, run_id, pid = selected
        if run_id != self.run_id:
            self.state, self.files = FocusState(), {}
            self.run_id, self.pid = run_id, pid
        for _, path, run, _ in reversed(candidates):
            if run == run_id:
                self.files.setdefault(path, (0, None))
        self.scan_after = now + 2

    def current(self, force=False):
        with self.lock:
            now = time.monotonic()
            if not force and now < self.poll_after:
                return self.state.current()
            self.poll_after = now + .05
            try:
                if now >= self.scan_after:
                    self._discover(now)
                if self.pid is None or not self.alive(self.pid):
                    self.state = FocusState()
                    self.scan_after = 0
                    return None
                for path, (offset, identity) in list(self.files.items()):
                    try:
                        info = path.stat()
                        new_identity = (info.st_dev, info.st_ino)
                        if identity is not None and (identity != new_identity or info.st_size < offset):
                            # Lost lifecycle events are not reconstructable by inference.
                            self.state, self.files, self.run_id = FocusState(), {}, None
                            self.scan_after = 0
                            self.error = 'Codex log changed; waiting for a fresh task view'
                            return None
                        if info.st_size == offset:
                            continue
                        with path.open('rb') as stream:
                            stream.seek(offset)
                            while True:
                                start = stream.tell()
                                raw = stream.readline()
                                if not raw or not raw.endswith(b'\n'):
                                    offset = start
                                    break
                                self.state.feed(raw.decode('utf-8', errors='replace'))
                                offset = stream.tell()
                        self.files[path] = (offset, new_identity)
                    except FileNotFoundError:
                        self.files.pop(path, None)
                self.error = '' if self.state.current() else 'No local task selected in a visible Codex window'
                return self.state.current()
            except OSError as error:
                self.state = FocusState()
                self.error = str(error)
                return None

    def status(self):
        thread_id = self.current()
        return {'source': 'desktop-view-log', 'thread_id': thread_id, 'error': self.error}


FOCUS = DesktopFocus()
