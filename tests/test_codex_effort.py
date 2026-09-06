import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import codex_effort as effort
import daemon
from adapters import codex_status


class EffortTest(unittest.TestCase):
    def state(self):
        return {'latestThreadSettings': {'model': 'gpt-test', 'effort': 'high',
                'serviceTier': 'fast', 'collaborationMode': {'mode': 'plan',
                'settings': {'model': 'gpt-test', 'reasoning_effort': 'high',
                             'developer_instructions': 'preserve me'}}}}

    def test_payload_preserves_collaboration_and_does_not_send_other_settings(self):
        state = self.state()
        original = copy.deepcopy(state)
        update = effort.effort_settings(state, 'low')
        self.assertEqual({'effort', 'collaborationMode'}, set(update))
        self.assertEqual('plan', update['collaborationMode']['mode'])
        self.assertEqual('preserve me', update['collaborationMode']['settings']['developer_instructions'])
        self.assertEqual('low', update['collaborationMode']['settings']['reasoning_effort'])
        self.assertEqual(original, state)

    def test_catalog_is_model_specific_and_ordered(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'models_cache.json').write_text(json.dumps({'models': [
                {'slug': 'gpt-test', 'supported_reasoning_levels': [
                    {'effort': 'high'}, {'effort': 'low'}]}]}))
            self.assertEqual(['low', 'high'], effort.supported_efforts('gpt-test', d))
            with self.assertRaises(ValueError):
                effort.supported_efforts('unknown', d)

    def test_catalog_survives_missing_model_and_partial_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'models_cache.json')
            now = [10.0]
            path.write_text(json.dumps({'models': [{'slug': 'gpt-test',
                'supported_reasoning_levels': [{'effort': 'high'}, {'effort': 'ultra'}]}]}))
            catalog = effort.ModelCatalog(d, clock=lambda: now[0])
            for replacement in ('{"models": []}', '{"models": [', '[]', 'null'):
                path.write_text(replacement)
                now[0] += 1
                self.assertEqual(['high', 'ultra'], catalog.levels_for('gpt-test'))
            path.unlink()
            self.assertEqual(['high', 'ultra'], catalog.levels_for('gpt-test'))
            with self.assertRaisesRegex(effort.CatalogError, "model='unseen'"):
                catalog.levels_for('unseen')
            now[0] += effort.CATALOG_GRACE_S
            with self.assertRaises(effort.CatalogError):
                catalog.levels_for('gpt-test')

    def test_new_valid_catalog_overrides_cached_levels(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'models_cache.json')
            def write(levels):
                path.write_text(json.dumps({'models': [{'slug': 'gpt-test',
                    'supported_reasoning_levels': [{'effort': level} for level in levels]}]}))
            write(['low', 'high', 'ultra'])
            catalog = effort.ModelCatalog(d)
            write(['low', 'high'])
            self.assertEqual(['low', 'high'], catalog.levels_for('gpt-test'))
            write([])
            with self.assertRaises(effort.CatalogError):
                catalog.levels_for('gpt-test')

    def test_snapshot_discards_content_and_applies_only_ordered_settings_patches(self):
        state, rev = effort.apply_change({}, None, {'type': 'snapshot', 'revision': 10,
                'conversationState': dict(self.state(), turns=[{'text': 'private text'}])})
        self.assertNotIn('turns', state)
        state, rev = effort.apply_change(state, rev, {'type': 'patches', 'baseRevision': 10,
            'revision': 11, 'patches': [
                {'op': 'replace', 'path': ['latestThreadSettings', 'collaborationMode',
                                         'settings', 'reasoning_effort'], 'value': 'low'},
                {'op': 'add', 'path': ['turns', 0], 'value': 'ignore'}]})
        self.assertEqual(('gpt-test', 'low'), effort.model_effort(state))
        with self.assertRaises(ValueError):
            effort.apply_change(state, rev, {'type': 'patches', 'baseRevision': 9,
                                             'revision': 12, 'patches': []})

    def test_dial_accumulates_signed_steps_and_rejects_other_task_or_menu(self):
        target = ['a']
        allowed = [True]
        control = effort.Controller(lambda: target[0], lambda: None, allowed=lambda: allowed[0])
        self.assertFalse(control.rotate(1))
        control.thread_id, control.connected = 'a', True
        self.assertTrue(control.rotate(3))
        self.assertTrue(control.rotate(-1))
        self.assertEqual(2, control.pending)
        target[0] = 'b'
        self.assertFalse(control.rotate(1))
        target[0], allowed[0] = 'a', False
        self.assertFalse(control.rotate(1))

    def test_hardware_menu_and_overlay_keep_their_encoder(self):
        control = mock.Mock()
        with mock.patch.object(daemon, 'EFFORT_CONTROLLER', control), \
             mock.patch.object(daemon, 'effort_input_allowed', return_value=False):
            self.assertFalse(daemon.handle_device_input_event(('encoder', 1)))
            control.rotate.assert_not_called()
        with mock.patch.object(daemon, 'EFFORT_CONTROLLER', control), \
             mock.patch.object(daemon, 'effort_input_allowed', return_value=True):
            daemon.handle_device_input_event(('encoder', -2))
            control.rotate.assert_called_once_with(-2)

    def test_more_detents_do_not_push_back_the_first_write(self):
        control = effort.Controller(lambda: 'a', lambda: None)
        control.thread_id, control.connected = 'a', True
        with mock.patch.object(effort.time, 'monotonic', return_value=10):
            control.rotate(1)
        with mock.patch.object(effort.time, 'monotonic', return_value=10.08):
            control.rotate(1)
        self.assertEqual(10, control.due)
        self.assertTrue(control.wake.is_set())
        self.assertIsNone(control.status()['feedback'])

    def test_desktop_stream_does_not_starve_dial_settings(self):
        changed = threading.Event()
        control = effort.Controller(lambda: 'a', changed.set)
        ipc = mock.Mock()
        state = self.state()

        def snapshot(*_):
            control.on_change({'type': 'snapshot', 'revision': 1, 'conversationState': state})

        def request(method, params, target):
            state['latestThreadSettings'].update(params['threadSettings'])
            snapshot()

        ipc.connect.side_effect = snapshot
        ipc.receive.side_effect = snapshot
        ipc.request.side_effect = request
        ipc.levels_for.return_value = ['low', 'high', 'xhigh']
        stop = threading.Event()
        with mock.patch.object(effort, 'DesktopIPC', return_value=ipc), \
             mock.patch.object(effort.select, 'select', return_value=([ipc.sock], [], [])):
            worker = threading.Thread(target=control.run, args=(stop,))
            worker.start()
            try:
                self.assertTrue(changed.wait(1))
                self.assertTrue(control.rotate(1))
                # A continuously readable stream used to skip pending input.
                for _ in range(100):
                    if control.status()['feedback'] == 'XHIGH':
                        break
                    stop.wait(.01)
                self.assertEqual('XHIGH', control.status()['feedback'])
                ipc.request.assert_called_once()
            finally:
                stop.set()
                worker.join(1)

    def test_rejected_encoder_event_records_the_blocking_reason(self):
        with mock.patch.object(daemon, 'EFFORT_CONTROLLER', mock.Mock()), \
             mock.patch.object(daemon, 'effort_input_allowed', return_value=False), \
             mock.patch.object(daemon, 'effort_input_block_reason', return_value='No foreground task'), \
             mock.patch.object(daemon, 'log') as log, \
             mock.patch.object(daemon, 'LAST_ENCODER', {}):
            self.assertFalse(daemon.handle_device_input_event(('encoder', 2)))
            self.assertEqual(2, daemon.LAST_ENCODER['delta'])
            self.assertFalse(daemon.LAST_ENCODER['handled'])
            self.assertEqual('No foreground task', daemon.LAST_ENCODER['reason'])
            log.assert_called_once_with('Codex dial ignored: No foreground task')


