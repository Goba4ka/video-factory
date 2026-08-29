from __future__ import annotations

import unittest

from video_factory.performance import evaluate_performance


def snapshot(hook, hold, shares, saves, follows, *, events=None):
    return {
        "engaged_views": 1000,
        "stayed_to_watch_rate": hook,
        "average_percentage_viewed": hold,
        "shares": shares,
        "saves": saves,
        "follows": follows,
        "policy_events": events or [],
    }


class PerformanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cohort = [
            snapshot(0.45, 0.55, 8, 4, 2),
            snapshot(0.50, 0.60, 10, 5, 3),
            snapshot(0.55, 0.65, 12, 7, 4),
            snapshot(0.60, 0.70, 16, 9, 5),
            snapshot(0.65, 0.75, 20, 12, 7),
        ]

    def test_winner_beats_hook_and_hold_medians(self) -> None:
        result = evaluate_performance(
            snapshot(0.70, 0.80, 18, 10, 6), self.cohort
        )
        self.assertTrue(result["winner"])
        self.assertEqual(result["maximum_followups"], 2)

    def test_policy_event_blocks_winner(self) -> None:
        result = evaluate_performance(
            snapshot(
                0.70,
                0.80,
                30,
                20,
                10,
                events=[{"kind": "copyright", "status": "claim"}],
            ),
            self.cohort,
        )
        self.assertFalse(result["winner"])
        self.assertFalse(result["safety_clear"])

    def test_small_cohort_does_not_claim_a_winner(self) -> None:
        result = evaluate_performance(snapshot(0.9, 0.9, 30, 20, 10), self.cohort[:3])
        self.assertEqual(result["status"], "insufficient_cohort")
        self.assertFalse(result["winner"])


if __name__ == "__main__":
    unittest.main()
