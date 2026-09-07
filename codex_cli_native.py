"""Client for the fork's native, settings-only TUI control endpoint."""
import time
import uuid

from codex_cli_client import CLIIPC


def normalize(state):
    if not isinstance(state, dict) or state.get('protocolVersion') != 1:
        raise ValueError('Unsupported native CLI control version')
    return {**state, 'native_control': True, 'thread_id': state.get('threadId'),
            'supported_efforts': state.get('supportedEfforts'),
            'context_pct': state.get('contextPct')}


class NativeCLIIPC(CLIIPC):
    def raw_rpc(self, value):
        import json
        self.sock.sendall(json.dumps(value).encode() + b'\n')
        line = self.stream.readline(16385)
        if not line or len(line) > 16384:
            raise ConnectionError('Invalid native CLI response')
        result = json.loads(line)['result']
        if 'error' in result:
            raise ValueError(result['error'])
        return result

    def receive(self):
        self.poll_after = time.monotonic() + .2
        state = normalize(self.raw_rpc({'method': 'status/read'}))
        if state['thread_id'] != self.thread_id or not state.get('ready'):
            raise ValueError('Native CLI selected task changed or is not ready')
        if self.state != state:
            self.state = state
            self.revision += 1
            self.on_change({'type': 'snapshot', 'revision': self.revision,
                'conversationState': {'latestThreadSettings': {
                    'model': state['model'], 'effort': state['effort'],
                    **({key: state.get(key) for key in ('serviceTier', 'fastServiceTier')}
                       if 'serviceTier' in state else {})}}})
        return {'result': state}

    def request(self, method, params, target=None):
        if method != 'thread-follower-update-thread-settings':
            raise ValueError('Unsupported native CLI operation')
        settings = params['threadSettings']
        if 'serviceTier' in settings:
            tier = settings['serviceTier']
            fast_tier = self.state.get('fastServiceTier')
            if not fast_tier or tier not in (fast_tier, 'default'):
                raise ValueError('Restart the updated native CLI with Fast mode support')
            command = {'method': 'fast/set', 'enabled': tier == fast_tier}
            key, expected = 'serviceTier', tier
        else:
            key, expected = 'effort', settings['effort']
            command = {'method': 'effort/set', 'effort': expected}
        request_id = str(uuid.uuid4())
        instance, model = self.state['instanceId'], self.state['model']
        try:
            result = self.raw_rpc({**command, 'requestId': request_id,
                'expectedRevision': self.state['revision'],
                'expectedThreadId': self.thread_id})
        except OSError:
            # Recover the exact request result; never repeat a settings write.
            self.close()
            self.connect(self.thread_id)
            if self.state['instanceId'] != instance:
                raise ValueError('Native CLI instance changed')
            result = self.raw_rpc({'method': 'request/read', 'requestId': request_id})
        deadline = time.monotonic() + 8
        while result['status'] == 'pending' and time.monotonic() < deadline:
            time.sleep(.025)
            result = self.raw_rpc({'method': 'request/read', 'requestId': request_id})
        if result['status'] != 'applied':
            raise ValueError((result.get('outcome') or {}).get('error') or 'Native settings confirmation is pending')
        outcome = result['outcome']
        if (outcome.get('threadId'), outcome.get('model'), outcome.get(key)) != (self.thread_id, model, expected):
            raise ValueError('Native CLI confirmation does not match request')
        # Let the native TUI consume its confirmed event and refresh the widget.
        while time.monotonic() < deadline:
            self.receive()
            if self.state['model'] == model and self.state.get(key) == expected:
                return {'result': self.state}
            time.sleep(.025)
        raise ValueError('Native CLI display has not caught up to confirmed settings')
