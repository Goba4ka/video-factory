from __future__ import annotations

import unittest

from video_factory.errors import ValidationError
from video_factory.freshness import evaluate_freshness


class FreshnessGateTestCase(unittest.TestCase):
    def test_celebrity_news_holds_after_two_hours(self) -> None:
        result = evaluate_freshness(
            lane="celebrity_news",
            checked_at="2026-08-29T08:00:00+03:00",
            now="2026-08-29T10:00:01+03:00",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["decision"], "hold")
        self.assertEqual(result["blockers"], ["fact_check_stale"])

    def test_timezone_is_required(self) -> None:
        with self.assertRaises(ValidationError):
            evaluate_freshness(
                lane="celebrity_news",
                checked_at="2026-08-29T08:00:00",
                now="2026-08-29T08:30:00+03:00",
            )


if __name__ == "__main__":
    unittest.main()
