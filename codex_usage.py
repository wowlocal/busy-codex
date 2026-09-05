"""Fresh account quotas via Codex's documented account/rateLimits/read API.

Only initialize and read account limits. No task is started, no model is called,
and authentication stays inside the installed Codex executable.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

POLL_S = 60
MAX_AGE_S = 180
REQUEST_TIMEOUT_S = 8


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def window_name(minutes):
    hours = minutes / 60
    return f'{round(hours)}h' if hours < 48 else f'{round(hours / 24)}d'


def account_quotas(result, observed_at, limit_id='codex'):
    """Select an explicit account bucket, never whichever model reported last."""
    buckets = result.get('rateLimitsByLimitId')
    if isinstance(buckets, dict):
        bucket = buckets.get(limit_id)
    else:
        bucket = result.get('rateLimits')
        if isinstance(bucket, dict) and bucket.get('limitId') not in (None, limit_id):
            bucket = None
    if not isinstance(bucket, dict):
        raise ValueError(f'Account limit bucket {limit_id!r} unavailable')
    quotas = []
    for key in ('primary', 'secondary'):
        window = bucket.get(key)
        if not isinstance(window, dict):
            continue
        used, minutes, reset = (window.get(k) for k in
                                ('usedPercent', 'windowDurationMins', 'resetsAt'))
        if not number(minutes) or minutes <= 0 or not number(used):
            continue  # null is unavailable, not zero usage
        reset = reset if number(reset) and reset > 0 else None
        quotas.append({'name': window_name(minutes),
                       'left_pct': max(0., min(100., 100 - used)),
                       'window_minutes': minutes, 'resets_at': reset,
                       'observed_at': observed_at,
                       'valid_until': observed_at + MAX_AGE_S})
    if not quotas:
        raise ValueError(f'No quota windows available for {limit_id!r}')
    return quotas


def codex_binary(env):
    override = env.get('BUSYBAR_CODEX_BIN')
    if override:
        return override
    for path in ('/Applications/ChatGPT.app/Contents/Resources/codex',
                 '/Applications/Codex.app/Contents/Resources/codex'):
        if Path(path).is_file():
            return path
    path = shutil.which('codex', path=env.get('PATH'))
    if not path:
        raise FileNotFoundError('Codex executable not found; set BUSYBAR_CODEX_BIN')
    return path


def read_rate_limits(env=None, stop=None):
    env = dict(os.environ if env is None else env)
    proc = subprocess.Popen(
        [codex_binary(env), 'app-server', '--listen', 'stdio://'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, text=True, bufsize=1,
    )
    replies = queue.Queue()

    def read_stdout():
        try:
            for line in proc.stdout:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        replies.put(value)
                except ValueError:
                    pass
        finally:
            replies.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + REQUEST_TIMEOUT_S

    def send(value):
        proc.stdin.write(json.dumps(value) + '\n')
        proc.stdin.flush()

    def request(request_id, method, params=None):
        value = {'id': request_id, 'method': method}
        if params is not None:
            value['params'] = params
        send(value)
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise InterruptedError('Usage reader stopped')
            try:
                reply = replies.get(timeout=.1)
            except queue.Empty:
                continue
            if reply is None:
                raise ConnectionError('Codex account reader disconnected')
            if reply.get('id') != request_id:
                continue
            if 'error' in reply:
                error = reply['error']
                code = error.get('code') if isinstance(error, dict) else None
                raise RuntimeError(f'Codex account request failed (code={code})')
            if not isinstance(reply.get('result'), dict):
                raise ValueError('Invalid Codex account response')
            return reply['result']
        raise TimeoutError('Codex account request timed out')

    try:
        request(1, 'initialize', {'clientInfo': {
            'name': 'busybar_usage', 'title': 'BUSY Bar usage display', 'version': '1.0.0'}})
        send({'method': 'initialized'})
        return request(2, 'account/rateLimits/read')
    finally:
        # A short-lived account reader picks up refreshed login state each poll
        # and cannot leave an idle app-server or a blocked stdout reader behind.
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        reader.join(timeout=1)
        proc.stdout.close()


class Monitor:
    def __init__(self, env=None, fetch=None, clock=time.time):
        self.env = dict(os.environ if env is None else env)
        self.limit_id = self.env.get('BUSYBAR_CODEX_LIMIT_ID', 'codex')
        self.fetch = fetch or (lambda stop: read_rate_limits(self.env, stop))
        self.clock = clock
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.quotas = []
        self.observed_at = None
        self.error = ''

    def refresh(self, stop=None):
        try:
            result = self.fetch(stop)
            observed_at = self.clock()
            quotas = account_quotas(result, observed_at, self.limit_id)
            with self.lock:
                self.quotas, self.observed_at, self.error = quotas, observed_at, ''
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            with self.lock:
                self.error = str(error)[:180]
        self.changed.set()

    def snapshot(self):
        with self.lock:
            now = self.clock()
            usable = any(q['valid_until'] > now and
                         (q['resets_at'] is None or q['resets_at'] > now)
                         for q in self.quotas)
            state = ('cached' if self.error else 'fresh') if usable else 'unavailable'
            return {'quotas': copy.deepcopy(self.quotas) or None,
                    'quota_status': {'source': 'codex-account', 'limit_id': self.limit_id,
                                     'state': state, 'observed_at': self.observed_at,
                                     'error': self.error}}

    def next_delay(self):
        with self.lock:
            if self.error:
                return 15
            # Fetch shortly after an actual reset, even if the next regular
            # minute has not arrived. Never manufacture a new seven-day window.
            remaining = [q['resets_at'] - self.clock() for q in self.quotas
                         if q['resets_at'] is not None]
            if any(seconds <= 0 for seconds in remaining):
                return 15
            return min([POLL_S] + [max(1., seconds + 1) for seconds in remaining])

    def run(self, stop):
        while not stop.is_set():
            self.refresh(stop)
            stop.wait(self.next_delay())
