import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import animgen
import codex_effort
import codex_fast
import daemon
import fast_animation


class FastTest(unittest.TestCase):
    def test_start_only_once_until_release_and_respects_canvas_ownership(self):
        controller = mock.Mock()
        with mock.patch.object(daemon, 'EFFORT_CONTROLLER', controller), \
             mock.patch.object(daemon, 'START_DOWN', False), \
             mock.patch.object(daemon, 'LAST_START', {}), \
             mock.patch.object(daemon, 'effort_input_allowed', return_value=True):
            self.assertTrue(daemon.handle_device_input_event(('button', 2, 0)))
            self.assertFalse(daemon.handle_device_input_event(('button', 2, 0)))
            self.assertFalse(daemon.handle_device_input_event(('button', 2, 2)))
            self.assertFalse(daemon.handle_device_input_event(('button', 2, 1)))
            daemon.handle_device_input_event(('button', 2, 0))
            self.assertEqual(2, controller.toggle_fast.call_count)
        controller.reset_mock()
        with mock.patch.object(daemon, 'EFFORT_CONTROLLER', controller), \
             mock.patch.object(daemon, 'START_DOWN', False), \
             mock.patch.object(daemon, 'effort_input_allowed', return_value=False):
            self.assertFalse(daemon.handle_device_input_event(('button', 2, 0)))
            controller.toggle_fast.assert_not_called()

    def test_desktop_fast_toggles_confirmed_task_only_and_preserves_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'models_cache.json').write_text(json.dumps({'models': [{
                'slug': 'test-model', 'service_tiers': [{'id': 'priority', 'name': 'Fast'}]}]}))
            settings = {'model': 'test-model', 'effort': 'ultra', 'serviceTier': 'default',
                        'collaborationMode': {'mode': 'plan', 'settings': {'reasoning_effort': 'ultra'}}}
            state = {'latestThreadSettings': copy.deepcopy(settings)}
            controller = codex_effort.Controller(lambda: 'a', lambda: None, home=directory)
            ipc, stop = mock.Mock(), threading.Event()
            def snapshot(*_):
                controller.on_change({'type': 'snapshot', 'revision': 1, 'conversationState': state})
            def request(method, params, target):
                self.assertEqual({'serviceTier', 'collaborationMode'}, set(params['threadSettings']))
                self.assertEqual(settings['collaborationMode'], params['threadSettings']['collaborationMode'])
                self.assertEqual('a', params['conversationId'])
                state['latestThreadSettings'].update(params['threadSettings'])
                snapshot()
            ipc.connect.side_effect = snapshot
            ipc.receive.side_effect = snapshot
            ipc.request.side_effect = request
            with mock.patch.object(codex_effort, 'DesktopIPC', return_value=ipc), \
                 mock.patch.object(codex_effort.select, 'select', return_value=([], [], [])):
                worker = threading.Thread(target=controller.run, args=(stop,))
                worker.start()
                try:
                    deadline = time.monotonic() + 2
                    while not controller.status()['connected'] and time.monotonic() < deadline:
                        time.sleep(.01)
                    for label, fast in (('FAST', True), ('NORMAL', False)):
                        self.assertTrue(controller.toggle_fast())
                        while controller.status()['feedback'] != label and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertEqual(label, controller.status()['feedback'])
                        self.assertEqual(fast, controller.status()['fast'])
                    self.assertEqual(settings, state['latestThreadSettings'])
                finally:
                    stop.set()
                    worker.join(1)

    def test_catalog_default_fast_is_explicitly_switched_off(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'models_cache.json').write_text(json.dumps({'models': [{
                'slug': 'test', 'default_service_tier': 'priority',
                'service_tiers': [{'id': 'priority', 'name': 'Fast'}]}]}))
            state = {'latestThreadSettings': {'model': 'test', 'serviceTier': None}}
            self.assertEqual(({'serviceTier': 'default'}, False),
                             codex_fast.toggle_settings(state, directory, 'desktop'))

    def test_fast_does_not_cross_task_selection_or_ownership(self):
        target, allowed = ['a'], [True]
        controller = codex_effort.Controller(lambda: target[0], lambda: None, allowed=lambda: allowed[0])
        controller.thread_id, controller.connected = 'a', True
        self.assertTrue(controller.toggle_fast())
        target[0] = 'b'
        self.assertFalse(controller.toggle_fast())
        target[0], allowed[0] = 'a', False
        self.assertFalse(controller.toggle_fast())
        self.assertEqual(1, len(controller.pending_fast))

    def test_scenes_remain_readable_roundtrip_and_end_transparent(self):
        for enabled in (True, False):
            for entering in (True, False):
                frames = fast_animation.frames(enabled, entering)
                animgen.decode_check(animgen.encode_anim(frames, fps=fast_animation.FPS), frames)
                self.assertFalse(any(frames[-1]))
                self.assertEqual({255}, set(frames[3][3::4]))
                # Hundreds of white label pixels, with no resampled font edges.
                colors = list(zip(*(iter(frames[10]),) * 4))
                mask = fast_animation.EFFORT_BOLD.layout('FAST' if enabled else 'NORMAL')
                self.assertEqual(colors.count((255, 252, 248, 255)), len(mask.pixels))
                self.assertEqual(fast_animation.filename(enabled, entering),
                    daemon.effort_overlay_elements('FAST' if enabled else 'NORMAL', entering=entering)[0]['path'])
        self.assertNotEqual(fast_animation.frame(True, 15), fast_animation.frame(False, 15))


if __name__ == '__main__':
    unittest.main()
