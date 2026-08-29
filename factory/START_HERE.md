# Видеофабрика: как запускать

Система настроена на пять независимых закреплённых чатов. Каждый чат — отдельная тематическая линия со своей памятью, источниками, редакционными правилами и safety-гейтом.

## Самый короткий запуск

1. Открой нужный закреплённый чат.
2. Напиши одним сообщением: `начинаем`.
3. Чат даст 3–5 тем с хуком, источниками и риском.
4. Ответь по темам `да` / `нет`.
5. После одобрения headless-воркеры готовят исследование, safety review, права,
   сценарий и shotlist; Fish worker умеет job-bound озвучку. Полностью
   unattended media discovery → HyperFrames compiler → render → semantic QC
   пока остаётся P0 до server cutover. Любой недостающий факт или право закрывает
   гейт, а публикация требует человека.

Если написать `начинаем` в главном чате, оркестратор может вести все пять линий как одну смену.

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
research → specialized safety review → rights → script → voice (Fish Audio) / source_audio (motivation only)
         → editor → [P0: media/compiler/render/semantic QC] → final review → publisher gate
```

У мотивации отдельный specialized safety review не нужен. Остальные линии автоматически получают `medical_review`, `privacy_review` или `sensitivity_review` из [реестра](./lanes/registry.json).

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

## Как держать качество при 10–15 роликах

- На каждый плановый выпуск держать 2–2.5 кандидата; для 15 роликов — около 35 тем.
- Запускать три волны: 5 утром, 5 днём, 5 вечером.
- Не ослаблять factual/rights/safety-гейт ради выполнения плана; заменять заблокированную тему запасной.
- Каждые 72 часа сравнивать удержание первых 3 секунд, средний просмотр, досмотры, репосты, скрытия и исправления только внутри сопоставимой тематики.
- Масштабировать победившие форматы, а не обещать просмотры заранее: просмотры можно измерить только после реальной публикации.
> V2 worker: [design/WORKER_RUNTIME.md](./design/WORKER_RUNTIME.md). Server
> cutover: [deployment/SERVER_MIGRATION_GUIDE.md](./deployment/SERVER_MIGRATION_GUIDE.md).
