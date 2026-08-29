# Automated short-video factory

Рабочая основа фабрики русскоязычных vertical short-form роликов на 10–15
выпусков в день. Репозиторий разделяет control plane, редакционные контракты и
каждый отдельный HyperFrames-проект, чтобы производство можно было масштабировать
без смешивания прав, исходников и рендеров.

## Что уже работает

- SQLite control plane с командами `начинаем`, `одобрить`, `отклонить`, `status`
  и идемпотентными переходами.
- Пять production-линий: история войн, новости знаменитостей, мотивация,
  китайская медицина и здоровье; каждая закреплена за отдельным чатом.
- Контракты для идей, фактов, прав, shotlist, рендера, QC, публикации и метрик.
- Числовые профили качества, восстановленные из двух пользовательских
  референсов.
- Preflight сценария и shotlist до скачивания исходников и рендера.
- Дедупликация идей/хуков/источников, reference-quality score и 72-часовой
  performance evaluator.
- Capacity planner для 10–15 роликов с тремя волнами и явными WIP/attrition
  допущениями.
- Heartbeat headless workers, DLQ/rework, versioned artifacts, publish outbox и
  bounded performance feedback.
- После рендера — обязательные checksum-bound проверки технического качества,
  звука, субтитров, фактов, прав, политики, perceptual dedup и кадрирования;
  отсутствующее или изменённое evidence блокирует финальный QC.
- Контрольный набор из шести master MP4 и шести Telegram-копий; внешняя
  публикация удерживается rights/human gates.

## Быстрый запуск

```powershell
$env:PYTHONPATH = "$PWD\factory\src"
python -m video_factory init --db factory\factory.sqlite3
python -m video_factory начинаем factory\candidates\pilot_round_001.json `
  --db factory\factory.sqlite3 --batch-size 5
python -m video_factory preflight pilots\moon_trees_preproduction `
  --profiles factory\quality\reference_profiles.json
```

## Структура

- `factory/src/video_factory/` — исполняемый control plane.
- `factory/design/` — архитектура, role prompts и Topic Packs.
- `factory/contracts/` — межагентные JSON Schemas.
- `factory/quality/` — измеримая планка качества референсов.
- `factory/music/` — отдельные lane-aware музыкальные пулы; успешные ролики
  дают только reference fingerprints, а production использует exact
  лицензированный WAV с platform/territory/placement-проверкой.
- `factory/design/MEDIA_PROVIDER_EXPANSION.md` — fail-closed план подключения
  архивов, Commons и официальных press-kit источников поверх Pexels.
- `factory/policies/` — права и publish gates.
- `factory/analytics/` — feedback loop для роста просмотров; платформенные
  метрики, окна 1/6/24/72/168 часов и решения `hold/iterate/scale/retire`
  зафиксированы в
  [REACH_OPERATING_MODEL_20260830.md](./factory/analytics/REACH_OPERATING_MODEL_20260830.md).
- `pilots/moon_trees_preproduction/` — факты, ассеты, сценарий и shotlist пилота;
  производство пока намеренно не авторизовано.

## Сервер

Пошаговый перенос, pinned toolchain, systemd workers, preflight, backup и
rollback описаны в
[SERVER_MIGRATION_GUIDE.md](./factory/deployment/SERVER_MIGRATION_GUIDE.md).
Контрольные доказательства и оставшиеся host-level gates — в
[V2_ACCEPTANCE_20260829.md](./factory/analysis/V2_ACCEPTANCE_20260829.md).
Одноразовые входы, которые должен предоставить владелец для real production,
собраны в
[OWNER_INPUTS_TO_GO_LIVE.md](./factory/operations/OWNER_INPUTS_TO_GO_LIVE.md).
