from __future__ import annotations

import unittest

from video_factory.errors import ValidationError
from video_factory.quality_score import evaluate_quality


def editorial(**overrides):
    value = {
        "visual_relevance": 1.0,
        "narrative_turn": 1.0,
        "opening_truthfulness": 1.0,
        "payoff": 1.0,
        "factual_review_passed": True,
        "freshness_review_passed": True,
        "rights_manifest_passed": True,
        "caption_review_passed": True,
        "technical_qc_passed": True,
        "visual_provenance_passed": True,
        "human_editor_approved": True,
    }
    value.update(overrides)
    return value


class QualityScoreTestCase(unittest.TestCase):
    def test_reference_quality_requires_score_and_all_gates(self) -> None:
        result = evaluate_quality(
            preflight={"checks": {"a": True, "b": True}},
            editorial=editorial(),
            originality={"decision": "allow", "similarity": 0.1},
        )
        self.assertTrue(result["reference_quality"])
        self.assertEqual(result["blockers"], [])
        self.assertGreaterEqual(result["score"], result["threshold"])

    def test_high_score_cannot_override_rights_gate(self) -> None:
        result = evaluate_quality(
            preflight={"checks": {"a": True, "b": True}},
            editorial=editorial(rights_manifest_passed=False),
            originality={"decision": "allow", "similarity": 0.0},
        )
        self.assertFalse(result["reference_quality"])
        self.assertIn("rights_manifest_passed", result["blockers"])

    def test_originality_review_is_blocking(self) -> None:
        result = evaluate_quality(
            preflight={"checks": {"a": True}},
            editorial=editorial(),
            originality={"decision": "review", "similarity": 0.7},
        )
        self.assertFalse(result["publish_ready"])
        self.assertIn("originality_review_required", result["blockers"])

    def test_stale_fact_check_is_a_hard_blocker(self) -> None:
        result = evaluate_quality(
            preflight={"checks": {"a": True}},
            editorial=editorial(freshness_review_passed=False),
            originality={"decision": "allow", "similarity": 0.0},
        )
        self.assertFalse(result["publish_ready"])
        self.assertIn("freshness_review_passed", result["blockers"])

    def test_invalid_subjective_score_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            evaluate_quality(
                preflight={"checks": {"a": True}},
                editorial=editorial(payoff=1.1),
                originality={"decision": "allow", "similarity": 0.0},
            )


if __name__ == "__main__":
    unittest.main()
