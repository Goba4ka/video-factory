# Локальный caption observer

`caption-observer` — bundled CLI для роли `caption_transcript`. Он принимает
ровно один JSON-объект по stdin и, только после реального локального inference,
возвращает один русский word-level transcript по stdout. Ошибка, пустая речь,
не-русский язык, неполные тайминги, drift модели или master завершают процесс с
кодом `2` и без JSON в stdout.

## Установка

На build-хосте с разрешённым egress соберите отдельное окружение. Runtime-job
не должен устанавливать Python-пакеты или скачивать модель:

```bash
python3.11 -m venv /opt/video-factory/caption-observer
/opt/video-factory/caption-observer/bin/python -m pip install --upgrade pip
/opt/video-factory/caption-observer/bin/pip install \
  '/opt/video-factory/releases/RELEASE/factory[caption-observer]'
/opt/video-factory/caption-observer/bin/python -c \
  'import importlib.metadata as m; assert m.version("faster-whisper") == "1.2.1"'
```

Перенесите заранее конвертированную multilingual Faster-Whisper модель в
неизменяемый каталог, принадлежащий `root`. В корне модели обязательны
`config.json`, `model.bin`, `tokenizer.json` и `vocabulary.*` (стандартный
Systran snapshot использует `vocabulary.txt`). `preprocessor_config.json`
поддерживается, но не обязателен: Faster-Whisper штатно использует встроенные
параметры feature extractor при его отсутствии. Отсутствующий локальный
`tokenizer.json` запрещён: библиотека
не должна пытаться получить tokenizer из Hugging Face.

```bash
sudo install -d -o root -g video-factory -m 0750 \
  /srv/video-factory/models/faster-whisper-large-v3-ct2
# rsync/copy model bytes from the verified build artifact here
sudo find /srv/video-factory/models/faster-whisper-large-v3-ct2 \
  -type d -exec chmod 0750 {} +
sudo find /srv/video-factory/models/faster-whisper-large-v3-ct2 \
  -type f -exec chmod 0440 {} +

MODEL_SHA=$(
  /opt/video-factory/caption-observer/bin/caption-observer \
    --fingerprint-model \
    /srv/video-factory/models/faster-whisper-large-v3-ct2
)
test "${#MODEL_SHA}" -eq 64
printf '%s\n' "$MODEL_SHA"
```

Запишите путь и полученный SHA-256 tree fingerprint в root-owned
`/etc/video-factory/runtime.env`:

```text
VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE=/opt/video-factory/caption-observer/bin/caption-observer
VIDEO_FACTORY_CAPTION_MODEL_PATH=/srv/video-factory/models/faster-whisper-large-v3-ct2
VIDEO_FACTORY_CAPTION_MODEL_SHA256=<64 lowercase hex chars>
VIDEO_FACTORY_CAPTION_DEVICE=cuda
VIDEO_FACTORY_CAPTION_COMPUTE_TYPE=float16
VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN=0.65
```

`faster-whisper` закреплён на `1.2.1`; другая установленная версия отклоняется.
Адаптер перед загрузкой проверяет байты всего model tree, требует абсолютные
пути без symlink, включает `local_files_only=True`, выставляет offline-флаги и
не передаёт Hugging Face credential. После inference он повторно проверяет
master SHA-256 и отсутствие изменения model files.

## Приёмка

Сначала прогоните адаптер вручную с JSON того же формата, который создаёт
`caption_transcript_handler`, затем один полный queue job на реальном русском
master. Проверьте:

- stdout содержит единственный JSON-объект и ничего больше;
- `language=ru`, `warnings=[]`, `status=completed`;
- каждое слово имеет фактические `start_seconds` и `end_seconds`;
- `engine.name` содержит первые 12 символов model fingerprint;
- отключение/переименование модели, подмена master или non-Russian audio дают
  код `2` и пустой stdout;
- на runtime-хосте нет исходящего обращения к Hugging Face во время job.

Только unit-тесты не закрывают live acceptance: нужны установленная модель,
совместимые CUDA/cuDNN/CTranslate2 библиотеки и русский master с известной
контрольной расшифровкой.
