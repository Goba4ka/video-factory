# Completion audit

> **Superseded:** this was the pre-render gap audit. The current verified state, including the final MP4, five production chats, 122 candidates and 88 passing tests, is in `factory/CONTROL_REPORT.md`.

Дата: 2026-08-27. Цель: автоматическое производство 10–15 разных вертикальных
видео в день с качеством не ниже двух референсов и feedback loop по просмотрам.

## Доказано

- Исполняемый SQLite control plane и русский topic-review workflow.
- Три versioned Topic Packs и role prompts.
- Fail-closed схемы и политики facts/rights/QC/publish.
- Reference profiles, preflight и hard-gated quality score.
- Детерминированная дедупликация title/hook/message/source URL.
- Capacity screen для трёх дневных волн.
- 72-часовой performance evaluator с comparable cohorts и policy veto.
- Verified pre-production одного пилота; 74 секунды, 145 слов, 11 планов,
  целостные claim/asset references.
- 31 unit/integration test проходит.

## Частично

- `начинаем` создаёт review jobs, но пока импортирует подготовленный JSON вместо
  запуска live scout/research workers.
- Canonical JSON Schemas существуют, но runtime ещё не валидирует каждый
  artifact против полного контракта.
- Rights gate существует, но production manifest ещё не содержит локальные
  файлы, hashes и receipts.
- Capacity plan для 15 роликов feasible при заданных assumptions, но cycle time
  не измерен и throughput не доказан.
- Performance logic реализована, но реальных platform snapshots и когорт ещё нет.

## Отсутствует

- Утверждённый HyperFrames brief и хотя бы один итоговый MP4.
- Скачанные и замороженные assets, TTS, captions, audio master, contact sheet и
  реальный QCReport.
- Runtime agent runner для scout/research/rights/script/editor с очередями и WIP.
- Полная artifact-versioning state machine с downstream invalidation.
- 15-job end-to-end/soak test и измеренные cycle time, cost, attrition и recovery.
- Human-gated publish connectors и реальные 1/6/24/72/168-hour metrics.

## Следующий критический путь

1. Получить одобрение темы пилота.
2. Закрыть file-level права, скачать и захешировать production assets.
3. Собрать TTS/captions/HyperFrames composition и MP4.
4. Выпустить contact sheet, QCReport и human quality decision.
5. Повторить pipeline на малом batch, затем на 15 заданиях с измерением времени.

Ни один unit-тест, план или preproduction dossier не считается доказательством
пропускной способности или просмотров. Этими доказательствами могут быть только
реальные E2E артефакты, замеры и platform metrics.
