"""Opt-in production CLI wrapper check; existing login, no model prompts.

Run: python3 tests/check_codex_cli_wrapper_live.py /path/to/native/codex-or-launcher
Optional: --model MODEL --all-levels, or --plan.
Only creates ephemeral test tasks. Exercises main(), run_tui(), live settings
RPC and the TUI's title output. Does not select an existing user's task.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import termios
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_cli as cli
from codex_cli_title import TitleOutput
from codex_effort import Controller


def child(options):
    class EphemeralBridge(cli.Bridge):
        def forward_client(self, message):
            if message.get('method') == 'thread/start':
                message = {**message, 'params': {**message.get('params', {}), 'ephemeral': True}}
            super().forward_client(message)

        def observe(self, message, pending=None):
            super().observe(message, pending)
            # Export only test assertions, without changing selection/settings.
            with self.condition:
                self.metadata['test_mode'] = (self.settings.get('collaborationMode') or {}).get('mode')
                self.metadata['supported_efforts'] = self.models.get(self.state['model'])
                self.publish()

    cli.Bridge = EphemeralBridge
    os.environ['BUSYBAR_CODEX_CLI_BIN'] = options.binary
    os.environ['BUSYBAR_CODEX_LEGACY_BRIDGE'] = '1'
    # The physical dial must not follow this background test terminal.
    os.environ['TERM_PROGRAM'] = 'busy-test'
    sys.argv = ['codex', '--yolo']
    if options.model:
        sys.argv += ['-m', options.model]
    if options.all_levels:
        sys.argv += ['-c', 'model_reasoning_effort="low"']
    return cli.main()


def check(options):
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 160, 0, 0))
    proc = subprocess.Popen([sys.executable, __file__, *sys.argv[1:], '--child'],
        stdin=slave, stdout=slave, stderr=slave,
        env=dict(os.environ, TERM='xterm-256color'), start_new_session=True)
    os.close(slave)
    record, last_title = [{}], ['']
    parser = TitleOutput(lambda title: last_title.__setitem__(0, title))
    controller = Controller(lambda: record[0].get('thread_id'), lambda: None,
                            target_info=lambda: {**record[0], 'kind': 'cli'})
    stop = threading.Event()
    worker = threading.Thread(target=controller.run, args=(stop,), daemon=True)
    worker.start()
    root = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'busybar-cli'
    phase, completed = 'start', False
    try:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if select.select([master], [], [], .02)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
                parser.feed(data)
            paths = list(root.glob(f'{proc.pid}-*/session.json'))
            if paths:
                record[0] = json.loads(paths[0].read_text())
            status = controller.status()
            assert not status['error'], status['error']
            state = record[0]
            if phase == 'start' and state.get('ready') and status['connected']:
                if options.plan:
                    os.write(master, b'/plan')
                    time.sleep(.3)  # keep Enter separate from native paste detection
                    os.write(master, b'\r')
                    phase = 'plan'
                else:
                    phase = 'rotate'
            elif phase == 'plan' and state.get('test_mode') == 'plan':
                print('PASS: native Plan mode confirmed', flush=True)
                phase = 'rotate'
            elif phase == 'rotate':
                original = state['effort']
                levels = state['supported_efforts']
                if options.all_levels:
                    targets = levels[levels.index(original) + 1:] + [original]
                else:
                    index = levels.index(original)
                    targets = [levels[index + (1 if index < len(levels) - 1 else -1)], original]
                assert targets and targets[0] != original, 'No alternate effort advertised'
                print(f'START: {state["model"]} {original}', flush=True)
                desired = targets.pop(0)
                assert controller.rotate(levels.index(desired) - levels.index(original))
                phase = 'confirm'
            elif phase == 'confirm' and state.get('effort') == desired and last_title[0].endswith(' | ' + desired):
                print(f'PASS: native TUI confirmed {desired}', flush=True)
                if not targets:
                    completed = True
                    break
                previous, desired = desired, targets.pop(0)
                assert controller.rotate(levels.index(desired) - levels.index(previous))
        assert completed, f'Wrapper check stopped at {phase}: {controller.status()}'
    finally:
        stop.set()
        worker.join(4)
        proc.terminate()
        deadline = time.monotonic() + 4
        while proc.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], .05)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        os.close(master)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary')
    parser.add_argument('--model')
    parser.add_argument('--all-levels', action='store_true')
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.child:
        raise SystemExit(child(options))
    check(options)
