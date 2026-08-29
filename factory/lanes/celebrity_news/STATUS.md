# Статус lane `celebrity_news`

Статус: `ARMED_WAITING_FOR_START`. Постоянная команда запуска — `начинаем`; сейчас производство не запущено.

Дата проверки: 2026-08-30 (Europe/Simferopol).

## Создано

- `TOPIC_PACK.json` — тематический профиль, источники, запреты, визуальные правила, сценарные варианты и hard gates.
- `EDITORIAL_PLAYBOOK.md` — рабочий цикл для русскоязычных вертикальных роликов 60–80 секунд.
- `SOURCE_POLICY.md` — иерархия источников, проверка цитат и подробная политика прав на фото/видео/аудио с визуальными заменами.
- `SAFETY.md` — stop-лист для слухов, диагнозов, частной информации, непроверенных отношений/беременностей/смертей, клеветы и синтетических подделок.
- `candidate_pool.json` — 24 обновляемых формата-шаблона; конкретные текущие события намеренно не заявлены без свежей проверки.
- `OPERATING_MODE.md` — постоянная state machine: slate 3–5 тем, ответы
  `да/нет`, registry-controlled производство, human rights/preview/final gates и
  запрет внешней публикации без отдельного разрешения.

## Проверки

- [x] `TOPIC_PACK.json` и `candidate_pool.json` разобраны `ConvertFrom-Json` без ошибок.
- [x] В `candidate_pool.json` — 24 шаблона и 24 уникальных ID; неполных шаблонов нет.
- [x] Дефолтная длительность пула и тематического пакета — 60–80 секунд.
- [x] Машинно подтверждено наличие запретов на слухи, диагнозы, непроверенные отношения, беременности, смерти, скрытые личные сведения и выдачу старого за новое.
- [x] Все семь файлов конфигурации присутствуют в `factory/lanes/celebrity_news/`.

## Постоянный operational contract

- На команду `начинаем` перечитать `TOPIC_PACK.json`, `EDITORIAL_PLAYBOOK.md`, `SOURCE_POLICY.md`, `SAFETY.md` и `OPERATING_MODE.md`.
- Перепроверить актуальные/evergreen-темы и показать slate из 3–5 вариантов с прямыми источниками и риском.
- После 2–3 ответов `да` провести ролики по точной цепочке
  `celebrity_news.roles` из `factory/lanes/registry.json`; не автоматизировать
  `rights`, `preview_review`, `final_review` и публикацию.
- Сохранить все артефакты только в `factory/runs/YYYY-MM-DD/celebrity_news/`.
- Применять medical, privacy и military-history gates по принципу fail-closed.
- Не публиковать во внешние соцсети без отдельного явного разрешения после review MP4.

## Интеграционная оговорка

`candidate_pool.json` намеренно хранит discovery-шаблоны, а не выдаёт
старые события за актуальные новости. Перед pipeline чат создаёт датированный
IdeaCard и повторно проверяет факт непосредственно перед выпуском.

Канонический DAG:

`research -> privacy_review -> media_discovery -> rights (human) -> media -> script -> voice -> editor -> bgm -> audio_mix -> compiler -> preview_review (human) -> render -> qc_auto_evidence -> caption_transcript -> captions_analyzer -> facts_analyzer -> policy_analyzer -> dedup_analyzer -> visual_analyzer -> qc_evidence_gate -> qc -> final_review (human) -> publisher`
