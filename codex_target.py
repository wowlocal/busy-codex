"""Choose the visible Desktop task or focused CLI; never follow background output."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

import codex_focus

TERMINALS = {'com.mitchellh.ghostty': 'ghostty', 'com.apple.Terminal': 'Apple_Terminal',
             'com.googlecode.iterm2': 'iTerm.app', 'dev.warp.Warp-Stable': 'WarpTerminal',
             'org.wezfurlong.wezterm': 'WezTerm', 'net.kovidgoyal.kitty': 'kitty',
             'org.alacritty': 'Alacritty'}
DESKTOP = {'com.openai.codex', 'com.openai.chat', 'com.openai.ChatGPT'}


def frontmost_bundle():
    if sys.platform != 'darwin':
        return None
    # NSWorkspace exposes application identity without reading UI content or
    # requiring Accessibility/Screen Recording. Keep the Objective-C objects local.
    ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', objc))
    string = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', objc))
    def msg(value, selector):
        return send(value, objc.sel_registerName(selector.encode()))
    pool = msg(msg(objc.objc_getClass(b'NSAutoreleasePool'), 'alloc'), 'init')
    try:
        workspace = msg(objc.objc_getClass(b'NSWorkspace'), 'sharedWorkspace')
        app = msg(workspace, 'frontmostApplication')
        bundle = msg(app, 'bundleIdentifier')
        value = string(bundle, objc.sel_registerName(b'UTF8String')) if bundle else None
        return value.decode() if value else None
    finally:
        msg(pool, 'drain')


def cli_sessions(home):
    records = []
    for path in (Path(home) / 'busybar-cli').glob('*/session.json'):
        try:
            if path.stat().st_uid != os.getuid():
                continue
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or not isinstance(value.get('pid'), int):
                continue
            os.kill(value['pid'], 0)
            if value['pid'] <= 0 or not re.fullmatch(codex_focus.UUID, value.get('thread_id') or ''):
                continue
            # Control must stay in the same private launch directory.
            if value.get('socket') != str(path.parent / 'control.sock'):
                continue
            records.append(value)
        except (OSError, ValueError, TypeError):
            continue
    return records


def choose_cli(records, terminal=None):
    candidates = [record for record in records if record.get('focused')
                  and record.get('ready') and (terminal is None or record.get('terminal') == terminal)]
    return candidates[0] if len(candidates) == 1 else None


class Target:
    def __init__(self, home=None, foreground=frontmost_bundle, sessions=cli_sessions):
        self.home = Path(home or os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        self.foreground = foreground
        self.sessions = sessions
        self.lock = threading.RLock()
        self.next_poll = 0
        self.selected = None
        self.last_display = None
        self.error = ''

    def status(self, force=False):
        with self.lock:
            now = time.monotonic()
            if now < self.next_poll:
                return dict(self.selected or {'source': None, 'thread_id': None, 'error': self.error})
            self.next_poll = now + .2
            self.selected = None
            self.error = ''
            try:
                bundle = self.foreground()
                if bundle in DESKTOP:
                    thread_id = codex_focus.FOCUS.current(force=force)
                    if thread_id:
                        self.selected = {'source': 'desktop-view-log', 'thread_id': thread_id,
                                         'kind': 'desktop', 'error': ''}
                elif bundle in TERMINALS or (bundle is None and sys.platform != 'darwin'):
                    records = self.sessions(self.home)
                    record = choose_cli(records, TERMINALS.get(bundle))
                    if record:
                        self.selected = {**record, 'kind': 'cli', 'source': 'cli-focus', 'error': ''}
                    else:
                        matching = [r for r in records if r.get('focused') and
                                    (bundle is None or r.get('terminal') == TERMINALS[bundle])]
                        self.error = ((matching[0].get('error') if len(matching) == 1 else None)
                                      or 'No uniquely focused BUSY Bar CLI; launch it with busy-codex-cli')
                else:
                    self.error = 'Codex Desktop or a CLI terminal is not in the foreground'
            except (OSError, ValueError, TypeError) as error:
                self.error = str(error)
            if self.selected:
                self.last_display = dict(self.selected)
            return dict(self.selected or {'source': None, 'thread_id': None, 'error': self.error})

    def current(self, force=False):
        return self.status(force).get('thread_id')

    def display(self):
        selected = self.status()
        if selected.get('thread_id'):
            return selected
        # Keep usage visible while another app (or the lock screen) is active,
        # but never grant dial control through this display-only fallback.
        if self.last_display and self.last_display.get('kind') == 'cli':
            records = self.sessions(self.home)
            record = next((r for r in records if r['socket'] == self.last_display['socket']), None)
            if record and record.get('ready'):
                return {**record, 'kind': 'cli'}
        thread_id = codex_focus.FOCUS.current()
        return {'kind': 'desktop', 'thread_id': thread_id} if thread_id else {}


FOCUS = Target()
