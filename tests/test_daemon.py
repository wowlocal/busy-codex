import unittest

import daemon


class WeekProgressTest(unittest.TestCase):
    def test_progress_fills_toward_reset(self):
        now = 1_000_000.0
        self.assertEqual(
            0.0,
            daemon.week_progress_pct(
                {"resets_at": now + daemon.WEEK_SECONDS}, now,
            ),
        )
        self.assertEqual(
            50.0,
            daemon.week_progress_pct(
                {"resets_at": now + daemon.WEEK_SECONDS / 2}, now,
            ),
        )
        self.assertGreater(
            daemon.week_progress_pct({"resets_at": now + 1}, now),
            99.9,
        )

    def test_expired_or_missing_reset_is_empty(self):
        now = 1_000_000.0
        self.assertEqual(0.0, daemon.week_progress_pct({"resets_at": now}, now))
        self.assertIsNone(daemon.week_progress_pct({}, now))


class FastContourTest(unittest.TestCase):
    def test_fast_changes_only_the_working_contour(self):
        self.assertEqual("work.anim", daemon.anim_element("WORKING")["path"])
        self.assertEqual(
            "work_fast.anim",
            daemon.anim_element("WORKING", ["fast"])["path"],
        )
        self.assertEqual(
            "wait.anim",
            daemon.anim_element("WAIT", ["fast"])["path"],
        )


if __name__ == "__main__":
    unittest.main()
