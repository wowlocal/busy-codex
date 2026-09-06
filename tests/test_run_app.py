"""Standalone launcher lifecycle: no private-data demo and owned child cleanup."""
import json
import contextlib
import io
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import run_app

ROOT = Path(__file__).resolve().parent.parent


class Device(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.reply({"app_name": "", "version": "test"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/api/display/draw":
            self.server.draws.append(json.loads(body))
        self.reply({})

    def do_DELETE(self):
        self.server.clears.append(self.path)
        self.reply({})

    def reply(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.device = ThreadingHTTPServer(("127.0.0.1", 0), Device)
        self.device.draws, self.device.clears = [], []
        self.thread = threading.Thread(target=self.device.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, CODEX_HOME=str(self.home),
                        BUSYBAR_CODEX_LOG_DIR=str(self.home / "no-desktop-logs"),
                        BUSYBAR_CODEX_BIN=str(self.home / "no-codex-executable"))
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            if child.stdout:
                child.stdout.close()
        self.device.shutdown()
        self.device.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def launch(self, *args):
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "run_app.py"), "--host",
             f"127.0.0.1:{self.device.server_port}", *args], cwd=ROOT, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.children.append(child)
        return child

    def test_demo_runs_without_codex_and_releases_its_canvas(self):
        child = self.launch("--demo")
        deadline = time.monotonic() + 4
        while not self.device.draws and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.device.draws, "demo never drew")
        child.send_signal(signal.SIGTERM)
        output, _ = child.communicate(timeout=4)
        self.assertEqual(child.returncode, 0, output)
        self.assertTrue(all(draw["application_name"] == "busy-codex" for draw in self.device.draws))
        self.assertEqual(self.device.clears[-1], "/api/display/draw?application_name=busy-codex")
        self.assertFalse(any(self.home.iterdir()), "demo touched the empty Codex home")

    def test_normal_run_waits_for_listener_and_stops_owned_workers(self):
        fake = self.home / "fake-codex"
        fake.write_text("#!" + sys.executable + "\n" + '''
import json, sys
for line in sys.stdin:
    value = json.loads(line)
    if "id" not in value: continue
    result = {}
    if value["method"] == "account/rateLimits/read":
        result = {"rateLimits": {"primary": {"usedPercent": 20,
            "windowDurationMins": 300, "resetsAt": 2000000000}}}
    print(json.dumps({"id": value["id"], "result": result}), flush=True)
''')
        fake.chmod(0o700)
        (self.home / "config.toml").write_text('model = "gpt-test"\nmodel_reasoning_effort = "high"\n')
        self.env["BUSYBAR_CODEX_BIN"] = str(fake)
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
        child = self.launch("--no-effort", "--no-upload", "--seconds", "1.5", "--port", str(port))
        output, _ = child.communicate(timeout=8)
        self.assertEqual(child.returncode, 0, output)
        self.assertTrue(self.device.draws, output)
        self.assertTrue(all(draw["application_name"] == "busy-codex" for draw in self.device.draws), output)
        self.assertIn("/api/display/draw?application_name=busy-codex", self.device.clears)
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0,
                                "daemon was left running after the launcher exited")

    def test_invalid_lifetime_and_host_are_rejected(self):
        for args in (("--seconds", "nan"), ("--seconds", "inf"),
                     ("--host", "example.com/api"), ("--host", "user:pass@example.com")):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run_app.arguments(args)


if __name__ == "__main__":
    unittest.main()
