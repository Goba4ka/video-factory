# Контрольный отчёт: Video Factory Runtime 0.6

> **Superseded:** актуальная V2-приёмка (heartbeat workers, schema v6,
> DLQ/rework, outbox, analytics и 15/30 soak) находится в
> [analysis/V2_ACCEPTANCE_20260829.md](./analysis/V2_ACCEPTANCE_20260829.md).

Дата проверки: 2026-08-29.

## Усиление V2 от 29 августа

- Добавлен fail-closed `freshness-gate` с TTL по каждой из пяти линий;
  `celebrity_news` перепроверяется не позднее чем за два часа до публикации.
- Quality score больше не может пройти без freshness и visual provenance.
- Добавлен отдельный originality/provenance gate: простые субтитры, рамки,
  кроп, скорость и цветокоррекция чужого видео не считаются достаточным новым
  вкладом.
- Финальные ролики получают динамический voice carve, микросубтитры,
  семантические punch-in и строгую проверку переходов, геометрии и контраста.
- Корректная проверка из корня: `python -m pip install -e factory`, затем
  `python -m pytest factory/tests -q`.

## Итог

Система переведена с prototype-конфигурации на ресурсно-ограниченный runtime
для пяти постоянных продюсерских чатов. Активная база, кэш, WAL и render scratch
расположены вне OneDrive:

```text
C:\Users\ns277\AppData\Local\VideoFactoryRuntime
├── active-plan.json
├── factory-v3.sqlite3
├── cache\
├── scratch\
└── hyperframes-frames\
```

Автоопределён профиль `balanced`. Применено 19 WIP-лимитов. Тяжёлый render и
FULL QC ограничены одним процессом. План выпуска — три волны `5 + 5 + 5`.

## Исправленные потери времени и ресурсов

| Было | Стало |
|---|---|
| до 5 одновременных render workers | 1 тяжёлый render, явный `--workers 1` |
| HyperFrames auto-worker calibration | штатный wrapper без auto-calibration |
| frames/cache в проекте и temp | общий `hyperframes-frames` вне OneDrive |
| повторный `freeze-media` всегда делал GET | совпавший URL + rights snapshot + SHA даёт 0 HTTP |
| каждый proxy/Telegram кодировался заново | immutable SHA-keyed media cache |
| полный QC на каждом draft | FAST на draft, FULL только на финал |
| повторные decode/loudness checks | SHA + FFmpeg + profile-version cache |
| старые три prototype lanes в day-plan | пять рабочих lanes по 3 видео |
| реестр указывал на setup-чаты | реестр переключён на пять producer-чатов |
| producer-чаты терялись среди задач | все пять закреплены в Codex |
| отсутствовали лимиты voice/source_audio | оба ресурса добавлены в schema-v3 runtime |
| Telegram мог кодироваться большим NVENC-файлом | compact `libx264`; NVENC только для proxy/draft |

## Фактический benchmark

Источник: реальный master
`motivation-v3-monologue-master.mp4`, а не синтетический ролик.

| Операция | Первый запуск | Повтор | Экономия времени |
|---|---:|---:|---:|
| Telegram 720×1280, libx264 | 2.359 s | 0.047 s | 98.0% |
| FAST QC | 0.938 s | 0.015 s | 98.4% |
| FULL QC master | 1.703 s | 0.016 s | 99.1% |

Контрольный FULL QC: `final_technical_pass`, `-14.63 LUFS-I`,
`-1.96 dBTP`, `2.7 LU`. Telegram-файл: 1,004,043 байта; FULL QC Telegram
также прошёл.

FFmpeg перечислял NVENC, но первоначальный 64×64 probe ложно считал его
нерабочим: GTX 1660 SUPER отвергает кадр ниже минимального размера. Probe
исправлен на 256×256; реальное аппаратное кодирование подтверждено. На этом
конкретном master NVENC proxy был на 5.6% быстрее, но в 2.4 раза больше по
размеру. Поэтому runtime применяет его только к временным proxy/draft.

## Проверка целостности

- `121/121` Python tests прошли.
- PowerShell wrapper HyperFrames прошёл parser validation.
- Реестр: пять уникальных production chat IDs.
- Новая runtime DB: SQLite schema version 3.
- Старая `factory/runtime/factory.sqlite3`: schema version 2, не изменялась.
- Проверено реальное повторное скачивание: второй freeze не делает HEAD/GET.
- Проверено повреждение кэша тем же размером: SHA mismatch принудительно
  перестраивает output.
- Cache cleanup остаётся dry-run без явного `--execute`.

## Дисковый резерв

Read-only аудит обнаружил около 1.8 ГиБ консервативно освобождаемого места:
повторные `node_modules`, платформенные копии FFmpeg, точные дубли медиа и
старые диагностики. Ничего из этого не удалено: файлы существующих проектов
сохранены. Runtime 0.6 останавливает дальнейшее размножение через общий кэш.

## Рабочие точки входа

- Описание runtime: `factory/design/FAST_RUNTIME.md`
- Конфигурация пяти линий: `factory/lanes/registry.json`
- HyperFrames wrapper: `factory/tools/render_hyperframes.ps1`
- Runtime planner: `video-factory optimize-runtime --profile auto --target 15 --apply`
- Proxy/delivery cache: `video-factory cache-media`
- QC: `video-factory media-qc`
- Состояние кэша: `video-factory cache-status`

## Честная граница автоматизации

SQLite-очередь управляет jobs, leases, retries, WIP и артефактами. Codex-чаты
остаются интерактивными producer-ячейками: команда `начинаем` запускает
исследование и предложения внутри конкретной линии, а одобрение темы остаётся
человеческим gate. Runtime не публикует ролики и не отправляет их во внешние
сервисы без явной команды.
