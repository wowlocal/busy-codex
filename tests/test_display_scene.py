import unittest

from display_scene import DrawCache


class Transport:
    def __init__(self):
        self.payloads = []
        self.accept = True

    def draw(self, payload):
        self.payloads.append(payload)
        return self.accept


class DrawCacheTest(unittest.TestCase):
    def setUp(self):
        self.scene = DrawCache('test', 30)
        self.transport = Transport()

    def test_dashboard_changes_share_a_request_but_keep_independent_expiry(self):
        ring = [{'id': 'ring', 'type': 'animation', 'path': 'work.anim'}]
        text = [{'id': 'label', 'type': 'text', 'text': 'WORK'}]
        self.assertTrue(self.scene.draw(self.transport,
            self.scene.pending('ring', ring, 100, 60),
            self.scene.pending('text', text, 100, 8)))
        self.assertEqual([{'application_name': 'test', 'priority': 30,
                          'elements': ring + text}], self.transport.payloads)
        self.assertFalse(self.scene.draw(self.transport,
            self.scene.pending('ring', ring, 101, 60),
            self.scene.pending('text', text, 101, 8)))
        self.scene.draw(self.transport,
            self.scene.pending('ring', ring, 108, 60),
            self.scene.pending('text', text, 108, 8))
        self.assertEqual(text, self.transport.payloads[-1]['elements'])

    def test_reconnecting_sends_latest_state_instead_of_replaying_failed_updates(self):
        self.transport.accept = False
        self.scene.draw(self.transport,
            self.scene.pending('text', [{'id': 'label', 'text': 'HIGH'}], 1, 8))
        self.transport.accept = True
        latest = [{'id': 'label', 'text': 'ULTRA'}]
        self.scene.draw(self.transport, self.scene.pending('text', latest, 2, 8))
        self.assertEqual(latest, self.transport.payloads[-1]['elements'])
        self.assertEqual(2, len(self.transport.payloads))
        self.assertIsNone(self.scene.pending('text', latest, 3, 8))

    def test_rejected_draw_remains_pending_and_reset_repaints(self):
        elements = [{'id': 'label', 'text': 'WORK'}]
        self.transport.accept = False
        self.assertFalse(self.scene.draw(self.transport,
            self.scene.pending('text', elements, 100, 8)))
        self.transport.accept = True
        self.assertTrue(self.scene.draw(self.transport,
            self.scene.pending('text', elements, 101, 8)))
        self.scene.reset()
        self.assertIsNotNone(self.scene.pending('text', elements, 102, 8))

    def test_avatar_updates_even_while_an_astra_ring_keeps_the_same_path(self):
        ring = {'id': 'ring', 'path': 'astra.anim'}
        working = [ring, {'id': 'avatar', 'path': 'avatar_work.anim'}]
        waiting = [ring, {'id': 'avatar', 'path': 'avatar_wait.anim'}]
        self.scene.draw(self.transport, self.scene.pending('animations', working, 1, 60))
        self.assertTrue(self.scene.draw(self.transport,
            self.scene.pending('animations', waiting, 2, 60)))
        self.assertEqual(waiting, self.transport.payloads[-1]['elements'])


if __name__ == '__main__':
    unittest.main()
