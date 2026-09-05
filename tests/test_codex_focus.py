import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_focus import DesktopFocus, FocusState
from adapters import codex_status

A = '01a070b8-9a38-7780-9495-92c2bed932f9'
B = '01a070a0-75f7-7950-8a27-ed5f3cd21f8b'


def event(task, active=True, window='1', focused=True, appearance='primary'):
    return ('2026-09-05T19:32:31.741Z info [electron-message-handler] '
            'thread_stream_view_activity_changed '
            f'active={str(active).lower()} conversationId={task} '
            f'rendererWindowId={window} rendererWindowAppearance={appearance} '
            f'rendererWindowFocused={str(focused).lower()} rendererWindowVisible=true '
            'resumeState=resumed streamRole=owner\n')


class FocusTest(unittest.TestCase):
    def test_background_chatter_never_selects_a_task(self):
        state = FocusState()
        state.feed(event(A))
        state.feed(event(B).replace('thread_stream_view_activity_changed', 'Reasoning summary item completed'))
        self.assertEqual(A, state.current())
        state.feed(event(A, False))
        state.feed(event(B))
        self.assertEqual(B, state.current())

    def test_home_or_settings_clear_target_and_pip_cannot_steal_it(self):
        state = FocusState()
        state.feed(event(A))
        state.feed(event(B, appearance='pip'))
        self.assertEqual(A, state.current())
        state.feed(event(A, False))
        self.assertIsNone(state.current())

    def test_old_unmount_does_not_clear_new_task(self):
        state = FocusState()
        state.feed(event(A))
        state.feed(event(B))
        state.feed(event(A, False))
        self.assertEqual(B, state.current())

    def test_multiple_visible_windows_are_not_guessed(self):
        state = FocusState()
        state.feed(event(A))
        state.feed(event(B, window='2'))
        self.assertIsNone(state.current())

    def test_partial_tail_rotation_and_app_exit(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / '2026/09/05'
            folder.mkdir(parents=True)
            path = folder / f'codex-desktop-{A}-123-t0-i1-120000-0.log'
            path.write_text(event(A))
            alive = [True]
            focus = DesktopFocus(d, alive=lambda pid: pid == 123 and alive[0])
            self.assertEqual(A, focus.current(force=True))
            with path.open('a') as stream:
                stream.write(event(A, False))
                stream.write(event(B)[:-1])
            self.assertIsNone(focus.current(force=True))
            with path.open('a') as stream:
                stream.write('\n')
            self.assertEqual(B, focus.current(force=True))
            rotation = folder / f'codex-desktop-{A}-123-t0-i1-120000-1.log'
            rotation.write_text(event(B, False) + event(A))
            focus.scan_after = 0
            self.assertEqual(A, focus.current(force=True))
            alive[0] = False
            self.assertIsNone(focus.current(force=True))

    def test_focused_adapter_does_not_follow_newer_background_or_guardian_rollout(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / '2026/09/05'
            folder.mkdir(parents=True)
            selected = folder / f'rollout-2026-09-05T12-00-00-{A}.jsonl'
            selected.write_text(json.dumps({'type':'session_meta','payload':{'id':A,'source':'vscode'}})+'\n')
            background = folder / f'rollout-2026-09-05T12-01-00-{B}.jsonl'
            background.write_text(json.dumps({'type':'session_meta','payload':{'id':B,'source':'vscode'}})+'\n')
            guardian = folder / 'rollout-guardian.jsonl'
            guardian.write_text(json.dumps({'type':'session_meta','payload':{'source':{'subagent':'guardian'}}})+'\n')
            with mock.patch.object(codex_status, 'SESSIONS', Path(d)), \
                 mock.patch.object(codex_status, '_FOCUSED_ROLLOUTS', {}), \
                 mock.patch.dict(codex_status.report.ENV, {'BUSYBAR_CODEX_THREAD_ID':''}), \
                 mock.patch.object(codex_status.codex_focus.FOCUS, 'current', return_value=A):
                self.assertEqual(selected, codex_status.newest_rollout())
            with mock.patch.object(codex_status.codex_focus.FOCUS, 'current', return_value=None), \
                 mock.patch.dict(codex_status.report.ENV, {'BUSYBAR_CODEX_THREAD_ID':''}):
                self.assertIsNone(codex_status.newest_rollout())


if __name__ == '__main__':
    unittest.main()
