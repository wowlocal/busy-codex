#!/usr/bin/env python3
"""Install/update BUSY Codex from this checkout: ./install.sh [--update].

Build first, identify owned processes, upload assets with rendering stopped,
then start and inspect the installation. Existing source/CLI installations
retain their supervisor, port and asset namespace. No login items are added.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DEFAULT_INSTALL = Path.home() / '.local/share/busy-codex'
DEFAULT_STATE = Path.home() / '.local/state/busy-codex'
PYTHON = '/usr/bin/python3' if sys.platform == 'darwin' else sys.executable
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def process_kind(command, root):
    """Match the actual Python script, not a similar path or a user's CLI."""
    for script, kind in (('app.py', 'launcher'), ('run_app.py', 'launcher'),
                         ('daemon.py', 'daemon'), ('adapters/codex_status.py', 'adapter')):
        prefix, sep, suffix = command.partition(' ' + str(root / script))
        if (sep and (not suffix or suffix.startswith(' '))
                and Path(prefix).name.lower().startswith('python')
                and ' ' not in prefix):
            return kind
    return None


def processes(install):
    output = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,stat=,command='], text=True)
    found = []
    for line in output.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4 or 'Z' in parts[2]:
            continue
        for root, mode in ((ROOT, 'legacy'), (install, 'standalone')):
            kind = process_kind(parts[3], root)
            if kind:
                found.append(dict(pid=int(parts[0]), parent=int(parts[1]), kind=kind,
                                  mode=mode, command=parts[3]))
                break
    return found


def stop_verified(records):
    """Never signal a reused PID, the Codex CLI, or an unrelated listener."""
    ordered = sorted(records, key=lambda r: r['kind'] != 'launcher')
    for record in ordered:
        result = subprocess.run(['ps', '-p', str(record['pid']), '-o', 'stat=', '-o', 'command='],
                                capture_output=True, text=True)
        parts = result.stdout.strip().split(None, 1)
        if not parts or 'Z' in parts[0]:
            continue
        if len(parts) != 2 or parts[1] != record['command']:
            raise RuntimeError('A worker PID changed; refusing to stop it. Run the installer again.')
        os.kill(record['pid'], signal.SIGTERM)
        # Let a launcher release its canvas and reap children before signalling
        # orphan workers. Otherwise its cleanup can erase the replacement app.
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = subprocess.run(['ps', '-p', str(record['pid']), '-o', 'stat='],
                                    capture_output=True, text=True)
            if not result.stdout.strip() or 'Z' in result.stdout:
                break
            time.sleep(.1)
        else:
            raise RuntimeError(f"Worker {record['pid']} did not stop; no replacement was started.")


def local_json(port, path, body=None, headers=None):
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json', **(headers or {})})
    with LOCAL.open(request, timeout=2) as response:
        return json.load(response)


def assert_free(port):
    with socket.socket() as probe:
        if os.name != 'nt':
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            raise RuntimeError(f'Port {port} is occupied. Its owner was not stopped; inspect it or use --port.') from None


def wait_hub(port, previous=None, child=None):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if child is not None and child.poll() is not None:
            raise RuntimeError('The launcher exited. See the installation log.')
        try:
            hub = local_json(port, '/hub')
            if hub.get('ok') and hub.get('instance') != previous:
                return hub
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise RuntimeError(f'No replacement BUSY Codex endpoint on port {port}. See the installation log.')


def validate_install(path):
    if path.name != 'busy-codex' or path == ROOT or ROOT in path.parents:
        raise RuntimeError('Install destination must be an external directory named busy-codex.')
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise RuntimeError('Install destination is not a real directory.')
    if path.exists():
        if any(p.is_symlink() for p in path.rglob('*')):
            raise RuntimeError('Install destination contains symlinks; refusing to overwrite it.')
        if any(path.iterdir()) and not (path / 'SOURCE.json').is_file():
            raise RuntimeError('Install destination is not a previous build (SOURCE.json missing).')


def verify_build(path):
    path = path.resolve()
    manifest = json.loads((path / 'SOURCE.json').read_text())
    for relative, digest in manifest['files'].items():
        file = path / relative
        if path not in file.resolve().parents or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Build provenance check failed.')
    return manifest


def publish(stage, install):
    validate_install(install)
    # Keep user-added files; copy only from the freshly verified build. Workers
    # using this directory have already stopped. Legacy workers use the checkout.
    shutil.copytree(stage, install, dirs_exist_ok=True)


def spawn(argv, env, cwd, log):
    with log.open('ab', buffering=0) as output:
        return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=output, start_new_session=True)


