from __future__ import annotations

import sys
import unittest
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.dedup import (  # noqa: E402
    DedupThresholds,
    canonical_tokens,
    compare_idea_cards,
    evaluate_candidate,
)


def idea(
    title: str,
    hook: str,
    message: str,
    *urls: str,
) -> dict[str, object]:
    return {
        "title": title,
        "hook": hook,
        "message": message,
        "source_candidates": [
            {"source_id": f"src-{index}", "url": url}
            for index, url in enumerate(urls, start=1)
        ],
    }


class DedupTestCase(unittest.TestCase):
    def test_exact_duplicate_blocks_after_unicode_canonicalization(self) -> None:
        left = idea(
            "Почему Ёж Видит Ночью?",
            "Как зрение помогает ёжику после заката",
            "Разбираем устройство глаза и ночное поведение животного.",
            "https://example.test/source-a",
        )
        right = idea(
            "  ПОЧЕМУ   ЁЖ видит ночью ",
            "Как зрение помогает ЁЖИКУ после заката!",
            "Разбираем устройство глаза — и ночное поведение животного",
            "https://example.test/source-b",
        )

        result = compare_idea_cards(left, right)

        self.assertEqual(result.decision, "block")
        self.assertEqual(result.score, 1.0)
        self.assertFalse(result.source_url_collision)
        self.assertEqual(canonical_tokens("КОСМОС и Ёж"), ("космос", "и", "ёж"))

    def test_paraphrase_is_sent_to_review(self) -> None:
        left = idea(
            "Зачем осьминог меняет цвет",
            "Осьминог меняет цвет не только для маскировки",
            "Цвет кожи помогает осьминогу маскироваться и передавать сигналы.",
            "https://example.test/octopus-a",
        )
        right = idea(
            "Почему осьминоги меняют цвет",
            "Маскировка — не единственная причина менять цвет кожи",
            "Осьминог использует цвет кожи для маскировки, а также для сигналов.",
            "https://example.test/octopus-b",
        )

        result = compare_idea_cards(left, right)

        self.assertEqual(result.decision, "review")
        self.assertGreaterEqual(result.score, 0.20)
        self.assertLess(result.score, 0.78)

    def test_unrelated_ideas_are_allowed(self) -> None:
        animal = idea(
            "Как пчёлы охлаждают улей",
            "Рабочие пчёлы превращают крылья в систему вентиляции",
            "Колония регулирует температуру движением воздуха и испарением воды.",
            "https://example.test/bees",
        )
        space = idea(
            "Почему марсоходу нужны автономные решения",
            "Сигнал с Земли приходит слишком поздно для мгновенного управления",
            "Бортовое программное обеспечение выбирает безопасный путь между препятствиями.",
            "https://example.test/rover",
        )

        result = compare_idea_cards(animal, space)

        self.assertEqual(result.decision, "allow")
        self.assertLess(result.score, 0.20)

    def test_exact_source_url_collision_blocks_unrelated_text(self) -> None:
        shared_url = "https://example.test/archive/item-42?view=full"
        left = idea(
            "История старого трамвая",
            "Один вагон изменил городской маршрут",
            "Архив объясняет развитие общественного транспорта.",
            shared_url,
        )
        right = idea(
            "Как спутник измеряет океан",
            "Высоту волн можно увидеть из космоса",
            "Радарные наблюдения помогают анализировать поверхность моря.",
            shared_url,
        )

        result = compare_idea_cards(left, right)

        self.assertEqual(result.decision, "block")
        self.assertEqual(result.reason, "exact_source_url_collision")
        self.assertEqual(result.shared_source_urls, (shared_url,))

    def test_similarity_and_decision_are_symmetric(self) -> None:
        left = idea(
            "Что скрывает фотография Сатурна",
            "Цвета на снимке Сатурна обработаны не случайно",
            "Фильтры научной камеры выделяют разные свойства атмосферы.",
            "https://example.test/saturn-a",
        )
        right = idea(
            "Как читать цветной снимок Сатурна",
            "Обработка цветов показывает детали атмосферы Сатурна",
            "Разные фильтры камеры помогают увидеть свойства атмосферы.",
            "https://example.test/saturn-b",
        )

        forward = compare_idea_cards(left, right)
        reverse = compare_idea_cards(right, left)

        self.assertEqual(forward.score, reverse.score)
        self.assertEqual(forward.decision, reverse.decision)
        self.assertEqual(forward.as_dict(), reverse.as_dict())

    def test_thresholds_are_configurable_and_collection_uses_strictest_match(self) -> None:
        candidate = idea("Одинаковая тема", "Похожий хук", "Похожее сообщение")
        existing = idea("Одинаковая тема", "Похожий хук", "Другое сообщение")
        thresholds = DedupThresholds(review=0.10, block=0.99)

        result = evaluate_candidate(candidate, [existing], thresholds=thresholds)

        self.assertEqual(result.decision, "review")
        self.assertEqual(result.best_match_index, 0)
        self.assertIsNotNone(result.best_match)


if __name__ == "__main__":
    unittest.main()
