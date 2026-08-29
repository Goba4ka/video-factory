# Video Factory V2 — контрольная приёмка

Дата: 2026-08-29. Область проверки: локальный Windows control plane и
переносимый Linux/server contract. Внешняя публикация не выполнялась.

## Результат control-plane проверки

- Последний полный Python suite после server packaging, target-lane scout,
  schema-bound editor и voice worker: `202 passed, 2 skipped, 15 subtests passed`.
- Shadow soak: отдельный headless worker полностью обработал партии 15 и 30
  задач; пустые polls не создали мусорных idempotency-операций.
- Реальный `codex exec` 0.151.0 / `gpt-5.4` / read-only / ephemeral turn создал
  русский 27-секундный `script_package` и прошёл локальную доменную валидацию.
- SHA-256 сценарного артефакта:
  `30967f6245d2f208fb5cbe9082f26ac8f31b139eb09d033e6687b08333452206`.
- Измеренный usage успешного turn: 17,002 input, 1,792 cached input, 888 output,
  202 reasoning output tokens.
- Отдельный isolated-workspace smoke также прошёл: 16,272 input, 1,792 cached,
  39 output и 12 reasoning tokens. Пустой workspace уменьшает файловую
  поверхность, но подтвердил, что основной baseline задаётся runtime-контекстом,
  а не сканированием release; экономию токенов ему не приписываем.
- Ранее собранные шесть master MP4 и шесть Telegram-копий остаются технически
  пройденными; их rights status остаётся `HOLD`, поэтому они не публиковались.

## Проверенные эксплуатационные свойства

1. Worker берёт fenced lease, продлевает heartbeat, дренирует активную задачу
   при shutdown и ограничивает время/размер stdin, stdout и stderr handler.
2. В executor передаётся root-first цепочка только успешных upstream results;
   lease token, payload зависимостей и credential-shaped значения не проходят.
3. Терминальные ошибки образуют durable DLQ; transient retry не меняет inputs;
   rework клонирует downstream DAG и сохраняет old→new lineage.
4. Новая версия upstream-артефакта транзитивно инвалидирует downstream.
5. Codex handler разрешает только research/specialized review/rights/script,
   использует фиксированный контракт и повторно валидирует ответ локально.
6. `final_review`, `publisher` и render не разрешены автономному Codex backend.
7. Publish outbox требует человеческого подтверждения точных render/metadata
   checksums; неизвестный исход delivery не повторяется автоматически.
8. Performance loop строит сопоставимую когорту, выдаёт не более двух
   редакционных рекомендаций и не меняет factual/rights/medical gates.

## Что ещё не принято как unattended video E2E

- автоматический media discovery/freeze из approved rights set;
- motivation `source_audio` queue handler;
- shotlist → HyperFrames project compiler;
- render queue handler, создающий canonical RenderManifest;
- semantic/visual QC и job-state → outbox bridge;
- один реальный health acceptance job без ручных промежуточных файлов;
- один реальный motivation acceptance job с item-level rights.

## Что проверяется только на целевом сервере

- реальный `systemd-analyze verify` и запуск units под Ubuntu;
- NVENC/WebGL render на выбранной GPU и повторный deterministic visual diff;
- restore drill encrypted off-host backup;
- живые OAuth/API лимиты и алерты provider-проекта;
- первые 10 production masters с реальными очищенными правами;
- 1/6/24/72/168-hour platform feedback после подтверждённой публикации.

Корректный статус — **control-plane acceptance passed; unattended MP4 E2E и
production host acceptance pending**. Shadow soak доказывает механику очереди,
но не производство видео. Миллионные просмотры не обещаются:
система измеряет retention/feedback и безопасно масштабирует выигравшие форматы.
