"""Opt-in check with a real Codex TUI, existing login, and no model prompts.

Run: python3 tests/check_codex_cli_live.py /path/to/native/codex-or-launcher
Creates temporary ephemeral threads, changes and restores their effort, then
checks /new selection. Does not attach to or change an existing user task.
"""
import fcntl
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_cli import Bridge
from codex_cli_title import TITLE_CONFIG, TitleOutput
from codex_effort import Controller


class EphemeralBridge(Bridge):
    history_reads = 0

    def forward_client(self, message):
        if message.get('method') == 'thread/start':
            message = {**message, 'params': {**message.get('params', {}), 'ephemeral': True}}
        if message.get('method') == 'thread/read':
            self.history_reads += 1
        super().forward_client(message)


def check(binary):
    env = dict(os.environ, TERM='xterm-256color')
    for key in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix='busy-native-', dir='/tmp') as directory:
        os.chmod(directory, 0o700)
        bridge = EphemeralBridge(binary, directory,
            {'pid': os.getpid(), 'terminal': 'test', 'focused': True}, env=env)
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 160, 0, 0))
        proc = subprocess.Popen([binary, '--remote', 'unix://' + str(bridge.socket_path),
                                 '--yolo', '-c', TITLE_CONFIG],
            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        last_title = ['']

        def title_changed(title):
            last_title[0] = title
            bridge.observe_title(title)

        parser = TitleOutput(title_changed)
        controller = Controller(lambda: bridge.snapshot()['thread_id'], lambda: None,
            target_info=lambda: {**bridge.snapshot(), 'kind': 'cli', 'socket': str(bridge.control_path)})
        stop = threading.Event()
        worker = threading.Thread(target=controller.run, args=(stop,), daemon=True)
        worker.start()
        phase = 0
        deadline = time.monotonic() + 30
        try:
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
                state = bridge.snapshot()
                if phase == 0 and state['ready'] and controller.status()['connected']:
                    first, original = state['thread_id'], state['effort']
                    levels = state['supported_efforts']
                    index = levels.index(original)
                    delta = 1 if index < len(levels) - 1 else -1
                    desired = levels[index + delta]
                    assert controller.rotate(delta)
                    phase = 1
                elif phase == 1 and state['effort'] == desired and last_title[0].endswith(' | ' + desired):
                    print(f'PASS: native TUI effort {original} -> {desired}', flush=True)
                    assert controller.rotate(-delta)
                    phase = 2
                elif phase == 2 and state['effort'] == original and last_title[0].endswith(' | ' + original):
                    print('PASS: native TUI effort restored', flush=True)
                    os.write(master, b'/new')
                    # Separate Enter from a paste burst, matching native TUI input handling.
                    time.sleep(.3)
                    os.write(master, b'\r')
                    phase = 3
                elif phase == 3 and state['ready'] and state['thread_id'] != first:
                    print('PASS: native /new selects the new task', flush=True)
                    phase = 4
                    break
            assert phase == 4, f'Native TUI check stopped at phase {phase}: {controller.status()}'
            print(f'Observed {bridge.history_reads} background history reads; dial stayed available.')
        finally:
            stop.set()
            worker.join(4)
            proc.terminate()
            deadline = time.monotonic() + 3
            # Keep draining during shutdown so the child's final screen cannot
            # fill its PTY and block process reaping.
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
            bridge.close()


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    check(sys.argv[1])
