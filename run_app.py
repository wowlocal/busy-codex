#!/usr/bin/env python3
"""Busy Codex: task status, account quotas and tactile reasoning effort.

    python3 app.py                         # USB BUSY Bar, local Codex
    python3 app.py --host 192.168.1.50      # Wi-Fi; BUSYBAR_TOKEN if required
    python3 app.py --host 127.0.0.1:8080 --demo  # emulator, no Codex or secrets

Download the complete app folder, not this entrypoint alone. Python 3.9+;
standard library only. macOS foreground targeting follows Codex Desktop or
the active CLI terminal. CLI effort control requires the native-control fork:
https://github.com/wowlocal/codex/releases/tag/v0.153.4-fork.1-native-control
Stock CLI remains usable for account limits, but cannot accept dial changes.

Reads local Codex session metadata and invokes the installed `codex app-server`
only to read account limits. It does not send prompts, edit Codex config,
install background services, update executables or restart exited processes.
SIGINT/SIGTERM stops the owned processes and clears only this app's canvas.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP = "busy-codex"
HERE = Path(__file__).resolve().parent
STOP = threading.Event()


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="10.0.4.20", metavar="IP[:PORT]")
    parser.add_argument("--port", type=int, default=18765,
                        help="local report listener, separate from installed busy-codex (18765)")
    parser.add_argument("--demo", action="store_true",
                        help="cycle real effort animations without opening Codex data")
    parser.add_argument("--test", action="store_true",
                        help="show a synthetic effort frame and exit; no Codex needed")
    parser.add_argument("--no-effort", action="store_true", help="read-only status and limits")
    parser.add_argument("--no-upload", action="store_true",
                        help="reuse this version's assets already uploaded to the Bar")
    parser.add_argument("--seconds", type=float, default=0,
                        help="stop automatically after this many seconds (0: until stopped)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.seconds < 0 or not math.isfinite(args.seconds):
        parser.error("--seconds must be a finite, nonnegative number")
    host = args.host.removeprefix("http://").rstrip("/")
    parsed = urllib.parse.urlsplit("http://" + host)
    try:
        valid = bool(parsed.hostname) and parsed.port != 0
    except ValueError:
        valid = False
    if not valid or parsed.path or parsed.query or parsed.fragment or parsed.username:
        parser.error("--host must be an IP or hostname with an optional port")
    args.host = host
    return args


class Bar:
    """Small preview transport; normal mode uses the daemon's USB-aware transport."""

    def __init__(self, host):
        from busybar_http import local_opener

        self.base = "http://" + host + "/api"
        source = os.environ.get("BUSYBAR_USB_SOURCE_IP", "10.0.4.21") if host == "10.0.4.20" else None
        self.opener = local_opener(source)
        self.headers = {}
        token = os.environ.get("BUSYBAR_TOKEN", "")
        if token:
            self.headers["X-API-Token"] = token

    def request(self, method, path, body=None, content_type="application/json"):
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=2) as response:
                response.read()
                return response.status
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError("BUSY Bar refused authentication; check BUSYBAR_TOKEN") from exc
            return exc.code

    def clear(self):
        try:
            self.request("DELETE", "/display/draw?application_name=" + APP)
        except OSError:
            pass


def demo(args):
    """Emit the same BGRA animation frames used to generate native .anim assets.

    PNG transport makes those frames reproducible with the gallery recorder,
    whose animation decoder does not support our native inter-frame encoding.
    The clock selects a current frame after every send; delayed frames are
    skipped rather than collected in a queue.
    """
    import effort_animation
    from pixel_ui import encode_png

    bar = Bar(args.host)
    levels = ("high", "xhigh", "max", "ultra")
    started = time.monotonic()
    deadline = started + (args.seconds or float("inf"))
    retry_delay = .25
    index = 0
    print(f"{APP} demo -> {bar.base}; synthetic data only", flush=True)
    try:
        while not STOP.is_set() and time.monotonic() < deadline:
            elapsed = time.monotonic() - started
            level = "ultra" if args.test else levels[int(elapsed / effort_animation.DURATION_S) % len(levels)]
            phase = elapsed % effort_animation.DURATION_S
            frame_index = 18 if args.test else int(phase * effort_animation.FPS)
            pixels = effort_animation.frame(level, frame_index, entering=False)
            name = f"demo-{index % 4}.png"
            index += 1
            try:
                status = bar.request("POST", f"/assets/upload?application_name={APP}&file={name}",
                                     encode_png(pixels, 72, 16), "application/octet-stream")
                if status == 200:
                    payload = {"application_name": APP, "priority": 30,
                               "elements": [{"id": "effort", "type": "image", "path": name,
                                             "x": 0, "y": 0, "timeout": 5}]}
                    status = bar.request("POST", "/display/draw", json.dumps(payload).encode())
                if status == 409:
                    STOP.wait(.5)
                    continue
                if status == 508:  # firmware still holds one of the rotating assets
                    STOP.wait(.1)
                    continue
                if status != 200:
                    raise RuntimeError(f"BUSY Bar returned HTTP {status}")
                retry_delay = .25
                if args.test:
                    STOP.wait(.25)
                    break
            except OSError as exc:
                if args.test:
                    raise RuntimeError(f"BUSY Bar unreachable: {type(exc).__name__}") from exc
                print(f"BUSY Bar unreachable: {type(exc).__name__}; retrying", file=sys.stderr)
                STOP.wait(retry_delay)
                retry_delay = min(5., retry_delay * 2)
                continue
            STOP.wait(1 / 12)
    finally:
        bar.clear()
    return 0


