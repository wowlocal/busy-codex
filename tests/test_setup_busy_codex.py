"""Installer lifecycle against a fake Bar and real, isolated app processes."""
import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import daemon
import report
import setup_busy_codex as setup

FAKE_APP = '''import argparse,json,os,signal,threading,uuid
from http.server import BaseHTTPRequestHandler,HTTPServer
p=argparse.ArgumentParser();p.add_argument('--port',type=int);args,_=p.parse_known_args()
instance=str(uuid.uuid4())
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_GET(self):
  value=({'ok':True,'instance':instance,'pid':os.getpid(),'device_mode':'CUSTOM',
          'device_input':{'connected':True},'rendering':True} if self.path=='/hub' else {'label':'TEST'})
  data=json.dumps(value).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
server=HTTPServer(('127.0.0.1',args.port),Handler)
signal.signal(signal.SIGTERM,lambda *a:threading.Thread(target=server.shutdown).start())
server.serve_forever();server.server_close()
'''


class Device(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path.startswith('/api/screen'):
            data = bytes(72 * 16 * 3 if 'display=0' in self.path else 160 * 80 // 2)
            body = base64.b64encode(data)
        else:
            body = b'{}'
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.server.uploads.append(self.path)
        self.send_response(503 if self.server.reject else 200)
        self.end_headers()
        self.wfile.write(b'{}')


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='busy-install-test-')
        self.root = Path(self.temp.name).resolve()
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        self.install = self.root / 'busy-codex'
        self.state = self.root / 'state'
        self.device = ThreadingHTTPServer(('127.0.0.1', 0), Device)
        self.device.uploads, self.device.reject = [], False
        self.thread = threading.Thread(target=self.device.serve_forever, daemon=True)
        self.thread.start()
        self.children = []
        self.revision = 0
        self.actual_run = subprocess.run
        self.actual_spawn = setup.spawn
        with socket.socket() as bound:
            bound.bind(('127.0.0.1', 0))
            self.port = bound.getsockname()[1]
        self.patches = [mock.patch.object(setup, 'ROOT', self.checkout),
                        mock.patch.object(setup.subprocess, 'run', side_effect=self.fake_run),
                        mock.patch.object(setup, 'spawn', side_effect=self.spawn),
                        mock.patch.object(report, 'load_env', return_value=dict(os.environ))]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
        for patch in reversed(self.patches):
            patch.stop()
        self.device.shutdown()
        self.device.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def fake_run(self, argv, **kwargs):
        if len(argv) > 1 and str(argv[1]).endswith('scripts/build_gallery.py'):
            stage = Path(argv[-1])
            (stage / 'assets').mkdir(parents=True)
            self.revision += 1
            (stage / 'app.py').write_text(FAKE_APP)
            (stage / 'assets/test.anim').write_bytes(b'animation-' + str(self.revision).encode())
            files = {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in stage.rglob('*') if p.is_file()}
            (stage / 'SOURCE.json').write_text(json.dumps({'files': files, 'revision': str(self.revision)}))
            return subprocess.CompletedProcess(argv, 0)
        return self.actual_run(argv, **kwargs)

    def spawn(self, *args):
        child = self.actual_spawn(*args)
        self.children.append(child)
        return child

    def invoke(self, first=True):
        args = ['--install-dir', str(self.install), '--state-dir', str(self.state)]
        if first:
            args += ['--host', f'127.0.0.1:{self.device.server_port}', '--port', str(self.port)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = setup.main(args)
        self.last_output = output.getvalue()
        return result

    def test_install_then_update_preserves_options_and_replaces_only_owned_app(self):
        self.assertEqual(0, self.invoke(), self.last_output)
        first = self.children[0]
        (self.install / 'user-note.txt').write_text('keep me')
        self.assertEqual(0, self.invoke(first=False), self.last_output)
        self.assertIsNotNone(first.poll())
        self.assertIsNone(self.children[-1].poll())
        self.assertEqual(2, len(self.device.uploads))
        self.assertEqual('keep me', (self.install / 'user-note.txt').read_text())
        saved = json.loads((self.state / 'install.json').read_text())
        self.assertEqual(str(self.revision), saved['revision'])
        self.assertEqual(self.port, saved['port'])
        self.assertEqual(self.children[-1].pid, saved['launcher_pid'])
        self.assertEqual(1, len(setup.processes(self.install)))
        self.assertTrue((self.state / 'front.png').read_bytes().startswith(b'\x89PNG'))

    def test_failed_upload_never_starts_app_or_records_success(self):
        self.device.reject = True
        self.assertEqual(1, self.invoke())
        self.assertEqual([], self.children)
        self.assertFalse((self.state / 'install.json').exists())

    def test_unrelated_listener_is_not_stopped(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', self.port))
            listener.listen()
            self.assertEqual(1, self.invoke())
            self.assertEqual(0, self.revision)
            self.assertEqual([], self.children)

    def test_owned_directory_does_not_authorize_writing_through_symlinks(self):
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep'
        sentinel.write_text('untouched')
        self.install.symlink_to(outside, target_is_directory=True)
        self.assertEqual(1, self.invoke())
        self.assertEqual('untouched', sentinel.read_text())

    def test_process_matching_excludes_cli_and_similar_paths(self):
        root = Path('/tmp/a checkout')
        self.assertEqual('daemon', setup.process_kind(f'/usr/bin/python3 {root}/daemon.py --port 8765', root))
        for command in (f'/usr/bin/python3 {root}/codex_cli.py --yolo',
                        f'/usr/bin/python3 {root}/daemon.py.other',
                        f'/bin/sh -c echo {root}/daemon.py',
                        f'/usr/bin/python3 /other/script.py {root}/daemon.py'):
            self.assertIsNone(setup.process_kind(command, root))

    def test_pid_reuse_never_signals_another_process(self):
        record = dict(pid=123, kind='daemon', command='/usr/bin/python3 /old/daemon.py')
        with mock.patch.object(setup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                               stdout='S /usr/bin/python3 /different/app.py')), mock.patch.object(setup.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, 'PID changed'):
                setup.stop_verified([record])
            kill.assert_not_called()


class MaintenanceTests(unittest.TestCase):
    def test_failed_upload_releases_the_maintenance_lease(self):
        with mock.patch.object(setup, 'local_json') as call:
            with self.assertRaises(OSError):
                with setup.maintenance(8765, {}) as refresh:
                    refresh()
                    raise OSError('upload failed')
        self.assertEqual([60, 60, 0], [c.args[2]['seconds'] for c in call.call_args_list])

    def test_pause_refresh_resume_and_expiry_keep_input_and_canvas_in_sync(self):
        server = daemon._Server(('127.0.0.1', 0), daemon.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        transport = mock.Mock()
        transport.clear.return_value = True
        try:
            with mock.patch.object(daemon, 'TRANSPORT', transport), \
                 mock.patch.object(daemon, 'MAINTENANCE_UNTIL', 0), \
                 mock.patch.object(daemon, 'HUB_TOKEN', ''), \
                 mock.patch.object(daemon, 'DEVICE_MODE', 'CUSTOM'), \
                 mock.patch.object(daemon, 'astra_app_status', return_value={'active': False}):
                port = server.server_address[1]
                setup.local_json(port, '/maintenance', {'seconds': 60})
                self.assertFalse(daemon.device_canvas_allowed())
                self.assertIn('update', daemon.effort_input_block_reason())
                setup.local_json(port, '/maintenance', {'seconds': 60})
                transport.clear.assert_called_once_with(daemon.APP_NAME)
                setup.local_json(port, '/maintenance', {'seconds': 0})
                self.assertTrue(daemon.device_canvas_allowed())
                daemon.MAINTENANCE_UNTIL = 1
                self.assertTrue(daemon.device_canvas_allowed(), 'abandoned leases must expire')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


class UpdateShellTests(unittest.TestCase):
    def test_update_refuses_uncommitted_files_without_changing_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(Path(__file__).resolve().parent.parent / 'install.sh', root / 'install.sh')
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            result = subprocess.run(['sh', str(root / 'install.sh'), '--update'], capture_output=True, text=True)
            self.assertEqual(1, result.returncode)
            self.assertIn('Local changes found', result.stderr)
            self.assertTrue((root / 'install.sh').exists())

    def test_update_reexecutes_the_pulled_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream, checkout = root / 'upstream', root / 'checkout'
            def git(*args, cwd=None):
                return subprocess.run(['git', *args], cwd=cwd, check=True,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            git('init', '-q', str(upstream))
            git('config', 'user.name', 'Installer test', cwd=upstream)
            git('config', 'user.email', 'installer@example.invalid', cwd=upstream)
            shutil.copyfile(Path(__file__).resolve().parent.parent / 'install.sh', upstream / 'install.sh')
            (upstream / 'install.sh').chmod(0o755)
            git('add', '.', cwd=upstream)
            git('commit', '-qm', 'initial installer', cwd=upstream)
            git('clone', '-q', str(upstream), str(checkout))
            (upstream / 'install.sh').write_text('#!/bin/sh\necho updated-installer-ran\n')
            git('add', '.', cwd=upstream)
            git('commit', '-qm', 'updated installer', cwd=upstream)
            result = subprocess.run([str(checkout / 'install.sh'), '--update'], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('updated-installer-ran', result.stdout)


if __name__ == '__main__':
    unittest.main()