@contextlib.contextmanager
def maintenance(port, headers):
    local_json(port, '/maintenance', {'seconds': 60}, headers)
    try:
        yield lambda: local_json(port, '/maintenance', {'seconds': 60}, headers)
    finally:
        local_json(port, '/maintenance', {'seconds': 0}, headers)


def device_opener(host, env):
    from busybar_http import local_opener
    source = env.get('BUSYBAR_USB_SOURCE_IP', '10.0.4.21') if host == '10.0.4.20' else None
    return local_opener(source)


def device_request(opener, host, env, path, data=None):
    headers = {'Content-Type': 'application/octet-stream'}
    if env.get('BUSYBAR_TOKEN'):
        headers['X-API-Token'] = env['BUSYBAR_TOKEN']
    req = urllib.request.Request(f'http://{host}/api{path}', data=data, headers=headers)
    with opener.open(req, timeout=15) as response:
        return response.read()


def upload(stage, host, namespace, env, refresh=lambda: None):
    opener = device_opener(host, env)
    assets = sorted((stage / 'assets').glob('*.anim'))
    for i, asset in enumerate(assets, 1):
        refresh()
        query = urllib.parse.urlencode({'application_name': namespace, 'file': asset.name})
        device_request(opener, host, env, '/assets/upload?' + query, asset.read_bytes())
        if i % 10 == 0 or i == len(assets):
            print(f'Uploaded {i}/{len(assets)} animations', flush=True)


