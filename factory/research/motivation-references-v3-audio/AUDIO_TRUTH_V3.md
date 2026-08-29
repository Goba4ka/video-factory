# AUDIO TRUTH V3 — 10 motivation references

Дата аудита: 2026-08-28  
Scope: независимый read-only аудит 10 пользовательских MP4 из `C:\Users\ns277\Downloads`; V3-композиция и её исходники не изменялись.

## Контрольный вывод

- Лучший **референсный чистый bed** — **Ref 04**: широкий, ровный, хорошо читаемый пульс около 97.5 BPM, без видимого говорящего и без признаков речи в просмотре. Подходит для быстрого спортивного монтажа.
- Второй пригодный bed — **Ref 01**: тёмный low-heavy фон около 70.8 BPM, достаточно длинный, но почти моно. Подходит для депрессивной мотивации и фото/текстового монтажа.
- **Ref 10** также выглядит чистым, но это лишь 6.7 с почти статичного суб-басового drone. Годится как интро/текстура, не как самостоятельная музыка 25–35-секундного ролика.
- Ref 02/03/05/06/07/08/09 содержат речь или кинодиалог; их нельзя честно считать чистыми музыкальными stems. Source separation в этом аудите не применялась, поэтому «чистый bed» из них не имитировался.
- Для production-safe версии предпочтительнее лицензированная альтернатива **Scott Buckley — Nightfall**. Она ближе к Ref 01 по тёмному низкочастотному настроению и оставляет больше места для речи.
- Основной subtitle truth для разговорного формата — **не длинные двухстрочные предложения**, а отдельные word/phrase cues по 1–4 слова, обычно 0.48–0.95 с. Акцент — максимум одно слово в cue.

## Метод и ограничения

Аудио каждого MP4 извлечено в PCM 24-bit / 48 kHz stereo. Измерения выполнены детерминированно локальными FFmpeg/NumPy-процедурами:

- integrated loudness, true peak и LRA — FFmpeg `loudnorm` analysis pass;
- crest factor, RMS-window dynamics, stereo mid/side и correlation;
- onset spacing, спектральный flux и ориентировочный BPM;
- low/vocal/air-band energy, spectral centroid и speech-like modulation;
- 1 fps и 6-frame contact sheets для визуальной проверки наличия речи, типа монтажа и ритма титров.

Важно: BPM у речевых или кинодиалоговых дорожек — лишь численный кандидат, а не музыкальная истина. В таблице он отмечен как ненадёжный там, где onset-структуру формирует голос, монтаж или SFX. Метрика vocal-band сама по себе не доказывает наличие голоса; окончательная классификация сделана совместно по аудио и кадрам.

Полные машинные метрики: `metrics/reference_audio_metrics.json`.

## Сводка 10/10

