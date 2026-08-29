# STATUS — lane «Мотивация»

Дата проверки: 2026-08-30

Статус пакета: **ARMED — WAITING FOR «НАЧИНАЕМ»**

Production-ready источники речи: **0 — все найденные публичные источники требуют clearance**

## Формат линии

Контракт приведён к пользовательским референсам:

- оригинальная речь реального спикера; Fish/TTS/voice clone запрещены;
- по умолчанию русскоязычный спикер; английский только с exact RU translation и bilingual human review;
- крупный speaker-first кадр без боковых надписей и рамок;
- русские word/phrase captions по 1–4 слова с human timing pass;
- dark/depressive-motivational grade;
- source audio + отдельно лицензированный BGM; B-roll audio muted.

Измеренная опора — `factory/analysis/motivation-refpack-20260828/metrics.json` и
`factory/research/motivation-references-v3-audio/AUDIO_TRUTH_V3.md`: длинные референсы имеют
39.934–73.800 с, медиану 51.267 с и две pacing-семьи — long-hold speaker
(13.31–14.58 с на план) и fast montage (2.67–3.68 с на план).

## Текущая поддержка DAG

| Формат | Статус |
|---|---|
| Один русский спикер | Поддерживается после exact transcript + human rights clearance |
| Один английский спикер, русские субтитры | Поддерживается после rights + bilingual translation review |
| 2–6 спикеров/речевых сегментов | Поддерживается `SourceAudioManifest` 1.1 после отдельных rights/transcript проверок каждого сегмента |
| Текст/фото только под музыку | **DISABLED**: motivation DAG требует original source speech/transcript |

Multi-speaker нельзя имитировать скрытым premix. Каждый из 2–6 сегментов связан с точным
`asset_id`, исходными байтами, таймкодами, спикером, transcript, rights evidence и retained PCM WAV.
Итоговый program WAV проверяется как точная порядковая конкатенация этих сегментов. Для английского
сегмента дополнительно обязательны original transcript, точный русский экранный текст и checksum-bound
bilingual human review.

## Candidate pool

`candidate_pool.json` содержит 20 research candidates:

- 10 русскоязычных single-speaker leads;
- 6 англоязычных single-speaker leads с зафиксированными RU translation requirements;
- 4 multi-speaker редакционные дуги, технически допустимые только после per-segment transcript/rights гейтов.

Ни один кандидат не имеет статуса `approved`. Официальный/verified/public URL подтверждает происхождение,
но не даёт прав скачать, извлечь речь, crop, субтитровать, перемонтировать или публиковать.

## Канонический запуск

После команды `начинаем` линия заново проверяет `TOPIC_PACK.json`, `EDITORIAL_PLAYBOOK.md`,
`SOURCE_POLICY.md`, `SAFETY.md`, источники, transcript, перевод и права, затем показывает slate из 3–5
вариантов. В production queue разрешены single-speaker и 2–6-segment multi-speaker форматы с явным human
rights approval точного RightsManifest SHA-256 и всех asset IDs; для multi-source дополнительно проверяются
порядок, диапазоны, байты и transcript/review каждого сегмента.

Канонический DAG остаётся заданным в `OPERATING_MODE.json` и registry. Human rights, preview,
final-review и отдельное разрешение публикации остаются fail-closed.