def runtime_env(args):
    env = dict(os.environ)
    # This focused launcher owns its report listener and renderer. Settings for
    # an independently installed multi-provider daemon must not leak into it.
    for key in ("BUSYBAR_HUB", "BUSYBAR_STANDBY", "BUSYBAR_HUB_TOKEN",
                "BUSYBAR_CODEX_THREAD_ID"):
        env.pop(key, None)
    env.update({"BUSYBAR_APP_NAME": APP, "BUSYBAR_DRAW_PRIORITY": "30", "BUSYBAR_MANAGED": "1",
                "BUSYBAR_PORT": str(args.port), "BUSYBAR_LISTEN": "127.0.0.1",
                "BUSYBAR_RENDER_MODE": "auto", "BUSYBAR_STYLE": "minimal",
                "BUSYBAR_AI_STATUS": "0", "BUSYBAR_X_PULSE": "0",
                "BUSYBAR_CODEX_EFFORT": "0" if args.no_effort else "1",
                "BUSYBAR_ASTRA_STATE": str(HERE / "disabled-astra-state.json"),
                "BUSYBAR_TRANSPORT": "usb" if args.host == "10.0.4.20" else "wifi",
                "BUSYBAR_DEVICE": args.host, "PYTHONUNBUFFERED": "1"})
    return env


def upload_assets(transport):
    assets = HERE / "assets"
    if not assets.is_dir():
        raise RuntimeError("Packaged assets are missing. Run scripts/build_gallery.py, then its app.py.")
    files = sorted(assets.glob("*.anim"))
    if not files:
        raise RuntimeError("Packaged animation assets are empty")
    print(f"Uploading {len(files)} animations into {APP}'s assets (no app installation)", flush=True)
    transport.clear(APP)
    for path in files:
        if STOP.is_set():
            return
        req = urllib.request.Request(
            transport.base + f"/assets/upload?application_name={APP}&file=" + urllib.parse.quote(path.name),
            data=path.read_bytes(), method="POST",
            headers={**transport.headers, "Content-Type": "application/octet-stream"})
        with transport.opener.open(req, timeout=8) as response:
            response.read()


def stop_children(children):
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 3.5
    for child in children:
        try:
            child.wait(timeout=max(.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1)


def wait_ready(child, port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 4
    while not STOP.is_set() and time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Display worker exited before becoming ready")
        try:
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=.2) as response:
                response.read()
                if response.status == 200:
                    return
        except OSError:
            pass
        STOP.wait(.05)
    if not STOP.is_set():
        raise RuntimeError("Display worker did not become ready")


def run(args):
    # Fail before drawing or starting anything when a prior instance owns the
    # local endpoint. It would be unsafe to feed an unrelated daemon's port.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError as exc:
            raise RuntimeError(f"Local port {args.port} is in use; choose --port or stop that instance") from exc
    env = runtime_env(args)
    os.environ.update(env)
    for key in ("BUSYBAR_HUB", "BUSYBAR_STANDBY", "BUSYBAR_HUB_TOKEN", "BUSYBAR_CODEX_THREAD_ID"):
        if key not in env:
            os.environ.pop(key, None)
    import daemon
    transport = daemon.make_transport()
    if not args.no_upload:
        upload_assets(transport)
    children = []
    deadline = time.monotonic() + (args.seconds or float("inf"))
    try:
        if STOP.is_set():
            return 0
        children.append(subprocess.Popen(
            [sys.executable, str(HERE / "daemon.py"), "--port", str(args.port)], cwd=HERE, env=env))
        wait_ready(children[0], args.port)
        if STOP.is_set():
            return 0
        children.append(subprocess.Popen(
            [sys.executable, str(HERE / "adapters" / "codex_status.py")], cwd=HERE, env=env))
        print(f"{APP} -> {args.host}; Ctrl-C stops both workers", flush=True)
        while not STOP.wait(.1) and time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A worker exited; stopping the app (no automatic restart)")
    finally:
        stop_children(children)
        transport.clear(APP)
    return 0


def main(argv=None):
    args = arguments(argv)
    STOP.clear()
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    try:
        return demo(args) if args.demo or args.test else run(args)
    except (OSError, RuntimeError) as exc:
        print(f"{APP}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
