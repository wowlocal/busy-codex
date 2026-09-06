#!/usr/bin/env python3
"""Cross-platform Codex `notify` hook (Python twin of codex_notify.sh).

Installed by install_codex_autostart.py as:
    notify = ["<python>", "<repo>/adapters/codex_notify.py"]
Codex appends the notification JSON as the final argument.

Chains the previously configured notifier (codex_notify_chain.json holds
its argv; legacy codex_notify_chain.sh is honored on POSIX), keeps the
daemon + codex adapter alive, and pushes the turn state immediately.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import report  # noqa: E402  (report.py: ensure_daemon + env loading)

PIDFILE = HERE / ".codex_adapter.pid"


def run_chain(args: list[str]):
    chain_json = HERE / "codex_notify_chain.json"
    chain_sh = HERE / "codex_notify_chain.sh"
    try:
        if chain_json.exists():
            argv = json.loads(chain_json.read_text())
            subprocess.run(argv + args, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif chain_sh.exists() and os.name != "nt":
            subprocess.run(["bash", str(chain_sh), *args], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass


def adapter_running() -> bool:
    try:
        pid = int(PIDFILE.read_text())
        if pid <= 0:
            return False
        os.kill(pid, 0)  # liveness probe; works on Windows too
        if os.name == 'posix':
            # kill(pid, 0) also succeeds for zombies and for a reused PID.
            # Launchers can outlive an adapter and have not reaped it yet.
            process = subprocess.check_output(
                ['ps', '-p', str(pid), '-o', 'stat=', '-o', 'command='],
                text=True, stderr=subprocess.DEVNULL, timeout=1).strip().split(None, 1)
            return (len(process) == 2 and 'Z' not in process[0]
                    and process[1].endswith(str(HERE / 'codex_status.py')))
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def ensure_adapter():
    if os.name == 'posix':
        import fcntl
        # Multiple idle CLI launchers and turn hooks may recover at once.
        with PIDFILE.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _ensure_adapter()
    else:
        _ensure_adapter()


def _ensure_adapter():
    if adapter_running():
        return
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    log = None
    try:
        path = report.LOG.with_name('busybar-codex-adapter.log')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1 << 20:
            path.write_text('')
        log = path.open('ab')
        kwargs['stderr'] = log
    except OSError:
        pass
    try:
        proc = subprocess.Popen([sys.executable, str(HERE / "codex_status.py")], **kwargs)
    finally:
        if log:
            log.close()
    try:
        PIDFILE.write_text(str(proc.pid))
    except OSError:
        pass


def main():
    run_chain(sys.argv[1:])
    report.ensure_daemon()
    ensure_adapter()
    # Push the turn's end state right now instead of waiting for the poll.
    subprocess.run([sys.executable, str(HERE / "codex_status.py"), "--once", "--no-usage-refresh"],
                   timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
