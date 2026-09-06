import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import install_codex_cli as installer


class InstallTest(unittest.TestCase):
    def test_interactive_flags_and_resume_keep_the_bridge(self):
        for args in ([], ['--yolo'], ['--model', 'exec', '--yolo'], ['-mtest', '--yolo'],
                     ['resume', '--last', '--yolo'], ['fork', 'task-id'],
                     ['-c', 'model_reasoning_effort="high"', 'resume', 'task-id'],
                     ['--', 'help'], ['explain this code']):
            with self.subTest(args=args):
                self.assertTrue(installer.interactive(args))

    def test_helpers_help_and_remote_clients_pass_through(self):
        for args in (['exec', '--json', 'prompt'], ['e', 'prompt'], ['app-server', '--listen', 'stdio://'],
                     ['-c', 'a=1', 'app-server'], ['mcp', 'list'], ['--version'],
                     ['resume', '--help'], ['--remote', 'unix:///tmp/test.sock'],
                     ['--remote=unix:///tmp/test.sock'], ['--unknown-option'], ['update']):
            with self.subTest(args=args):
                self.assertFalse(installer.interactive(args))

    def test_install_preserves_relative_symlink_and_uninstall_restores_it(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            original = folder / 'original'
            original.write_text('#!/bin/sh\nexit 0\n'); original.chmod(0o755)
            command = folder / 'codex'; command.symlink_to('original')
            with mock.patch('builtins.print'):
                installer.install(folder)
                installer.install(folder)  # repeat is harmless
            self.assertTrue(installer.owned_shim(command))
            backup = folder / '.codex-busybar-original'
            self.assertEqual('original', os.readlink(backup))
            self.assertEqual(original.resolve(), backup.resolve())
            with mock.patch('builtins.print'):
                installer.uninstall(folder)
            self.assertEqual('original', os.readlink(command))
            self.assertFalse(backup.exists())

    def test_uninstall_does_not_overwrite_a_subsequent_codex_update(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            command = folder / 'codex'
            command.write_text('#!/bin/sh\nexit 0\n'); command.chmod(0o755)
            with mock.patch('builtins.print'):
                installer.install(folder)
            command.write_text('user updated codex')
            with self.assertRaisesRegex(ValueError, 'leaving it unchanged'):
                installer.uninstall(folder)
            self.assertEqual('user updated codex', command.read_text())

    def test_dispatch_routes_only_interactive_tui_without_recursion(self):
        with mock.patch.object(installer.sys.stdin, 'isatty', return_value=True), \
             mock.patch.object(installer.sys.stdout, 'isatty', return_value=True), \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(installer.os, 'execve', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                installer.dispatch('/original/codex', ['--yolo'])
            argv = execute.call_args.args[1]
            self.assertEqual(['--yolo'], argv[2:])
            self.assertEqual(str(installer.HERE / 'codex_cli.py'), argv[1])
            self.assertEqual('/original/codex', execute.call_args.args[2]['BUSYBAR_CODEX_CLI_BIN'])
            with self.assertRaises(SystemExit):
                installer.dispatch('/original/codex', ['app-server', '--listen', 'stdio://'])
            self.assertEqual('/original/codex', execute.call_args.args[0])

    def test_non_tty_and_opt_out_leave_native_codex_unchanged(self):
        for tty, env in ((False, {}), (True, {'BUSYBAR_CODEX_LAUNCH': '0'})):
            with self.subTest(tty=tty, env=env), \
                 mock.patch.object(installer.sys.stdin, 'isatty', return_value=tty), \
                 mock.patch.object(installer.sys.stdout, 'isatty', return_value=tty), \
                 mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(installer.os, 'execve', side_effect=SystemExit) as execute:
                with self.assertRaises(SystemExit):
                    installer.dispatch('/original/codex', ['--yolo'])
                self.assertEqual('/original/codex', execute.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
