"""Run BUSY Bar independently of CLI wrappers (macOS LaunchAgents).

python3 native_services.py install
"""
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time
import urllib.request

import report
from adapters import codex_notify

HERE = Path(__file__).resolve().parent
ROLES = {'daemon': HERE / 'daemon.py', 'adapter': HERE / 'adapters/codex_status.py'}


def run(role):
    if role == 'adapter':
        codex_notify.PIDFILE.write_text(str(os.getpid()))
    env = report.load_env()
    # launchd has a minimal PATH; CLI-only installations live outside it.
    env['PATH'] = os.pathsep.join([str(Path.home() / '.local/bin'),
                                  '/opt/homebrew/bin', env.get('PATH', os.defpath)])
    for key in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
        env.pop(key, None)
    os.execve(sys.executable, [sys.executable, str(ROLES[role])], env)


def install():
    if sys.platform != 'darwin':
        raise SystemExit('This service installer uses macOS launchd.')
    domain = f'gui/{os.getuid()}'
    agents = Path.home() / 'Library/LaunchAgents'
    agents.mkdir(parents=True, exist_ok=True)
    log_dir = Path.home() / '.claude'
    log_dir.mkdir(exist_ok=True)
    for role in ROLES:
        label = f'local.busy-codex.{role}'
        subprocess.run(['launchctl', 'bootout', f'{domain}/{label}'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if codex_notify.adapter_running():
        os.kill(int(codex_notify.PIDFILE.read_text()), signal.SIGTERM)
    try:
        report.OPENER.open(urllib.request.Request(report.BASE + '/shutdown', data=b'{}'), timeout=2).close()
    except OSError:
        pass
    deadline = time.monotonic() + 4
    while report.daemon_alive() and time.monotonic() < deadline:
        time.sleep(.1)
    for role in ROLES:
        label = f'local.busy-codex.{role}'
        log = log_dir / ('busybar-daemon.log' if role == 'daemon' else 'busybar-codex-adapter.log')
        path = agents / (label + '.plist')
        path.write_bytes(plistlib.dumps({'Label': label,
            'ProgramArguments': [sys.executable, str(Path(__file__).resolve()), role],
            'WorkingDirectory': str(HERE), 'RunAtLoad': True, 'KeepAlive': False,
            'ThrottleInterval': 10, 'StandardOutPath': str(log), 'StandardErrorPath': str(log)}))
        subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
    print('BUSY Bar daemon and adapter are managed by launchd independently of Codex CLI.')


if __name__ == '__main__':
    if sys.argv[1:] == ['install']:
        install()
    elif len(sys.argv) == 2 and sys.argv[1] in ROLES:
        run(sys.argv[1])
    else:
        raise SystemExit(__doc__)
