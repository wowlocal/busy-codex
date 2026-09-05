import copy
import json
from pathlib import Path
import tempfile
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
        self.assertNotEqual(up[3], down[3])
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
        self.assertEqual(48, width)
        self.assertEqual(12, max(y for _, y in pixels) + 1)
        self.assertGreater(len(pixels), 250)
        switching = animation.frames('ultra', n=1, entering=False)
        self.assertTrue(all(alpha == 255 for alpha in switching[0][3::4]))

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