| Ref | Исходный файл | Длина | LUFS-I | TP dBTP | LRA | BPM / надёжность | Stereo S/M | Наблюдаемая структура | Решение по bed |
|---:|---|---:|---:|---:|---:|---|---:|---|---|
| 01 | `ogjb1N3eMRef0MRYAIXAIvjN1D8uTHLgQ2AFIj.mp4` | 41.22 s | -18.59 | -5.60 | 1.5 | 70.79 / высокая | -24.91 dB | Текст/фото, ровный dark bass, без спикера | **Да; кандидат #1**, почти mono |
| 02 | `owGEDV4mqIA3hC0XROegufgW4QE2nBD6qQYRoF.mp4` | 26.82 s | -18.67 | -0.95 | 8.0 | 56.17 / низкая | -29.92 dB | Arsen, речь доминирует; большой динамический разброс | Нет: voice-contaminated |
| 03 | `okDojoGYsNMeJBU1YfTfMLjIQ9OLIIUYGEgAjw.mp4` | 38.25 s | -18.93 | -2.24 | 12.4 | 184.57 / низкая | -13.56 dB | Кинодиалог + score/SFX, очень высокая LRA | Нет: dialogue/score mix |
| 04 | `o0DOqXEEIDQqX6hfCQFmHnumfB7VYEAxDBzRQ7.mp4` | 16.05 s | -20.32 | -10.84 | 0.8 | 97.51 / хорошая | -11.41 dB | Sports montage, ровный широкий pulse, без спикера | **Да; кандидат #2 и лучший ref-bed** |
| 05 | `oYiwFBE1RAhKBRNQi64iWIWAzKMfEmRlC2qoIB.mp4` | 60.68 s | -11.66 | +0.54 | 3.7 | 161.50 / низкая | -22.30 dB | Multi-speaker montage, агрессивный master | Нет: речь + clipping |
| 06 | `oQM4Q3BqBg0IPERE1E5jfHFAN7jDhFmnvcjeME.mp4` | 16.37 s | -7.98 | **+1.39** | 1.6 | 95.70 / низкая | -17.89 dB | Несколько спикеров, word captions, сильно crushed | Нет: речь + сильный clipping |
| 07 | `o0I1EQLyjVjaAkMRfXCZ8uFfzpeIDQBAIhqbDd.mp4` | 44.79 s | -16.10 | -1.58 | 4.6 | 136.00 / низкая | -17.48 dB | Athlete monologue, B&W/scanline | Нет: voice-contaminated |
| 08 | `oATPD9oIS8GRzQUgIGLqAe4GAcYeANXjefDRyA.mp4` | 21.06 s | -23.06 | -9.43 | 4.9 | 50.67 / низкая | -27.43 dB | Goggins close-up, voice-dominant | Нет: voice-contaminated |
| 09 | `ooehMGeIQAeept0QEQFVGgAKNcgsLARQGMnwC2.mp4` | 25.80 s | -14.05 | -1.15 | 2.5 | 112.35 / низкая | -24.19 dB | Markaryan monologue, узкий/limited mix | Нет: voice-contaminated |
| 10 | `owgeQKD3jYGHAGMTjWIr8LfrcADIdQ3AofmEC0.mp4` | 6.71 s | -16.90 | -5.86 | 1.2 | 120.19 / ненадёжная | -21.06 dB | Text-only, low drone/sub-bass, слишком короткий | Reserve texture only |

Дополнительные диагностические признаки:

- Ref 01: onset median 0.279 s, CV 0.407, low-band ratio 0.531, centroid 276 Hz, late-vs-early +0.46 dB.
- Ref 04: onset median 0.255 s, CV 0.448, low-band ratio 0.650, centroid 308 Hz, late-vs-early +0.82 dB.
- Ref 05 и 06 имеют true peak выше 0 dBTP. Их мастер нельзя использовать как loudness target.
- Ref 02/03 имеют LRA 8.0/12.4 LU, что согласуется с голосовой/кинематографической динамикой, а не с готовым ровным bed.

## Локальные music-bed кандидаты

Все четыре монтажных WAV имеют длину **28.370 s**, формат PCM 24-bit / 48 kHz stereo, полностью декодируются без ошибок.

### A. Референсные — только тест/разрешение владельца

#### 1. Ref 01 — Dark Bass 70 BPM

- Файл: `candidates/reference/reference-bed-01-dark-bass-70bpm.wav`
- Media OS: `.media/audio/bgm/bgm_001.wav`
- Source: Ref 01, участок 00:00.000–00:28.370, fade-in 0.20 s, fade-out 0.35 s.
- Cut metrics: -18.85 LUFS-I, -5.60 dBTP, LRA 1.2, 70.79 BPM, side/mid -25.98 dB.
- Starting gain для речи при bed target около -23 LUFS: **-4.15 dB** (`0.620`).
- Rights: **permission_required**. Только внутренний тест до подтверждения прав.
- SHA-256: `B3EC8889EE72046A7AB3BCDE656A487A13C2AB809271A74A01B38170827E5D22`.

#### 2. Ref 04 — Wide Pulse 97 BPM Loop

- Файл: `candidates/reference/reference-bed-04-wide-pulse-97bpm-loop.wav`
- Media OS: `.media/audio/bgm/bgm_002.wav`
- Source: Ref 04; два прохода с 1.20 s crossfade, затем финальный fade-out.
- Cut metrics: -20.42 LUFS-I, -10.84 dBTP, LRA 0.9, 97.51 BPM, side/mid -11.67 dB.
- Starting gain для речи: **-2.58 dB** (`0.743`).
- Rights: **permission_required**. Только внутренний тест до подтверждения прав.
- SHA-256: `9C55285B98295DAEF6A3D1584B8676B7F29F229834665AC36B19B7392457D70A`.

### B. Лицензированные альтернативы — CC BY 4.0

