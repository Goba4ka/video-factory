# Политика источников lane «Мотивация»

## 1. Два независимых решения

Для speech clip отдельно проверяются:

1. **Редакционная точность** — кто говорит, что именно произнесено, в каком контексте и не изменил ли монтаж смысл.
2. **Право использования** — кто владеет записью, речью/исполнением и вложенной музыкой; разрешены ли extraction, crop, субтитры, remix, коммерческое использование и целевые платформы.

Первичный URL может доказать точность и всё равно не дать прав. Пока оба решения не `PASS`, источник остаётся research-only: его нельзя автоматически скачивать, извлекать, замораживать в production, монтировать или публиковать.

## 2. Приоритет speech sources

### Green — может идти к human rights approval

- owned запись с подписанным talent/model release;
- commissioned запись, где договор явно передаёт коммерческие права на монтаж, crop, субтитры, музыку и платформы;
- master, предоставленный правообладателем с письменным разрешением на точный excerpt;
- коммерчески лицензированный архив интервью/выступления с разрешёнными derivatives;
- item-level CC license, разрешающая коммерческую переработку, после проверки автора, всех сторонних элементов и условий атрибуции.

### Yellow — только исследование и запрос прав

- оригинальное интервью на официальном канале спикера или шоу;
- официальный event/university/sports channel;
- verified YouTube, TikTok или Instagram account;
- пресс-кит без явных video reuse terms;
- пользовательский MP4, если пользователь не подтвердил право выдавать лицензию на содержащуюся речь, изображение и музыку.

`Public`, `official`, `verified`, embed и техническая скачиваемость не являются лицензией.

### Red — не использовать

- мотивационный repost/compilation account без цепочки прав;
- скачанный social clip, фильм, сериал, спортивная трансляция или подкаст без разрешения;
- private/deleted source, который нельзя заново проверить;
- «no copyright», «royalty free» или `fair use` как единственное основание;
- `NC` в монетизируемом проекте, `ND` для crop/subtitle/remix;
- источник с неизвестным спикером, правообладателем или встроенной музыкой.

## 3. Source-of-truth для точной речи

На каждый одноголосый кандидат сохраняются:

- `speaker_name` и язык;
- официальный landing URL, publisher/rightsholder, title и publication date;
- точные `source_in` / `source_out` до кадра;
- минимум 10–20 секунд контекста до и после окна для внутренней проверки;
- original transcript без стилистического «улучшения»;
- human listen result и пометки спорных слов;
- SHA-256 source master, extracted PCM и transcript;
- явное решение: `verbatim`, `editorial_paraphrase` или `blocked`.

ASR используется как черновик тайминга. Он не заменяет человеческое прослушивание окончаний, отрицаний, имён и адресата.

Для русского спикера captions повторяют слышимую русскую речь. Сокращённая экранная формулировка, не являющаяся дословной, не берётся в кавычки и не приписывается человеку.

## 4. Английский источник и русский перевод

Английский спикер допустим только при выполнении всех условий:

1. original English transcript сверён по аудио;
2. русский перевод сохраняет отрицание, адресата, модальность, причинность и степень уверенности;
3. original и RU строки связаны с одним и тем же source window;
4. bilingual human reviewer утверждает перевод до caption burn-in;
5. cue timing соответствует английской реплике, а не опережает ещё не произнесённый смысл;
6. delivery captions полностью русские.

Внутри ledger перевод помечается `перевод редакции`. Нельзя собирать одну русскую «цитату» из нескольких английских предложений, менять «может» на «должен», удалять оговорку или усиливать обещание.

## 5. Монтажные операции над речью

Разрешены только операции, покрытые правами и не меняющие смысл:

- точный start/end trim;
- удаление длинной технической паузы, кашля или сбоя с сохранением последовательности;
- loudness normalization, EQ, de-noise и безопасный limiter;
- 9:16 reframe, цвет, субтитры и отдельно лицензированная музыка.

Запрещены:

- перестановка слов/предложений;
- соединение фрагментов так, будто это одна непрерывная цитата;
- удаление отрицания, условия или оговорки;
- AI voice clone, Fish Audio, TTS, speech-to-speech и синтетическое продолжение;
- lip-sync или изображение, создающее впечатление, что человек сказал чужой текст.

## 6. Multi-speaker: обязательные per-segment bindings

`SourceAudioManifest` 1.1 поддерживает 2–6 отдельных speech segments. Каждый segment обязан иметь собственные:

