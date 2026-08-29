# Video Factory V2 — контрольная приёмка

Дата исходной приёмки: 2026-08-29; контрольное обновление: 2026-08-30.
Область проверки: локальный Windows control plane и
переносимый Linux/server contract. Внешняя публикация не выполнялась.

## Результат control-plane проверки

- Последний полный Python suite после Pexels discovery, rights-bound media,
  program-audio compiler, восьмикатегорийного evidence DAG,
  review-release bridge, throughput gate и server hardening:
  `376 passed, 4 skipped, 76 subtests passed` за `232.86s`.
- Отдельный read-only throughput acceptance suite:
  `11 passed, 1 skipped`; Windows-пропуск относится только к недоступному в
  текущем окружении тестов праву создать symlink. Gate принимает исключительно
  реальную партию из 10–15 checksum-verified masters с полным DAG/evidence,
  отклоняет simulation/shadow и оставляет `final_review`/`publisher` нетронутыми.
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
- Ранее собранные master/Telegram-копии не считаются release-ready. Повторный
  live FFmpeg-QC трёх client-final V2 MP4 выявил: celebrity `-13.97 LUFS`,
  `LRA 0.9`; motivation A `LRA 0.9`; motivation B `-0.97 dBTP`. Строгий gate
  их не пропускает. Три сжатых V4 preview были технически пересведены и
  отправлены только в Telegram «Избранное»: celebrity `-14.87 LUFS / -2.34
  dBTP`, motivation A `-14.68 / -1.28`, motivation B `-15.00 / -1.04`.
  Это не production masters: rights status остаётся `HOLD`, celebrity Fish-
  голос ранее отклонён пользователем, а человеческая художественная приёмка не
  проведена. Публикация не выполнялась.

## Проверенные эксплуатационные свойства

1. Worker берёт fenced lease, продлевает heartbeat, дренирует активную задачу
   при shutdown и ограничивает время/размер stdin, stdout и stderr handler.
2. В executor передаётся root-first цепочка только успешных upstream results;
   lease token, payload зависимостей и credential-shaped значения не проходят.
3. Терминальные ошибки образуют durable DLQ; transient retry не меняет inputs;
   rework клонирует downstream DAG и сохраняет old→new lineage.
4. Новая версия upstream-артефакта транзитивно инвалидирует downstream.
5. Codex handler разрешает только research, privacy/sensitivity review,
   script и editor, использует фиксированный контракт и повторно валидирует
   ответ локально. `medical_review` и `rights` исключены из автономного
   allowlist и требуют атрибутированного человеческого решения.
6. `final_review`, `publisher` и render не разрешены автономному Codex backend.
7. Publish outbox требует человеческого подтверждения точных render/metadata
   checksums; неизвестный исход delivery не повторяется автоматически.
8. Performance loop строит сопоставимую когорту, выдаёт не более двух
   редакционных рекомендаций и не меняет factual/rights/medical gates.
9. После каждого render обязательная цепочка создаёт техническое/аудио/rights
   evidence, word-level transcript, отдельные captions/facts/policy/dedup/visual
   отчёты и только затем checksum-bound `qc_evidence_bundle`. Queue и release
   bridge отклоняют отсутствующий анализатор, подменённые evidence bytes и stale
   render binding.

## Что уже реализовано, но требует live acceptance

- Pexels discovery использует официальный `/v1/videos/search`, сохраняет
  provider/creator/source URL и передаёт ровно эти данные в RightsManifest.
  Media handler скачивает только URL из принятого RightsManifest, после
  item-level receipt/release checks и только при явно включённом network gate.
  Мок-тесты зелёные, но live credential/provider soak ещё не выполнялся;
- motivation `source_audio`: rights-bound handler, FFmpeg extraction, повторная
  queue-проверка байтов и trusted runtime unit готовы, но реальный
  rights-cleared acceptance job ещё не принят;
