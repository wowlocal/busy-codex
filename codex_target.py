"""Choose the visible Desktop task or focused CLI; never follow background output."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import plistlib
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
_FOREGROUND_API = None
_FOREGROUND_API_LOCK = threading.Lock()


class ProcessSerialNumber(ctypes.Structure):
    _fields_ = [('high', ctypes.c_uint32), ('low', ctypes.c_uint32)]


def frontmost_pid(processes=None):
    # NSWorkspace.frontmostApplication caches activation until AppKit's main
    # run loop runs. These headless workers have no AppKit loop: query the
    # WindowServer's front process on every poll instead.
    global _FOREGROUND_API
    if processes is None:
        with _FOREGROUND_API_LOCK:
            if _FOREGROUND_API is None:
                api = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/'
                                  'Frameworks/HIServices.framework/HIServices')
                # Framework Python otherwise registers a Dock application when
                # querying WindowServer. Transform only once: repeating returns
                # paramErr even though the process is already background-only.
                api.TransformProcessType.argtypes = [ctypes.POINTER(ProcessSerialNumber), ctypes.c_uint32]
                api.TransformProcessType.restype = ctypes.c_int32
                current = ProcessSerialNumber(0, 2)  # kCurrentProcess
                status = api.TransformProcessType(ctypes.byref(current), 2)  # background application
                if status != 0:
                    raise OSError(f'macOS background registration failed (status={status})')
                _FOREGROUND_API = api
            processes = _FOREGROUND_API
    processes.GetFrontProcess.argtypes = [ctypes.POINTER(ProcessSerialNumber)]
    processes.GetFrontProcess.restype = ctypes.c_int16  # OSErr
    processes.GetProcessPID.argtypes = [ctypes.POINTER(ProcessSerialNumber), ctypes.POINTER(ctypes.c_int32)]
    processes.GetProcessPID.restype = ctypes.c_int32  # OSStatus
    front, pid = ProcessSerialNumber(), ctypes.c_int32()
    status = processes.GetFrontProcess(ctypes.byref(front))
    if status == 0:
        status = processes.GetProcessPID(ctypes.byref(front), ctypes.byref(pid))
    if status != 0 or pid.value <= 0:
        raise OSError(f'macOS foreground process unavailable (status={status})')
    return pid.value


def bundle_for_pid(pid):
    # Read executable metadata without loading AppKit. Framework Python turns
    # these headless workers into Dock applications when AppKit initializes.
    proc = ctypes.CDLL('/usr/lib/libproc.dylib')
    proc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
    if proc.proc_pidpath(pid, buffer, len(buffer)) <= 0:
        return None
    executable = Path(os.fsdecode(buffer.value))
    for directory in executable.parents:
        if directory.suffix != '.app':
            continue
        try:
            with (directory / 'Contents/Info.plist').open('rb') as stream:
                return plistlib.load(stream).get('CFBundleIdentifier')
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
    return None


def frontmost_bundle():
    return bundle_for_pid(frontmost_pid()) if sys.platform == 'darwin' else None


def cli_sessions(home):
    records = []
    paths = list((Path(home) / 'tui-control').glob('*/session.json'))
    paths += list((Path(home) / 'busybar-cli').glob('*/session.json'))
    for path in paths:
        try:
            if path.stat().st_uid != os.getuid():
                continue
            value = json.loads(path.read_text())
            if path.parent.parent.name == 'tui-control':
                from codex_cli_native import normalize
                value = normalize(value)
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
                  and (terminal is None or record.get('terminal') == terminal)]
    return candidates[0] if len(candidates) == 1 and candidates[0].get('ready') else None


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
        self.bundle = None

    def snapshot(self):
        return {**(self.selected or {'source': None, 'thread_id': None, 'error': self.error}),
                'foreground_bundle': self.bundle}

    def status(self, force=False):
        with self.lock:
            now = time.monotonic()
            if not force and now < self.next_poll:
                return self.snapshot()
            self.next_poll = now + .2
            self.selected = None
            self.error = ''
            self.bundle = None
            try:
                bundle = self.bundle = self.foreground()
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
                                      or 'No uniquely focused CLI with native TUI control')
                else:
                    self.error = 'Codex Desktop or a CLI terminal is not in the foreground'
            except (OSError, ValueError, TypeError) as error:
                self.error = str(error)
            if self.selected:
                self.last_display = dict(self.selected)
            return self.snapshot()

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
