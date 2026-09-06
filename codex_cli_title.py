"""Observe native TUI window-title reports without retaining terminal text.

Codex's configurable `thread-id` title item follows the displayed ChatWidget,
including cached agent switches that make no app-server request. Version 0.153.4
shortens this UUID to 29 characters plus `...`; the bridge must resolve that
prefix uniquely against threads loaded by this TUI's own server.
"""
import re

TITLE_CONFIG = 'tui.terminal_title=["thread-id","activity","project-name","model","reasoning"]'
THREAD = re.compile(r'(?<![0-9a-f-])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-'
                    r'(?:[0-9a-f]{12}|[0-9a-f]{5}(?=\.\.\.)))(?![0-9a-f-])')


def thread_prefix(title):
    matches = THREAD.findall(title)
    return matches[0] if len(matches) == 1 else None


class TitleOutput:
    """Bounded OSC parser; ordinary output is discarded and forwarded by caller."""
    def __init__(self, changed):
        self.changed = changed
        self.mode = 'text'
        self.payload = bytearray()

    def feed(self, data):
        for byte in data:
            if self.mode == 'text':
                if byte == 27:
                    self.mode = 'escape'
            elif self.mode == 'escape':
                self.mode = 'osc' if byte == 93 else 'escape' if byte == 27 else 'text'
                self.payload.clear()
            elif self.mode in ('osc', 'osc_escape'):
                if byte == 7 or (self.mode == 'osc_escape' and byte == 92):
                    if self.payload.startswith((b'0;', b'2;')):
                        self.changed(self.payload[2:].decode('utf-8', errors='replace'))
                    self.payload.clear()
                    self.mode = 'text'
                elif byte == 27:
                    self.mode = 'osc_escape'
                elif self.mode == 'osc_escape' or len(self.payload) >= 2048:
                    # Malformed/oversized control strings cannot select a task.
                    if self.payload.startswith((b'0;', b'2;')):
                        self.changed('')
                    self.payload.clear()
                    self.mode = 'discard'
                else:
                    self.payload.append(byte)
            elif self.mode == 'discard':
                if byte == 7:
                    self.mode = 'text'
                elif byte == 27:
                    self.mode = 'discard_escape'
            elif self.mode == 'discard_escape':
                self.mode = 'text' if byte in (7, 92) else 'discard'