- shotlist → HyperFrames → render подключён к DAG через обязательный
  checksum-bound human preview approval. Compiler требует job-bound Fish WAV
  для четырёх линий или rights-cleared source WAV для мотивации, перепроверяет
  фактические байты/SHA-256. Перед compiler отдельные `bgm` и `audio_mix`
  связывают лицензированный музыкальный WAV, локальный receipt, точный
  RightsManifest и human approval; профиль `speech-forward-audible-bgm-v1`
  использует −9 dB pre-gain, speech-keyed ducking и двухпроходную нормализацию
  до −15 LUFS / −1 dBTP. Compiler добавляет ровно один checksum-bound program
  mix и принудительно отключает исходный звук B-roll. Реальный повторяемый
  WebGL/NVENC job на целевом сервере ещё не принят;
- review-release timer открывает очередь read-only и материализует только
  immutable `pending_human_review` bundle/event. Он не принимает финальное
  решение и не публикует материал;
- bundled Faster-Whisper caption observer и OpenCV YuNet face observer
  реализованы и fail-closed закрепляют локальные модели по SHA-256. Локальный
  CPU smoke на реальном русском celebrity master прошёл с вероятностью языка
  `0.9959`; YuNet на пяти реальных кадрах обнаружил `2/1/1/1/1` лиц и не
  назначил speaker в кадре с двумя лицами. CUDA/NVENC acceptance на целевом
  сервере всё ещё не выполнен;
- dedup corpus builder принимает только отдельный human approval, привязанный
  к точным RenderManifest/master bytes, и атомарно обновляет corpus. Сам corpus
  пока нельзя заполнить: доступные masters имеют rights `HOLD` и не получили
  человеческого corpus approval.
- server bootstrap теперь использует двухфазный stage/activate, offline
  wheelhouse с SHA-256, exact systemd allowlist, flock, clean environment и
  автоматический rollback config/units/current. Семь исполняемых отказных
  сценариев прошли локально; реальные Ubuntu/systemd/NSS/ACL/GPU свойства ещё
  требуют live acceptance.

## Что блокирует unattended video E2E

- semantic QC stage и пять evidence producers собраны и fail-closed покрыты
  control-plane тестами. Caption/YuNet adapters уже приняты локальными live
  smoke; для live E2E ещё нужно provision их exact model bytes на целевом
  сервере, проверить CUDA runtime и подготовить непустой corpus только из
  действительно одобренных masters;
- trusted provider/runtime/review systemd units и preflight добавлены, но
  `systemd-analyze verify` и живой запуск возможны только на целевом Ubuntu host;
- один реальный health acceptance job без ручных промежуточных файлов;
- один реальный motivation acceptance job с item-level rights;
- реальная смешанная media-партия по всем пяти линиям, затем 15/30-job soak;
- read-only `throughput-acceptance` должен пройти на этой реальной партии; на
  существующей локальной runtime DB он корректно закрылся с ошибкой
  `database schema version 2 does not match required 6`, не мигрируя и не
  изменяя базу;
- автоматический импорт 1/6/24/72/168-hour метрик платформ и A/B feedback;
- отдельные `chat_id` настроены в реестре, но долговременная работа пяти
  независимых внешних чатов/воркеров ещё не подтверждена live-прогоном.

## Что проверяется только на целевом сервере

- реальный `systemd-analyze verify` и запуск units под Ubuntu;
- NVENC/WebGL render на выбранной GPU и повторный deterministic visual diff;
- restore drill encrypted off-host backup;
- живые OAuth/API лимиты и алерты provider-проекта;
- первые 10 production masters с реальными очищенными правами;
- синхронизация word-level таймингов субтитров с реальной речью;
- 1/6/24/72/168-hour platform feedback после подтверждённой публикации.

Корректный статус — **control-plane acceptance passed; unattended MP4 E2E и
production host acceptance pending**. Shadow soak доказывает механику очереди,
но не производство видео. Миллионные просмотры не обещаются:
система измеряет retention/feedback и безопасно масштабирует выигравшие форматы.
