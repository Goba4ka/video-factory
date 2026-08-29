# Видеофабрика: как запускать

Система настроена на пять независимых закреплённых чатов. Каждый чат — отдельная тематическая линия со своей памятью, источниками, редакционными правилами и safety-гейтом.

## Самый короткий запуск

1. Открой нужный закреплённый чат.
2. Напиши одним сообщением: `начинаем`.
3. Чат даст 3–5 тем с хуком, источниками и риском.
4. Ответь по темам `да` / `нет`.
5. После одобрения headless-воркеры готовят исследование, safety review, права,
   media manifest, сценарий, звук, shotlist и preview-ready HyperFrames project.
   Pexels discovery, rights-bound media/source audio, compiler, preview gate,
   render/QC handlers, пять semantic evidence producers, строгий evidence gate,
   bundled real caption/face observers и human-review handoff реализованы.
   До production cutover остаются provision exact observer models, непустой
   human-approved dedup corpus и live provider/CUDA/GPU acceptance на сервере.
   Любой недостающий факт или право закрывает гейт, а публикация требует человека.

Пять `chat_id` уже закреплены в реестре и изолируют queue pod/артефакты. Команда
`начинаем` в главном чате пока формирует review-first смену, но не доказывает
live-долговременную маршрутизацию пяти внешних чатов: это отдельный server/chat
cutover gate, который нельзя закрыть одним наличием ID в JSON.

## Линии и дневная квота

| Линия | Минимум | Цель | Обязательная спецпроверка |
|---|---:|---:|---|
| История войн | 2 | 3 | sensitivity / источники / архивные права |
| Новости про звёзд | 2 | 3 | privacy / defamation / повторная проверка перед выпуском |
| Мотивация | 2 | 3 | доказательность / права / QC |
| Китайская медицина | 2 | 3 | medical safety |
| Здоровье | 2 | 3 | medical safety |
| **Итого** | **10** | **15** | финальный human publish gate |

## Что происходит после «да»

```text
research → specialized safety review → media_discovery → rights → media → script
         → voice (Fish Audio) / source_audio (motivation only) → editor
         → bgm → audio_mix → compiler
         → preview_review → render → qc_auto_evidence → caption_transcript
         → captions/facts/policy/dedup/visual analyzers → qc_evidence_gate
         → qc → final_review → publisher gate
```

У мотивации отдельный specialized safety review не нужен. Остальные линии получают
`medical_review`, `privacy_review` или `sensitivity_review` из
[реестра](./lanes/registry.json). `privacy_review` и `sensitivity_review` могут
готовиться автономным read-only агентом и обязаны закрыться при неоднозначности;
`medical_review` всегда является атрибутированным human gate и требует указать
квалификацию проверяющего и примечание к решению.

`rights` также не выполняется автономным Codex-воркером. Человек подтверждает
точный SHA-256 RightsManifest, перечисляет все просмотренные `asset_id` и
оставляет примечание; подмена хотя бы одного поля манифеста после решения
блокирует media freeze и весь downstream.

`bgm` никогда не качает музыку из сети. Он фиксирует готовый WAV, нормализует
его к стабильным −14 LUFS / −1.5 dBTP и связывает SHA-256 с точным
RightsManifest, human approval и локальным файлом license evidence. `audio_mix`
ставит этот bed на −9 dB до speech-keyed ducking и сводит authoritative
voice/source audio по записанному FFmpeg recipe; compiler получает только
program mix, а B-roll остаётся без аудио.

Публикация в соцсети намеренно не выполняется без отдельного checksum-bound
подтверждения и подключённых аккаунтов. `final_review` и `publisher` никогда не
запускаются автономным Codex-воркером.

Точный readiness matrix и условие переключения writer находятся в
[SERVER_MIGRATION_GUIDE.md](./deployment/SERVER_MIGRATION_GUIDE.md). Наличие
ручного render wrapper или ранее собранных MP4 не считается доказательством
unattended queue E2E.

## Проверка control plane

Из корня проекта:

```powershell
$env:PYTHONPATH = (Resolve-Path 'factory/src').Path
$factoryPython = 'C:\Users\ns277\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $factoryPython -m video_factory lanes --registry factory/lanes/registry.json
& $factoryPython -m pip install -e factory
& $factoryPython -m pytest factory/tests -q
```

Первая команда должна вернуть `ok: true`, `enabled_lanes: 5`,
`total_candidates: 122` и дневную раскладку `10/15/15`. Тесты всегда запускаются
из корня проекта, чтобы относительные пути реестра разрешались одинаково
локально и на CI/server.

## Где лежат результаты

- Общие настройки: `factory/lanes/registry.json`
- Пакеты линий: `factory/lanes/<lane>/`
- Новые смены: `factory/runs/YYYY-MM-DD/<lane>/`
- Контрольный ролик с Fish Audio: `MOTIVATION_CONTROL_FISH_FINAL.mp4`
- Его проект и доказательства: `factory/pilots/motivation-first-action/`
- Приёмка V2: `factory/analysis/V2_ACCEPTANCE_20260829.md`
- Перенос на сервер: `factory/deployment/SERVER_MIGRATION_GUIDE.md`
- План расширения лицензированных источников: `factory/design/MEDIA_PROVIDER_EXPANSION.md`
- Read-only приёмка реальной производительности 10–15 master-файлов:
  `factory/design/THROUGHPUT_ACCEPTANCE.md`
- Read-only проверка пяти отдельных Codex-сессий по явно переданному индексу и
  rollout evidence: `factory/design/CHAT_TOPOLOGY_AUDIT.md`

## Как держать качество при 10–15 роликах

- На каждый плановый выпуск держать 2–2.5 кандидата; для 15 роликов — около 35 тем.
- Запускать три волны: 5 утром, 5 днём, 5 вечером.
- Не ослаблять factual/rights/safety-гейт ради выполнения плана; заменять заблокированную тему запасной.
- Каждые 72 часа сравнивать удержание первых 3 секунд, средний просмотр, досмотры, репосты, скрытия и исправления только внутри сопоставимой тематики.
- Масштабировать победившие форматы, а не обещать просмотры заранее: просмотры можно измерить только после реальной публикации.
> V2 worker: [design/WORKER_RUNTIME.md](./design/WORKER_RUNTIME.md). Server
> cutover: [deployment/SERVER_MIGRATION_GUIDE.md](./deployment/SERVER_MIGRATION_GUIDE.md).
