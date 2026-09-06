import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from adapters import codex_notify as notify


@unittest.skipUnless(os.name == 'posix', 'POSIX process status')
class AdapterLivenessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pidfile = Path(self.temp.name, 'adapter.pid')
        self.pidfile.write_text('12345')
        self.path_patch = mock.patch.object(notify, 'PIDFILE', self.pidfile)
        self.path_patch.start()
        self.kill_patch = mock.patch.object(notify.os, 'kill')
        self.kill_patch.start()

    def tearDown(self):
        self.kill_patch.stop()
        self.path_patch.stop()
        self.temp.cleanup()

    def test_live_adapter_is_recognized(self):
        output = 'S /usr/bin/python3 ' + str(notify.HERE / 'codex_status.py')
        with mock.patch.object(notify.subprocess, 'check_output', return_value=output):
            self.assertTrue(notify.adapter_running())

    def test_zombie_does_not_block_recovery(self):
        with mock.patch.object(notify.subprocess, 'check_output', return_value='Z <defunct>'):
            self.assertFalse(notify.adapter_running())

    def test_pid_reused_by_another_program_is_not_an_adapter(self):
        with mock.patch.object(notify.subprocess, 'check_output', return_value='S /usr/bin/sleep 60'):
            self.assertFalse(notify.adapter_running())

    def test_nonpositive_pid_does_not_probe_a_process_group(self):
        self.pidfile.write_text('0')
        self.assertFalse(notify.adapter_running())
        notify.os.kill.assert_not_called()