class AnimationTest(unittest.TestCase):
    def test_all_labels_fit_and_native_codec_roundtrips(self):
        import effort_animation as animation
        import animgen
        for level in animation.LEVELS:
            pixels, width = animation.word_pixels(level.upper())
            self.assertLessEqual(width, 66)
            self.assertTrue(pixels)
        up = animation.frames('ultra', 1, n=12)
        down = animation.frames('ultra', -1, n=12)
        self.assertNotEqual(up[1], down[1])
        self.assertEqual(up[9], down[9])
        self.assertEqual(72 * 16 * 4, len(up[0]))
        animgen.decode_check(animgen.encode_anim(up), up)

    def test_overlay_fades_to_live_dashboard_and_text_is_bold(self):
        import effort_animation as animation
        frames = animation.frames('ultra')
        self.assertFalse(any(frames[0]))
        self.assertFalse(any(frames[-1]))
        self.assertTrue(all(alpha == 255 for alpha in frames[20][3::4]))
        self.assertTrue(all(0 < alpha < 255 for alpha in frames[-5][3::4]))
        pixels, width = animation.word_pixels('ULTRA')
        self.assertEqual(12, max(y for _, y in pixels) + 1)
        # The full word is legible after two frames and stays uniformly white.
        for x, y in pixels:
            index = ((y + 2) * 72 + x + (72 - width) // 2) * 4
            self.assertEqual(bytes((255, 250, 245, 255)), frames[2][index:index + 4])
        switching = animation.frames('ultra', n=1, entering=False)
        self.assertTrue(all(alpha == 255 for alpha in switching[0][3::4]))

    def test_higher_efforts_have_distinct_increasing_motion(self):
        import effort_animation as animation
        energies = []
        for rank in range(4, 8):
            motion = 0
            for f in range(8, 28):
                for x in range(0, 72, 2):
                    for y in (0, 1, 14, 15):
                        before = animation.background(rank, x, y, (f - 1) / animation.FPS)
                        after = animation.background(rank, x, y, f / animation.FPS)
                        motion += sum(abs(a - b) for a, b in zip(before, after))
            energies.append(motion)
        # Each tier must be visibly more active, not merely a recoloured loop.
        for lower, higher in zip(energies, energies[1:]):
            self.assertGreater(higher, lower * 1.25)

    def test_native_overlay_is_above_dashboard_and_ids_keep_types(self):
        normal = daemon.effort_overlay_elements('ULTRA')
        error = daemon.effort_overlay_elements('ERR')
        cleared = daemon.effort_overlay_elements(None)
        self.assertTrue(all(element['z_index'] >= 100 for element in normal))
        types = [(element['id'], element['type']) for element in normal]
        self.assertEqual(types, [(e['id'], e['type']) for e in error])
        self.assertEqual(types, [(e['id'], e['type']) for e in cleared])
        self.assertEqual('effort_clear.anim', cleared[0]['path'])


if __name__ == '__main__':
    unittest.main()
