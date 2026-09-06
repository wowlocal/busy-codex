#!/usr/bin/env python3
"""Make ordinary interactive `codex` launches connect to the BUSY Bar.

python3 install_codex_cli.py install
python3 install_codex_cli.py uninstall
The original command is preserved beside the shim and restored on uninstall.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
MARKER = '# BUSY Bar Codex command shim v1'
NONINTERACTIVE = {'exec', 'e', 'review', 'login', 'logout', 'mcp', 'plugin',
                  'mcp-server', 'app-server', 'remote-control', 'app', 'completion',
                  'update', 'doctor', 'sandbox', 'debug', 'apply', 'a', 'queue',
                  'archive', 'delete', 'migrate-rollouts', 'unarchive', 'cloud',
                  'exec-server', 'features', 'help', 'agents'}
VALUE_FLAGS = {'-c', '--config', '--enable', '--disable', '-i', '--image',
               '-m', '--model', '--local-provider', '-p', '--profile',
               '-s', '--sandbox', '-C', '--cd', '--add-dir', '-a', '--ask-for-approval'}
BOOL_FLAGS = {'--oss', '--approve-for-me', '--yolo', '--search', '--no-alt-screen',
              '--strict-config', '--dangerously-bypass-approvals-and-sandbox',
              '--dangerously-bypass-hook-trust'}


def interactive(args):
    """Route TUI invocations; helpers and explicit remote clients pass through."""
    command = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--':
            break
        if arg in ('-h', '--help', '-V', '--version', '--remote', '--remote-auth-token-env'):
            return False
        if arg.startswith(('--remote=', '--remote-auth-token-env=')):
            return False
        if arg in VALUE_FLAGS:
            i += 2
            continue
        if '=' in arg and arg.split('=', 1)[0] in VALUE_FLAGS:
            i += 1
            continue
        if len(arg) > 2 and arg[:2] in VALUE_FLAGS and not arg.startswith('--'):
            i += 1
            continue
        if arg.startswith('-'):
            # Resume/fork options are consumed by that TUI subcommand.
            if arg not in BOOL_FLAGS and command not in ('resume', 'fork'):
                return False
        elif command is None:
            command = arg
            if command in NONINTERACTIVE:
                return False
        i += 1
    return True


def dispatch(original, args):
    env = dict(os.environ)
    if (sys.stdin.isatty() and sys.stdout.isatty() and interactive(args)
            and env.get('BUSYBAR_CODEX_LAUNCH', '1').lower() not in ('0', 'false', 'off')):
        env.setdefault('BUSYBAR_CODEX_CLI_BIN', str(original))
        os.execve(sys.executable, [sys.executable, str(HERE / 'codex_cli.py'), *args], env)
    os.execve(str(original), [str(original), *args], env)


def owned_shim(path):
    try:
        if path.is_symlink():
            return False
        with path.open('rb') as stream:
            return MARKER.encode() in stream.read(4096)
    except OSError:
        return False


def install(directory=None, original=None):
    directory = Path(directory or Path.home() / '.local/bin')
    directory.mkdir(parents=True, exist_ok=True)
    command = directory / 'codex'
    backup = directory / '.codex-busybar-original'
    manifest = directory / '.codex-busybar-install.json'
    if owned_shim(command):
        print('Ordinary codex launches already connect to BUSY Bar')
        return
    if backup.exists() or backup.is_symlink() or manifest.exists():
        raise ValueError('Existing BUSY Bar backup found; refusing to overwrite the original command')
    existing = command.exists() or command.is_symlink()
    original = Path(original or shutil.which('codex') or '')
    if not existing and not original.is_file():
        raise ValueError('Codex command not found')
    if existing and (not command.is_file() or not os.access(command, os.X_OK)):
        raise ValueError('Existing codex command is not executable')
    temporary = directory / '.codex-busybar-install.tmp'
    shim = (f'#!{sys.executable}\n{MARKER}\nimport sys\n'
            f'sys.path.insert(0, {str(HERE)!r})\n'
            'from install_codex_cli import dispatch\n'
            f'dispatch({str(backup)!r}, sys.argv[1:])\n')
    temporary.write_text(shim)
    temporary.chmod(0o755)
    try:
        if existing:
            command.rename(backup)
        else:
            backup.symlink_to(original.resolve())
        manifest.write_text(json.dumps({'existing': existing, 'repo': str(HERE)}))
        temporary.replace(command)
    except BaseException:
        if existing and not command.exists() and backup.exists():
            backup.rename(command)
        elif not existing and backup.is_symlink():
            backup.unlink()
        manifest.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Installed: {command}\nOriginal preserved: {backup}')


def uninstall(directory=None):
    directory = Path(directory or Path.home() / '.local/bin')
    command = directory / 'codex'
    backup = directory / '.codex-busybar-original'
    manifest = directory / '.codex-busybar-install.json'
    if not owned_shim(command):
        raise ValueError('codex is not the BUSY Bar shim; leaving it unchanged')
    data = json.loads(manifest.read_text())
    if not backup.exists():
        raise ValueError('Original codex command is missing; leaving the shim unchanged')
    if data['existing']:
        backup.replace(command)
    else:
        command.unlink()
        backup.unlink()
    manifest.unlink()
    print('Restored the original codex command')


if __name__ == '__main__':
    action = sys.argv[1:]
    try:
        if action == ['install']:
            install()
        elif action == ['uninstall']:
            uninstall()
        else:
            raise SystemExit(__doc__)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
