import copy
import unittest
from unittest.mock import patch

from codex_effort import Controller
from codex_model_menu import model_settings


class ModelMenuTest(unittest.TestCase):
    def controller(self):
        c = Controller(lambda: 'task', lambda: None)
        c.thread_id, c.connected = 'task', True
        c.state = {'latestThreadSettings': {'model': 'one', 'effort': 'high'}}
        c.model_choices = [dict(model=m, levels=['low', 'high'], default='low') for m in ['one', 'two']]
        return c

    def test_browse_only_writes_after_confirmation(self):
        c = self.controller()
        self.assertTrue(c.crown())
        self.assertTrue(c.rotate(1))
        self.assertEqual(c.status()['model_menu']['model'], 'two')
        self.assertEqual(c.pending, 0)
        self.assertIsNone(c.pending_model)
        self.assertTrue(c.crown())
        self.assertIsNone(c.status()['model_menu'])
        self.assertEqual(c.pending_model[0]['model'], 'two')

    def test_cancel_timeout_and_focus_loss_do_not_apply_preview(self):
        c = self.controller()
        c.crown()
        c.rotate(1)
        self.assertTrue(c.cancel_menu())
        self.assertIsNone(c.pending_model)
        c.crown()
        c.menu.until = 0
        self.assertIsNone(c.status()['model_menu'])
        self.assertIsNone(c.pending_model)
        c.crown()
        c.target = lambda: 'other'
        self.assertFalse(c.crown())
        self.assertFalse(c.rotate(1))
        self.assertIsNone(c.pending_model)

    def test_plan_preserved_and_unsupported_effort_uses_default(self):
        state = {'latestThreadSettings': {'model': 'one', 'effort': 'ultra',
                 'collaborationMode': {'mode': 'plan', 'settings': {
                     'model': 'one', 'reasoning_effort': 'ultra', 'developer_instructions': 'keep'}}}}
        before = copy.deepcopy(state)
        result = model_settings(state, 'two', ['low', 'high'], 'low')
        self.assertEqual(result, {'model': 'two', 'effort': 'low', 'collaborationMode': {
            'mode': 'plan', 'settings': {'model': 'two', 'reasoning_effort': 'low', 'developer_instructions': 'keep'}}})
        self.assertEqual(state, before)

    def test_crown_release_and_repeat_do_not_confirm(self):
        import daemon
        c = self.controller()
        with patch.object(daemon, 'EFFORT_CONTROLLER', c), patch.object(daemon, 'CROWN_DOWN', False), \
                patch.object(daemon, 'effort_input_allowed', return_value=True), \
                patch.object(daemon, 'astra_app_status', return_value={'active': False}):
            self.assertTrue(daemon.handle_device_input_event(('button', 0, 0)))
            self.assertFalse(daemon.handle_device_input_event(('button', 0, 0)))
            self.assertFalse(daemon.handle_device_input_event(('button', 0, 1)))
            self.assertIsNone(c.pending_model)
            self.assertTrue(daemon.handle_device_input_event(('button', 0, 0)))
            self.assertIsNotNone(c.pending_model)

    def test_menu_rendering_fits_display_and_clears_overlay(self):
        import daemon
        elements = daemon.model_menu_elements({'model': 'gpt-5.3-codex-spark', 'index': 6, 'count': 7})
        self.assertTrue(all(daemon.est_width(e['text']) <= 69 for e in elements if e['type'] == 'text'))
        cleared = daemon.model_menu_elements(None)
        self.assertEqual([e['id'] for e in elements], [e['id'] for e in cleared])
        self.assertEqual('#00000000', cleared[0]['fill_colors'][0])