Лицензия/условия автора: [Using This Music — Scott Buckley](https://www.scottbuckley.com.au/library/using-this-music/). На текущей странице указана CC BY 4.0: коммерческое использование разрешено при корректной атрибуции; нельзя переиздавать музыку как standalone-продукт или заявлять её в Content ID. Атрибуция должна быть в описании ролика.

#### 3. Scott Buckley — Nightfall, cut 135.00–163.37

- Файл: `candidates/licensed/licensed-nightfall-135.00-163.37.wav`
- Media OS: `.media/audio/bgm/bgm_003.wav`
- Страница трека: [Nightfall](https://www.scottbuckley.com.au/library/nightfall/)
- Полный локальный MP3: `candidates/licensed/scott-buckley-nightfall.mp3`
- Cut metrics: -14.47 LUFS-I, -0.96 dBTP, LRA 1.5, 79.51 BPM, side/mid -8.30 dB.
- Starting gain для речи: **-8.53 dB** (`0.375`).
- Почему похож: тёмный atmospheric hybrid, низкий центр тяжести и драматический backend; функционально ближе всего к Ref 01, но заметно шире.
- Required credit: **`'Nightfall' by Scott Buckley - released under CC-BY 4.0. www.scottbuckley.com.au`**
- SHA-256: `B07CC006372A087D389F9317CF4061F3B8E613C84F835302457DC96A1AA90A04`.

#### 4. Scott Buckley — Resonance, cut 180.00–208.37

- Файл: `candidates/licensed/licensed-resonance-180.00-208.37.wav`
- Media OS: `.media/audio/bgm/bgm_004.wav`
- Страница трека: [Resonance](https://www.scottbuckley.com.au/library/resonance/)
- Полный локальный MP3: `candidates/licensed/scott-buckley-resonance.mp3`
- Cut metrics: -11.56 LUFS-I, -0.18 dBTP, LRA 2.3, 50.17 BPM half-time / ~100.34 BPM perceived double-time, side/mid -2.16 dB.
- Starting gain для речи: **-11.44 dB** (`0.268`).
- Почему похож: dark electronica с явным pulse и driving synth backend; по perceived double-time близок к Ref 04.
- Required credit: **`'Resonance' by Scott Buckley - released under CC-BY 4.0. www.scottbuckley.com.au`**
- SHA-256: `12957BF3F5F653978A586D2EB8DD84722ADB1FA643092C14B00D853504176B64`.

## Speech/music relationship для следующего микса

Для ролика со спикером:

1. Голос — якорь: нормализовать dialog bus примерно к **-16…-14 LUFS-I**, true peak после лимитера **не выше -1.0 dBTP**.
2. Bed до ducking — ориентир **-24…-22 LUFS-I**. Указанные выше starting gains приводят кандидатов примерно к -23 LUFS.
3. На music bus сделать speech carve, а не только общий volume duck:
   - broad cut 1.5–4.0 kHz на 2–4 dB во время речи;
   - optional low-mid cut 180–350 Hz на 1–2 dB, если мужской голос мутнеет;
   - attack 25–45 ms, release 350–450 ms, duck depth обычно 3–6 dB.
4. Не качать музыку по каждому слову. Медленный release сохраняет «дорогую» плотность и не выдаёт компрессор.
5. В паузах длиннее 0.45 s разрешить возврат музыки на 2–3 dB. В финальных 1.5–2.0 s без речи — вернуть bed к nominal и дать короткий audio resolve.
6. Финальный master target для social short: около **-14 LUFS-I**, true peak **≤ -1.0 dBTP**. Не копировать перегруз Ref 05/06.

Для text-only ролика без речи music bed может быть громче: около **-16…-14 LUFS-I**, при условии TP ≤ -1.0 dBTP и отсутствия слышимого pumping.

## Subtitle timing truth

### Что фактически делают референсы

| Ref | Тип титров | Наблюдаемый ритм |
|---:|---|---|
| 01 | Текстовый монтаж, прогрессивные фразы | Ступени по 1–3 s; фраза собирается/заменяется частями. Контраст serif/condensed, белый + красный/жёлтый акцент. |
| 02 | Статичный lower-third | Имя/рубрика почти без речевой синхронизации; не брать как timing template. |
| 03 | Кинематографические phrase cards | 2–5 слов, обычно 1.5–3 s; есть длинные участки вообще без текста. |
| 04 | Статичный английский тезис | Одна строка на всём ролике; не речевые субтитры. |
| 05 | English multi-speaker word captions | Почти непрерывная смена; примерно 1–4 слова/cue, чаще 0.45–1.0 s. |
| 06 | Russian word-swap, несколько спикеров | В основном одно слово в центре; около 0.45–0.85 s, иногда короткая пауза на cut. |
| 07 | Russian athlete monologue | 1–4 слова, чаще 0.65–1.15 s; иногда 2–3 компактные строки для смыслового удара. |
| 08 | Russian Goggins lower captions | 1–3 слова, чаще 0.7–1.2 s, неброско и близко к нижней safe-area. |
| 09 | Russian Markaryan/glitch | Одно слово или пара слов примерно 0.55–1.0 s; одно цветное слово/underline, не continuous karaoke. |
| 10 | Text-only static sentence | Почти одна фраза на 6.7 s; не подходит как speech timing template. |

### Рекомендуемая production-схема

- Default cue: **1–4 слова**, 0.48–0.95 s.
- Сильное одиночное слово: **0.30–0.60 s**.
- Короткая фраза 2–4 слова: **0.70–1.25 s**.
- Cue входит на **40–70 ms раньше** слышимого onset и уходит через **70–120 ms после** последней фонемы.
- Между hard-swap cues оставлять 20–40 ms, но не допускать визуального overlap.
- Не более 2 строк; идеал для русского — 11–16 знаков на строку, жёсткий максимум около 20.
- Для talking-head: baseline примерно 300–430 px от нижнего края 1080×1920, но каждый shot проверять на лицо/руки/UI-safe area.
- Не больше одного accent word в cue. Акцент лучше muted gold/brass или тёмно-красный; не красить всю строку.
- Отдельные ASS `Dialogue` events на слова/короткие фразы надёжнее, чем сложное nested karaoke. Это даёт seek-safe hard swaps и проще чинится по waveform.
- Для Ref 01-like text montage использовать другой preset: 1–3 s phrase blocks, максимум 5–7 слов, иногда progressive build. Не применять его к спикеру.

### Рекомендуемый ASS preset, 1080×1920

```ass
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: WordBase,Montserrat ExtraBold,82,&H00F2F2F2,&H00F2F2F2,&H00080808,&H64000000,-1,0,0,0,100,100,0.6,0,1,4.2,1.8,2,90,90,350,204
Style: WordAccent,Montserrat ExtraBold,86,&H0078B9D9,&H0078B9D9,&H00080808,&H64000000,-1,0,0,0,100,100,0.4,0,1,4.4,2.0,2,90,90,350,204
Style: Phrase,Oswald SemiBold,72,&H00F2F2F2,&H00F2F2F2,&H00080808,&H50000000,-1,0,0,0,100,100,0.3,0,1,3.8,1.5,2,100,100,340,204

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,0:00:00.95,0:00:01.58,WordBase,,0,0,0,,{\fad(55,85)\blur0.6}ТЫ НЕ УСТАЛ
Dialogue: 1,0:00:01.61,0:00:02.08,WordAccent,,0,0,0,,{\fad(45,80)\blur0.5}ТЫ СДАЛСЯ
```

Цвета ASS записаны в BGR. `&H0078B9D9&` — приглушённый тёплый gold в ASS-порядке. Если Montserrat/Oswald не установлены в render environment, шрифт нужно заморозить рядом с проектом или заменить на имеющийся тяжёлый condensed grotesque; полагаться на silent font fallback нельзя.

## Rights gate

- Два файла в `candidates/reference/` — производные от пользовательских референсов. Они помечены **permission_required** и не должны публиковаться без подтверждения прав на исходники и музыку.
- Два файла в `candidates/licensed/` можно использовать по CC BY 4.0 только при выполнении текущих условий автора и обязательной атрибуции. Перед массовой публикацией зафиксировать snapshot/текст лицензии в production ledger.
- Аудит не подтверждает права на лица, интервью, киноматериал или speech clips внутри 10 референсов.

## Контроль целостности

- Все 4 candidate WAV: 28.370 s, PCM24/48 kHz stereo.
- FFmpeg full-decode: 0 ошибок.
- Машинный ledger с provenance, gains и SHA-256: `CANDIDATE_LEDGER_V3.json`.
- Исходники V3 в рамках этого аудита не редактировались.