- `asset_id` и точно один item в `RightsManifest`/`FrozenMediaManifest`;
- source path/hash и source in/out;
- имя спикера, source language, original transcript и русский delivery transcript;
- rights status/evidence и retained extracted WAV hash;
- program in/out в порядке склейки.

Для `source_language=en` разрешение требует checksum-bound human bilingual review, связанный с exact asset,
range, original transcript hash и Russian transcript hash. Для `ru` original и delivery transcript совпадают.

Общий program WAV пишется детерминированно и при каждой queue/QC/release проверке должен быть exact PCM
конкатенацией retained segment WAVs. Скрытый неучтённый premix или подмена порядка блокирует production.
Любое изменение segment bindings или байтов после successful task обнаруживается до release.

## 7. B-roll и изображение спикера

Source video спикера проходит rights review и используется как главный визуальный слой. Для B-roll приоритет:

1. собственная съёмка с релизами;
2. commercial stock с сохранённым invoice/license receipt;
3. CC0/CC BY, допускающая коммерческий remix, с item-level snapshot;
4. written-permission asset от правообладателя.

Pexels или другой stock URL проверяется на дату ingest; автор, точная landing page, license snapshot, реальное разрешение, локальный hash и timeline usage сохраняются. Лицензия stock не разрешает ложный endorsement или унизительное использование узнаваемого человека.

Нельзя закрывать спикера случайным luxury/success B-roll. В single-speaker формате B-roll занимает не более 25% таймлайна, его исходный звук выключен.

## 8. Музыка

Музыка — отдельный asset, а не звук, «вытащенный» из референса или чужой речи.

Разрешены:

- заказанный original bed с переданными коммерческими правами;
- commercial library track с разрешёнными social/client/monetization terms;
- item-level CC BY/CC0 track с текущим license snapshot и полной атрибуцией.

Для каждого bed сохраняются landing page, автор, track title, license/receipt, required credit, retrieved_at, локальный SHA-256 и точный timeline cut. Вложенная в source clip музыка отдельно очищается либо удаляется настолько, насколько это разрешено и технически честно.

`AUDIO_TRUTH_V3.md` фиксирует, что ref-derived beds 01/04 требуют разрешения. Кандидаты Scott Buckley могут использоваться только после повторной проверки актуальной CC BY 4.0 страницы, сохранения snapshot и точной атрибуции; сам исследовательский документ не является license receipt.

## 9. Human rights gate

До freeze атрибутированный человек проверяет точный canonical `RightsManifest` SHA-256 и полный список `asset_id`:

- speech/source video;
- likeness/talent release, если применимо;
- вложенную музыку/SFX;
- B-roll;
- отдельный BGM;
- шрифты, фотографии, логотипы и графические элементы.

Approval содержит имя, timestamp, note, manifest hash и все reviewed asset IDs. Любая замена файла, новый crop source, иной speech interval или другой music cut меняет evidence и требует нового решения.

## 10. Evidence, использованное для lane presets

- `factory/analysis/motivation-refpack-20260828/metrics.json` — измеренная длительность 39.934–73.800 с и две семьи pacing.
- `factory/research/motivation-references-v3-audio/AUDIO_TRUTH_V3.md` — speech/music classification, loudness, subtitle rhythm и rights status beds.
- `factory/research/motivation-ru-source-packet-20260828.md` — семь русскоязычных первичных source leads; все `permission_required`.
- `factory/research/motivation-web-source-packet-20260828.md` — английские source leads, exact excerpt transcripts/translations и запрет считать официальный URL лицензией.
- `factory/research/russian-motivation-1/SOURCE.md` и `factory/research/russian-motivation-2/SOURCE.md` — два локально проверенных русскоязычных source windows; оба `permission_required`.

Эти файлы доказывают исследовательскую и техническую пригодность, но не commercial reuse rights.

## 11. Финальная проверка источника

- [ ] Спикер и первичный publisher установлены.
- [ ] Есть exact timecodes, контекст, human-checked original transcript и hashes.
- [ ] Для английского есть exact RU translation и bilingual human approval.
- [ ] Rights basis разрешает extraction, 9:16 crop, subtitles, BGM, derivatives, commercial use и целевые platforms.
- [ ] Source master получен разрешённым способом; публичная платформа не выдана за download permission.
- [ ] Вложенная музыка очищена либо отсутствует.
- [ ] B-roll, BGM, likeness, fonts и graphics имеют отдельные evidence entries.
- [ ] Multi-speaker кандидат не попал в production через single-source контракт.
- [ ] Fish/TTS/voice clone отсутствуют.
- [ ] Точный RightsManifest и все asset IDs одобрены человеком.
