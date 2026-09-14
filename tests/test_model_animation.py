import unittest

import animgen
import daemon
import model_animation as animation
from codex_model_menu import ModelMenu
from display_scene import DrawCache


class ModelAnimationTests(unittest.TestCase):
    def test_all_native_assets_roundtrip_and_transitions_clear(self):
        names = set()
        for name, frames in animation.assets():
            self.assertNotIn(name, names)
            names.add(name)
            animgen.decode_check(animgen.encode_anim(frames, fps=animation.FPS), frames)
            self.assertTrue(all(len(frame) == 72 * 16 * 4 for frame in frames))
            if name.endswith(('_right.anim', '_left.anim', '_set.anim')):
                self.assertFalse(any(frames[-1]))
            else:
                self.assertNotEqual(frames[0], frames[25])
        self.assertEqual(4 * len(animation.PROFILES), len(names))

    def test_profiles_are_explicit_not_derived_from_version_or_effort(self):
        self.assertEqual('neutral', animation.profile_key('gpt-99-ultra'))
        self.assertEqual('neutral', animation.profile_key('custom-provider-mini'))
        self.assertEqual('astra', animation.profile_key('GPT-6-Astra'))
        self.assertEqual([1, 2, 3, 4], [animation.PROFILES[k].rank
                                      for k in ('luna', 'terra', 'sol', 'astra')])
        self.assertEqual('5.3 SPARK', animation.label('gpt-5.3-codex-spark'))
        for key in animation.PROFILES:
            self.assertEqual(animation.frame(key, 0), animation.frame(key, animation.FRAMES))

    def test_titles_hints_and_geometry_fit_in_every_phase(self):
        for model in (*animation.MODEL_PROFILES, 'provider-a-very-long-distinct-model-suffix'):
            for phase in ('browse', 'saving', 'confirmed'):
                menu = dict(model=model, index=99, count=100, phase=phase,
                            effort='minimal', until=12, changed_at=0)
                for now in (0, 2, 5, 11.9):
                    for e in daemon.model_menu_elements(menu, now):
                        self.assertGreaterEqual(e['x'], 0)
                        self.assertGreaterEqual(e['y'], 0)
                        if e['type'] == 'text':
                            self.assertLessEqual(e['x'] + daemon.est_width(e['text']), 72)
                        else:
                            self.assertLessEqual(e['x'] + e['width'], 72)
                            self.assertLessEqual(e['y'] + e['height'], 16)
                self.assertEqual(animation.label(model), ''.join(daemon.model_name_pages(menu)))

    def test_native_orbit_is_not_restarted_by_countdown_or_label_pages(self):
        cache = DrawCache('test', 30)
        class Transport:
            def draw(self, payload):
                return True
        menu = dict(model='gpt-6-astra', index=0, count=8, until=12, changed_at=0)
        background = daemon.model_menu_animation_elements(menu)
        cache.draw(Transport(), cache.pending('bg', background, 0, 12))
        first = daemon.model_menu_elements(menu, 0)
        later = daemon.model_menu_elements(menu, 3)
        self.assertNotEqual(first, later)
        self.assertIsNone(cache.pending('bg', background, 3, 12))
        self.assertEqual('effort_clear.anim', daemon.model_menu_animation_elements(None)[0]['path'])
        self.assertEqual('effort_clear.anim', daemon.model_menu_transition_elements(None)[0]['path'])

    def test_detent_direction_boundaries_and_active_identity(self):
        menu = ModelMenu([dict(model='one'), dict(model='two')], 'one', 10)
        self.assertTrue(menu.snapshot()['active'])
        menu.rotate(100, 11)
        self.assertEqual(1, menu.index)
        self.assertFalse(menu.snapshot()['active'])
        menu.rotate(1, 12)
        self.assertEqual(11, menu.snapshot()['changed_at'])
        self.assertEqual(24, menu.until)
        menu.rotate(-100, 13)
        self.assertEqual(-1, menu.snapshot()['direction'])
        self.assertTrue(menu.snapshot()['active'])


if __name__ == '__main__':
    unittest.main()
