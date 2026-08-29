# Video factory control plane

> Runtime V2 (heartbeat workers, DLQ/rework, outbox, analytics):
> [START_HERE.md](./START_HERE.md) и
> [deployment/SERVER_MIGRATION_GUIDE.md](./deployment/SERVER_MIGRATION_GUIDE.md).

> Актуальный вход в готовую пятилинейную систему: [START_HERE.md](./START_HERE.md). Контрольный результат и честные границы: [CONTROL_REPORT.md](./CONTROL_REPORT.md).

Исполняемый control plane для фабрики коротких видео. Он использует стандартную
библиотеку Python и SQLite; внешние Codex/Fish/HyperFrames процессы подключаются
через ограниченные handlers. `start` импортирует идеи в review batch, а
allowlisted headless workers после одобрения выполняют schema-bound этапы. Плохие
источники, права или safety evidence закрывают гейт; публикация остаётся
человеческим действием.

## Быстрый старт

```powershell
python -m video_factory init --db factory.sqlite3
python -m video_factory start ideas.json --db factory.sqlite3 --batch-size 5
python -m video_factory list --db factory.sqlite3 --entity jobs
python -m video_factory approve job_xxx --db factory.sqlite3
python -m video_factory next job_xxx --db factory.sqlite3 --idempotency-key job_xxx-rights-open
python -m video_factory preflight ../pilots/moon_trees_preproduction --profiles quality/reference_profiles.json
python -m video_factory план-дня examples/day_plan_15.json
python -m video_factory дубли --candidate candidate.json --existing recent_ideas.json
python -m video_factory качество --preflight preflight.json --editorial editorial.json --originality originality.json
python -m video_factory свежесть --lane celebrity_news --checked-at 2026-08-29T09:00:00+03:00
python -m video_factory метрики --candidate snapshot_72h.json --cohort cohort.json
```

## Fish Audio

Для озвучиваемых линий озвучка встроена как этап `script -> voice -> editor`.
Линия `motivation` принципиально использует
`script -> source_audio -> editor`: только исходная речь из лицензированного
видео, без Fish/TTS. Ключ Fish защищён
Windows DPAPI вне проекта; общий SQLite-ledger физически блокирует третий
Fish-запрос для одного `video_id`, даже если производство ведут разные чаты.

```powershell
python -m video_factory fish-voices
python -m video_factory fish-tts --video-id job_000001 --text-file narration.txt --output voice.wav
python -m video_factory fish-tts-status job_000001
```

Полный контракт, аварийные состояния и QA: [design/FISH_AUDIO_RUNTIME.md](./design/FISH_AUDIO_RUNTIME.md).

`preflight` не запускает рендер. Он проверяет целостность claim/asset-ссылок,
таймлайн, число слов, скорость речи, количество и медиану длительности планов
относительно выбранного референсного профиля. В результате отдельно указаны
`ready_for_topic_approval` и блокеры, которые должны быть сняты до рендера.

`план-дня` рассчитывает необходимые кандидаты, ожидаемые одобрения, три волны,
render-slot’ы и человеческие минуты с явными коэффициентами attrition. Это
capacity screen, а не обещание фактической пропускной способности.

`дубли` сравнивает title/hook/message и URL источников с недавними идеями.
Парафразы идут на review, exact/near-exact и повтор источника блокируются.

`качество` объединяет профиль референса, редакционную оценку, права, факты,
caption/technical QC и originality. Числовой score не может переопределить hard
gate. `метрики` сравнивает 72-часовой результат с сопоставимой когортой и не
объявляет победителя при маленькой выборке или policy event.

`свежесть` блокирует публикацию, если последняя проверка фактов старше TTL
линии. Для новостей о знаменитостях TTL по умолчанию равен двум часам.

Русские alias: `начинаем`, `одобрить`, `отклонить`.

Если пакет не установлен, добавьте `factory/src` в `PYTHONPATH` или выполните
из каталога `factory`:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m video_factory --help
```

## Формат идей

Поддерживается JSON-массив либо объект с массивом `ideas`:

```json
{
  "ideas": [
    {
      "id": "ocean-001",
      "title": "Кит 52 герца",
      "topic": "тайны океана",
      "summary": "Фактическая заявка и ссылки остаются в исходном payload",
      "sources": ["https://example.invalid/source"]
    }
  ]
}
```

Обязателен только непустой `title`. Если `id` отсутствует, control plane
создаёт технический детерминированный ID из canonical JSON. Весь объект идеи
сохраняется без дополнения внешними данными. Повторный импорт того же объекта
безопасен; одинаковый `id` с другим payload считается конфликтом.

## State machine

```text
review_pending -> approved -> rights_pending
review_pending -> rejected
rights_pending -> production_pending | rights_failed
rights_failed -> rights_pending
production_pending -> qc_pending
qc_pending -> ready | qc_failed
qc_failed -> qc_pending
```

`approve` и `reject` работают только на review-этапе. `next` делает ровно один
разрешённый переход и требует уникальный `--idempotency-key`. Повтор команды с
тем же ключом и теми же параметрами возвращает сохранённый ответ, не двигая job
второй раз.

### Rights evidence

Для выхода из `rights_pending` нужен `--gate-result pass|fail` и JSON-файл
`--evidence`. Успешное evidence:

```json
{
  "items": [
    {
      "asset": "clip-01.mp4",
      "basis": "licensed",
      "reference": "license-receipt-123"
    }
  ]
}
```

Допустимые `basis`: `licensed`, `public_domain`, `owned`, `permission`,
`creative_commons`. Для `fail` достаточно объекта с непустым `reason`.

### QC evidence

Для выхода из `qc_pending` с результатом `pass` обязательны все проверки:

```json
{
  "checks": {
    "duration": true,
    "aspect_ratio": true,
    "captions": true,
    "audio": true,
    "rights": true
  }
}
```

## JSON и аудит

Все команды печатают стабильный JSON. Опция `--export path.json` атомарно
сохраняет тот же результат в файл. `list --entity audit` и `status ID` дают
историю переходов. Таблицы SQLite: `ideas`, `jobs`, `audit`, `operations`;
последняя хранит idempotency responses.

## Тесты

```powershell
python -m pip install -e factory
python -m pytest factory/tests -q
```
