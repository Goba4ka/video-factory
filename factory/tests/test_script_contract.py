from __future__ import annotations

import copy
import unittest

from video_factory.contracts import validate_artifact
from video_factory.errors import ValidationError


def valid_script() -> dict:
    return {
        "schema_version": "1.0.0",
        "idea_id": "health_idea_001",
        "job_id": "job_health_001",
        "lane_id": "health",
        "language": "ru",
        "target_duration_seconds": 18,
        "hook": {
            "spoken_text": "Горячая вода не делает руки чище.",
            "first_frame_text": "ГОРЯЧАЯ ВОДА НЕ НУЖНА?",
            "duration_seconds": 1.8,
        },
        "segments": [
            {
                "segment_id": "hook",
                "start_seconds": 0,
                "end_seconds": 5.5,
                "spoken_text": "Горячая вода не удаляет бактерии лучше прохладной.",
                "caption_text": "НЕ УДАЛЯЕТ ЛУЧШЕ",
                "visual_intent": "Крупный план мытья рук",
                "claim_ids": ["claim_01"],
            },
            {
                "segment_id": "proof",
                "start_seconds": 5.5,
                "end_seconds": 12,
                "spoken_text": "Главное — мыло и полное трение ладоней.",
                "caption_text": "ГЛАВНОЕ — МЫЛО",
                "visual_intent": "Пена между пальцами",
                "claim_ids": ["claim_02"],
            },
            {
                "segment_id": "payoff",
                "start_seconds": 12,
                "end_seconds": 18,
                "spoken_text": "Выбирайте комфортную температуру и мойте около двадцати секунд.",
                "caption_text": "ОКОЛО 20 СЕКУНД",
                "visual_intent": "Таймер и чистые руки",
                "claim_ids": ["claim_03"],
            },
        ],
        "caption_style": {
            "max_lines": 2,
            "max_words_per_card": 4,
            "safe_zone": "center_lower_third",
            "side_labels": False,
        },
        "edit_direction": {
            "visual_world": "Холодный клинический реализм",
            "music_mood": "Сдержанное напряжение",
            "average_cut_seconds": 2.2,
            "speaker_scale": 0.9,
        },
        "disclaimer": "Образовательный материал, не медицинская консультация.",
        "decision": {
            "passed": True,
            "needs_human_review": False,
            "review_notes": [],
        },
    }


class ScriptContractTestCase(unittest.TestCase):
    def test_valid_russian_script_passes(self) -> None:
        document = valid_script()
        self.assertIs(validate_artifact("script_package", document), document)

    def test_english_caption_fails_ru_contract(self) -> None:
        document = copy.deepcopy(valid_script())
        document["segments"][1]["caption_text"] = "SOAP MATTERS"
        with self.assertRaisesRegex(ValidationError, "must be Russian"):
            validate_artifact("script_package", document)

    def test_side_labels_and_long_hook_are_rejected(self) -> None:
        document = copy.deepcopy(valid_script())
        document["caption_style"]["side_labels"] = True
        with self.assertRaises(ValidationError):
            validate_artifact("script_package", document)
        document = copy.deepcopy(valid_script())
        document["hook"]["duration_seconds"] = 3
        with self.assertRaises(ValidationError):
            validate_artifact("script_package", document)

    def test_overlaps_and_caption_density_are_rejected(self) -> None:
        document = copy.deepcopy(valid_script())
        document["segments"][1]["start_seconds"] = 5
        with self.assertRaisesRegex(ValidationError, "must not overlap"):
            validate_artifact("script_package", document)
        document = copy.deepcopy(valid_script())
        document["segments"][0]["caption_text"] = "СЛИШКОМ МНОГО СЛОВ В ОДНОЙ КАРТОЧКЕ"
        with self.assertRaisesRegex(ValidationError, "max_words_per_card"):
            validate_artifact("script_package", document)


if __name__ == "__main__":
    unittest.main()
