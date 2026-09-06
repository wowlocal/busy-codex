"""Opt-in native TUI check. Uses an isolated home and submits no model prompts.

python3 tests/check_codex_cli_native_live.py /path/to/native/codex-or-launcher
The existing login and model cache are copied into a private temporary home.
Tests effort, Plan mode, native focus, stale selection and request replay.
The title is an independent test assertion; it is never used to route control.
"""
import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_cli_native import NativeCLIIPC
from codex_cli_title import TitleOutput
from codex_effort import Controller


def check_dial(record, home):
    info = {'kind': 'cli', 'native_control': True, 'thread_id': record['threadId'],
            'socket': record['socket']}
    controller = Controller(lambda: record['threadId'], lambda: None, home=home,
                            target_info=lambda: info)
    stop = threading.Event()
    worker = threading.Thread(target=controller.run, args=(stop,), daemon=True)
    worker.start()
    try:
        wait_for(lambda: controller.status()['connected'], 'Dial controller did not connect')
        timings = []
        for delta in (1, 1, 1, 1, 1, -1, -1, -1):
            revision = controller.status()['feedback_revision']
            assert controller.rotate(delta)
            wait_for(lambda: controller.status()['feedback_revision'] > revision,
                     'Dial controller did not confirm a step')
            status = controller.status()
            assert not status['error'], status['error']
            timings.append(status['confirmation_ms'])
        print('PASS: native dial confirmation latency (ms):', timings, flush=True)
    finally:
        stop.set()
        worker.join(2)


def wait_for(predicate, message, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.03)
    raise AssertionError(message)


def check(binary):
    original = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    with tempfile.TemporaryDirectory(prefix='busy-native-live-', dir='/tmp') as directory:
        home = Path(directory).resolve()
        for name in ('auth.json', 'models_cache.json'):
            if (original / name).exists():
                shutil.copyfile(original / name, home / name)
        (home / 'config.toml').write_text('model="gpt-6-astra"\nmodel_reasoning_effort="low"\n'
            'plan_mode_reasoning_effort="max"\ncheck_for_update_on_startup=false\n'
            f'[projects.{json.dumps(str(home))}]\ntrust_level="trusted"\n')
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 160, 0, 0))
        env = dict(os.environ, CODEX_HOME=str(home), CODEX_TUI_CONTROL='1',
                   TERM='xterm-256color', TERM_PROGRAM='busy-test',
                   CODEX_FORK_CONFIG=str(original / 'config.toml'))
        for key in ('CODEX_THREAD_ID','CODEX_SESSION_ID'):
            env.pop(key, None)
        proc = subprocess.Popen([binary, '--yolo', '-c',
            'tui.terminal_title=["thread-id","model","reasoning"]'],
            stdin=slave, stdout=slave, stderr=slave, env=env, cwd=home, start_new_session=True)
        os.close(slave)
        stopped, title = threading.Event(), ['']
        parser = TitleOutput(lambda value: title.__setitem__(0, value))

        def drain():
            while not stopped.is_set():
                if select.select([master], [], [], .05)[0]:
                    try:
                        data = os.read(master, 65536)
                        if not data:
                            return
                        if b'\x1b[6n' in data:
                            os.write(master, b'\x1b[1;1R')
                        parser.feed(data)
                    except OSError:
                        return

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        client = None
        try:
            def ready():
                paths = list((home / 'tui-control').glob('*/session.json'))
                return paths and json.loads(paths[0].read_text()).get('ready')
            wait_for(ready, 'Native TUI endpoint not ready')
            record_path = next((home / 'tui-control').glob('*/session.json'))
            record = json.loads(record_path.read_text())
            client = NativeCLIIPC(record['socket'], lambda _: None)
            client.connect(record['threadId'])
            for effort in client.state['supported_efforts'][1:] + ['low']:
                client.receive()
                client.request('thread-follower-update-thread-settings', {'threadSettings': {'effort': effort}})
                wait_for(lambda: title[0].endswith(' | ' + effort), 'TUI did not show ' + effort)
                print('PASS: native TUI effort', effort, flush=True)

            os.write(master, b'/plan')
            time.sleep(.3)
            os.write(master, b'\r')
            def in_plan():
                client.receive()
                return client.state['collaborationMode'] == 'plan'
            wait_for(in_plan, 'Plan mode did not activate')
            client.request('thread-follower-update-thread-settings', {'threadSettings': {'effort': 'high'}})
            assert client.state['collaborationMode'] == 'plan'
            print('PASS: effort changes preserve Plan mode', flush=True)

            os.write(master, b'\x1b[O')
            def unfocused():
                client.receive()
                return not client.state['focused']
            wait_for(unfocused, 'Native focus loss not reported')
            try:
                client.request('thread-follower-update-thread-settings', {'threadSettings': {'effort': 'low'}})
                raise AssertionError('Unfocused effort change was accepted')
            except ValueError as error:
                assert 'focused' in str(error), error
            assert client.state['effort'] == 'high'
            print('PASS: native focus loss blocks mutation', flush=True)
            os.write(master, b'\x1b[I')
            wait_for(lambda: client.receive()['result']['focused'], 'Native focus gain not reported')

            request = {'method':'effort/set', 'requestId':str(uuid.uuid4()),
                'expectedRevision':client.state['revision'], 'expectedThreadId':client.thread_id,'effort':'low'}
            client.raw_rpc(request)
            client.close()
            client.connect(record['threadId'])
            def applied():
                return client.raw_rpc({'method':'request/read','requestId':request['requestId']})['status'] == 'applied'
            wait_for(applied, 'Native request result lost after reconnect')
            assert client.raw_rpc(request)['status'] == 'applied'
            client.receive()
            print('PASS: reconnect and duplicate request preserve confirmed result', flush=True)

            client.close()
            check_dial(record, home)
            client.connect(record['threadId'])

            old_thread, old_revision = client.thread_id, client.state['revision']
            os.write(master, b'/new')
            time.sleep(.3)
            os.write(master, b'\r')
            wait_for(lambda: json.loads(record_path.read_text()).get('threadId') not in (None, old_thread), 'Native task selection did not change')
            result = client.raw_rpc({'method':'effort/set','requestId':str(uuid.uuid4()),
                'expectedRevision':old_revision,'expectedThreadId':old_thread,'effort':'xhigh'})
            request_id = result['requestId']
            wait_for(lambda: client.raw_rpc({'method':'request/read','requestId':request_id})['status'] == 'rejected', 'Stale selected task was accepted')
            print('PASS: native task switch rejects stale target', flush=True)
        finally:
            if client:
                client.close()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stopped.set()
            reader.join(1)
            os.close(master)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    check(sys.argv[1])
