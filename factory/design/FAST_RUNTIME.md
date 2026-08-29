# Быстрый runtime видеофабрики 0.6

Это штатный режим для производства 10–15 вертикальных видео в пяти отдельных
Codex-чатах. Он оптимизирован под текущий компьютер: 12 логических потоков,
около 16 ГиБ RAM и NVIDIA с 4 ГиБ VRAM.

## Что изменилось

- Активные SQLite/WAL, кэш и временные рендеры вынесены из OneDrive в
  `%LOCALAPPDATA%\VideoFactoryRuntime`.
- Создаётся новая чистая `factory-v3.sqlite3`; старая prototype-база только
  инспектируется и не изменяется.
- Одновременно допускается один тяжёлый HyperFrames/WebGL render. Это убирает
  конкуренцию за 4 ГиБ VRAM и неудачную авто-калибровку нескольких workers.
- Исследование, сценарии и проверки прав остаются параллельными; видео идут
  тремя волнами по одному job из каждой тематической линии.
- Прокси, Telegram-файлы и QC-отчёты адресуются по SHA-256 исходника,
  параметрам, версии FFmpeg и версии профиля. Повторный запуск не декодирует и
  не кодирует тот же контент заново.
- Повторный `freeze-media` сначала проверяет существующий ledger и локальный
  SHA-256. При полном совпадении URL и снимка прав сеть не используется.
- Каждый draft получает дешёвый FAST QC; FULL QC, Telegram-транскод и
  визуальный финальный просмотр выполняются только для утверждённого master.
- Очистка кэша всегда является dry-run, пока оператор явно не передаст
  `--execute`. Существующие проекты, исходники и рендеры автоматически не
  удаляются.

## Штатный запуск

Из корня проекта:

```powershell
$env:PYTHONPATH = (Resolve-Path 'factory/src').Path
$factoryPython = 'C:\Users\ns277\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

& $factoryPython -m video_factory optimize-runtime --profile auto --target 15 --apply
```

Команда делает четыре вещи:

1. Проверяет CPU, RAM, свободный диск, FFmpeg/ffprobe, NVIDIA и расположение runtime.
2. Выбирает `economy`, `balanced` или `throughput`.
3. Сохраняет `active-plan.json` вне OneDrive.
4. Создаёт чистую базу актуальной версии схемы и применяет WIP-лимиты для всех ролей и пяти lanes.

На текущем компьютере ожидаемый профиль — `balanced`:

| Ресурс | Лимит |
|---|---:|
| HyperFrames / WebGL render | 1 |
| FULL QC | 1 |
| параллельные лёгкие media decode | 2 |
| local speech model | 1, не одновременно с render |
| FFmpeg CPU threads на процесс | 4 |
| proxy | максимум 960 px по высоте |
| draft | 720×1280 |
| master | 1080×1920 |
| Telegram | 720×1280 |

NVENC используется для временных proxy/draft, когда важнее освободить CPU.
Telegram delivery кодируется `libx264`: на контрольном ролике файл получился
примерно в 2.7 раза меньше при сопоставимом времени. Режим можно принудительно
перевести на CPU флагом `cache-media --cpu`.

`HYPERFRAMES_WORKERS=1` должен передаваться каждому HyperFrames render явно.
Для обычной склейки footage + ASS + музыка используется FFmpeg; HyperFrames
остаётся для DOM/WebGL, сложной типографики и motion-композиций.

Штатный wrapper уже фиксирует один worker, один concurrent render и общий
content-addressed frame cache вне OneDrive:

```powershell
& factory/tools/render_hyperframes.ps1 -Project factory/pilots/my-video `
  -Output factory/pilots/my-video/renders/master.mp4 -Quality high -Crf 16
```

## Пять отдельных чатов и волны

Источником истины остаётся `factory/lanes/registry.json`. История чата не
является состоянием производства: job, lease, checksum и результаты хранятся
в общей SQLite-очереди и content-addressed artifacts.

Для 15 видео runtime создаёт три волны `5 + 5 + 5`; для 10 видео — `5 + 5`.
В каждой волне максимум один новый job на чат:

1. `war_history`
2. `celebrity_news`
3. `motivation`
4. `chinese_medicine`
5. `health`

Research/rights/script разных jobs выполняются одновременно. Render проходит
через единый тяжёлый слот. Пока он занят, следующий job может исследоваться,
проверять права, готовить сценарий, субтитры и прокси, но не запускать второй
WebGL render или локальную speech-модель.

Старый `prepare-day` использует три prototype-категории и не должен быть
точкой входа для этих пяти линий. Темы создаются внутри закреплённых lane-чатов
по их `TOPIC_PACK`, source policy и свежему веб-исследованию; после одобрения
они попадают в общую очередь через `launch-approved`.

## Кэш медиа

```powershell
& $factoryPython -m video_factory cache-media input.mp4 --mode proxy
& $factoryPython -m video_factory cache-media master.mp4 --mode telegram
& $factoryPython -m video_factory cache-status
```

В ответе `cache_hit: true` означает, что FFmpeg не запускался. Изменение хотя
бы одного байта исходника, профиля, параметра или версии FFmpeg создаёт новый
ключ и новый immutable output.

Проверить объём возможной очистки без удаления:

```powershell
& $factoryPython -m video_factory cache-prune --older-than-days 21
```

Удаление выполняется только после осознанного повторного запуска с `--execute`.

## Двухступенчатый QC

Для каждого draft:

```powershell
& $factoryPython -m video_factory media-qc draft.mp4 --level fast --profile portrait_draft
```

FAST делает ffprobe и один уменьшенный полный decode с black/freeze/silence
детекторами. Он возвращает `draft_pass`, `draft_warn` или `draft_fail` и никогда
не устанавливает `publish_eligible`.

Только для финального master:

```powershell
& $factoryPython -m video_factory media-qc master.mp4 --level full --profile motivation_v3_master
& $factoryPython -m video_factory media-qc telegram.mp4 --level full --profile motivation_v3_telegram
```

FULL дополнительно проверяет точное разрешение, H.264/AAC, yuv420p, 30 fps,
48 kHz, A/V drift, integrated loudness, true peak и LRA. Технический pass не
заменяет ledger прав и финальный человеческий просмотр.

## Что не удалять автоматически

Аудит нашёл примерно 1.8 ГиБ консервативно освобождаемого места: повторные
`node_modules`, платформенные FFmpeg-бинарники, точные дубли ассетов и старые
диагностические рендеры. Они принадлежат существующим проектам, поэтому runtime
0.6 прекращает создавать новые дубли, но не удаляет старые без отдельного
решения владельца.

## Проверка

```powershell
& $factoryPython -m unittest discover -s factory/tests -v
& $factoryPython -m video_factory lanes --registry factory/lanes/registry.json
```

Критерии готовности: все тесты проходят, `enabled_lanes = 5`, лимит render равен
1, runtime находится вне OneDrive, а второй одинаковый `cache-media` и
`media-qc` возвращают cache hit.