def capture_screens(host, env, folder):
    from pixel_ui import encode_png
    opener = device_opener(host, env)
    for display, name, width, height in ((0, 'front', 72, 16), (1, 'back', 160, 80)):
        raw = base64.b64decode(device_request(opener, host, env, f'/screen?display={display}'), validate=True)
        if len(raw) != (width * height * 3 if display == 0 else width * height // 2):
            raise RuntimeError('Unexpected screen capture size')
        pixels = bytearray()
        for i in range(width * height):
            if display == 0:
                pixels.extend(raw[i * 3:i * 3 + 3] + b'\xff')
            else:
                packed = raw[i // 2]
                value = ((packed >> 4) if i % 2 == 0 else packed & 15) * 17
                pixels.extend((value, value, value, 255))
        (folder / f'{name}.png').write_bytes(encode_png(bytes(pixels), width, height))


def diagnostics(port, host, env, folder):
    hub = local_json(port, '/hub')
    status = local_json(port, '/status')
    for name, value in (('hub', hub), ('status', status)):
        (folder / f'{name}.json').write_text(json.dumps(value, indent=2) + '\n')
    try:
        capture_screens(host, env, folder)
        print(f'Display screenshots: {folder / "front.png"} and back.png')
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Screen capture unavailable ({type(error).__name__}); check the device connection.')
    print(f'Running. Status: http://127.0.0.1:{port}/hub')
    if hub.get('device_mode') is None:
        print('Display mode is not yet reported. Move the Bar switch to CUSTOM, then press Crown.')
    elif hub.get('device_mode') in ('APPS', 'SETTINGS', 'OFF', 'UNKNOWN'):
        print(f'Display mode is {hub["device_mode"]}. Exit the menu and switch to CUSTOM.')
    if not hub.get('device_input', {}).get('connected'):
        print('Device input is not connected yet; inspect hub.json before using the controls.')
    if hub.get('device_error'):
        print('The device reported an error; inspect hub.json.')
    if hub.get('codex_target', {}).get('error'):
        print('Controls need a sent task in foreground Codex Desktop or a supported focused CLI.')
    print('Process health alone does not verify visibility; inspect the screenshots and try Crown.')


def install(args):
    import report
    env = report.load_env()
    install_dir = args.install_dir.expanduser().absolute()
    install_dir = install_dir.parent.resolve() / install_dir.name
    state = args.state_dir.expanduser().absolute()
    state.mkdir(parents=True, exist_ok=True)
    if state.is_symlink():
        raise RuntimeError('State directory must not be a symlink.')
    os.chmod(state, 0o700)
    validate_install(install_dir)
    import fcntl
    with (state / 'install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another BUSY Codex installation is already running.') from None
        saved_path = state / 'install.json'
        saved = json.loads(saved_path.read_text()) if saved_path.exists() else {}
        owned = processes(install_dir)
        modes = {p['mode'] for p in owned}
        if len(modes) > 1:
            raise RuntimeError('Both legacy and standalone workers are running. Stop the duplicate installation first.')
        mode = next(iter(modes), saved.get('mode', 'standalone'))
        if mode == 'legacy':
            if any(p['kind'] == 'launcher' for p in owned):
                raise RuntimeError('A source launcher is running. Stop it before using the installer.')
            if env.get('BUSYBAR_HUB') or env.get('BUSYBAR_TRANSPORT', 'usb') not in ('usb', 'wifi'):
                raise RuntimeError('The simple installer supports a local USB/Wi-Fi installation only.')
            host = '10.0.4.20' if env.get('BUSYBAR_TRANSPORT', 'usb') == 'usb' else env.get('BUSYBAR_DEVICE', '')
            port = int(env.get('BUSYBAR_PORT', 8765))
            namespace = env.get('BUSYBAR_APP_NAME', 'claude_status')
            if ((args.host and args.host != host) or (args.port and args.port != port) or args.no_effort is not None):
                raise RuntimeError('Existing source installation detected. Set device/port/controls in env.sh, then rerun without overrides.')
        else:
            host = args.host or saved.get('host', '10.0.4.20')
            port = args.port or saved.get('port', 18765)
            namespace = 'busy-codex'
        from run_app import arguments
        launch_args = ['--host', host, '--port', str(port), '--no-upload']
        no_effort = args.no_effort if args.no_effort is not None else saved.get('no_effort', False)
        if no_effort:
            launch_args.append('--no-effort')
        validated = arguments(launch_args)
        host = validated.host
        # Refuse unknown port owners before building or stopping any workers.
        previous = None
        previous_port = saved.get('port', port) if mode == 'standalone' else port
        if any(p['kind'] in ('daemon', 'launcher') for p in owned):
            running = local_json(previous_port, '/hub')
            if running.get('pid') and running['pid'] not in {p['pid'] for p in owned if p['kind'] in ('daemon', 'launcher')}:
                raise RuntimeError('The report endpoint belongs to another process; refusing to stop it.')
            previous = running.get('instance')
            if not previous:
                raise RuntimeError('The running worker did not identify itself as BUSY Codex.')
        else:
            assert_free(port)
        if previous_port != port:
            assert_free(port)
        log = state / 'app.log'
        with tempfile.TemporaryDirectory(prefix='busy-codex-build-') as temporary:
            stage = Path(temporary) / 'busy-codex'
            subprocess.run([PYTHON, str(ROOT / 'scripts/build_gallery.py'), '--output', str(stage)], check=True)
            manifest = verify_build(stage)
            # Check connectivity/authentication before interrupting the old app.
            device_request(device_opener(host, env), host, env, '/version')
            print(f'Updating {mode} installation on port {port}', flush=True)
            stop_verified(owned)
            publish(stage, install_dir)
            if mode == 'legacy':
                # Old CLI watchdogs may already have recovered the source daemon.
                # Its bind-before-workers rule ensures only one wins the port.
                try:
                    assert_free(port)
                except RuntimeError:
                    pass
                else:
                    spawn([PYTHON, str(ROOT / 'daemon.py')], env, ROOT, log)
                wait_hub(port, previous)
                # Use the source adapter's existing cross-process startup lock.
                subprocess.run([PYTHON, '-c', 'from adapters.codex_notify import ensure_adapter; ensure_adapter()'],
                               cwd=ROOT, env=env, check=True)
                with maintenance(port, report.HEADERS) as refresh:
                    upload(stage, host, namespace, env, refresh)
                launcher_pid = None
            else:
                assert_free(port)
                upload(stage, host, namespace, env)
                child = spawn([PYTHON, str(install_dir / 'app.py'), *launch_args], env, install_dir, log)
                launcher_pid = child.pid
                wait_hub(port, child=child)
            saved_path.write_text(json.dumps({'mode': mode, 'host': host, 'port': port,
                'no_effort': no_effort, 'launcher_pid': launcher_pid,
                'install_dir': str(install_dir), 'revision': manifest['revision']}, indent=2) + '\n')
        print(f'Build installed in {install_dir}\nLog: {log}', flush=True)
        # Give the adapter a moment to publish its first selected task.
        time.sleep(1)
        diagnostics(port, host, env, state)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', type=Path, default=DEFAULT_INSTALL)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--host', help='USB/Wi-Fi address; remembered for standalone updates')
    parser.add_argument('--port', type=int, help='Local standalone report port')
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument('--no-effort', dest='no_effort', action='store_true', help='Standalone status-only mode')
    controls.add_argument('--effort', dest='no_effort', action='store_false', help='Re-enable standalone controls')
    parser.set_defaults(no_effort=None)
    args = parser.parse_args(argv)
    if os.name != 'posix':
        parser.error('This installer needs macOS/Linux; run the complete gallery app.py on other systems.')
    try:
        return install(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        # URL exceptions can include credentials from an environment URL; keep
        # transport details out of the console and never print the environment.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            message = f'HTTP {error.code}; check device/API access and rerun the installer.'
        print(f'Install/update failed: {message}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
