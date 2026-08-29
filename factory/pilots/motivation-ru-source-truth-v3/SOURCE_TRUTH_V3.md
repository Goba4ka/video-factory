# SOURCE TRUTH V3 — Russian motivation

Status: `source_select_complete`  
Date: 2026-08-28  
Scope: source-quality lane only; no montage or composition files were edited.

## Delivery gate

- A: one official-channel monologue, 22.040 s editorial range.
- B: three different Russian-speaking people, 6.500–10.000 s editorial ranges.
- All four local deliverables are H.264/AAC MP4 at 1920×1080.
- Every selected interval was inspected as a contact sheet. The frame remains on the speaker; no B-roll or embedded captions are present inside the selected intervals.
- The speech is strongly center-weighted and no conspicuous stereo music bed was detected. A final human listen is still required before permanent subtitle burn-in.
- All rights states are `permission_required`. A public upload and an official channel do not grant republication rights.

| ID | Speaker | Official source | Source range | Editorial duration | Local file | Verified media | SHA-256 | Rights |
|---|---|---|---:|---:|---|---|---|---|
| A | Ирина Хакамада | [official video `HSmy3qBE3H8`](https://www.youtube.com/watch?v=HSmy3qBE3H8) | `00:05:04.460–00:05:26.500` | 22.040 s | [`clips/A-khakamada-monologue-22.04s-1080p.mp4`](clips/A-khakamada-monologue-22.04s-1080p.mp4) | 1920×1080, 30 fps, H.264; AAC 44.1 kHz stereo; local 22.067 s, 3,121,867 B | `f027fd2fbbea97f7ad1ade177b1d0f9e80f68e3872fa3d8acdce32d201fb8e06` | `permission_required` |
| B1 | Радислав Гандапас | [official video `XGW9v9zASG0`](https://www.youtube.com/watch?v=XGW9v9zASG0) | `00:02:31.060–00:02:39.840` | 8.780 s | [`clips/B1-gandapas-8.78s-1080p.mp4`](clips/B1-gandapas-8.78s-1080p.mp4) | 1920×1080, 25 fps, H.264; AAC 44.1 kHz stereo; local 8.840 s, 1,785,764 B | `c2f80f471c8f058675a67155227f6e665fef21ad9d9d3b8c9650ef6e5a222e15` | `permission_required` |
| B2 | Арсен Маркарян | [official video `LnO_g5YAHHY`](https://www.youtube.com/watch?v=LnO_g5YAHHY) | `00:02:29.600–00:02:36.100` | 6.500 s | [`clips/B2-markaryan-6.50s-1080p.mp4`](clips/B2-markaryan-6.50s-1080p.mp4) | 1920×1080, 25 fps, H.264; AAC 44.1 kHz stereo; local 6.520 s, 1,185,021 B | `c9cad42778380084dba96fd6b3761e2158dc9c78365cb0b1a1ac6368ef23c3c2` | `permission_required` |
| B3 | Алексей Ситников | [official video `fXcYILoHAQY`](https://www.youtube.com/watch?v=fXcYILoHAQY) | `00:13:38.000–00:13:48.000` | 10.000 s | [`clips/B3-sitnikov-10.00s-1080p.mp4`](clips/B3-sitnikov-10.00s-1080p.mp4) | 1920×1080, 25 fps, H.264; AAC 44.1 kHz stereo; local 10.000 s, 940,448 B | `b30979bf923b3fdaa4447822e5120c3df92317b72aa0db9bf319e97f316a841c` | `permission_required` |

The small difference between editorial and local duration on A/B1/B2 is frame-boundary padding from exact HLS section extraction. Use the editorial source ranges as source truth; trim local files to the required montage frame if necessary.

## Transcript and cue truth

Times below are source-video times. For local-file offsets subtract the corresponding source in-point in the table above. Transcription was cross-checked against local `faster-whisper small` output and available Russian auto-captions. `human_listen_before_burn_in_required: true`.

### A — Ирина Хакамада

The complete 22-second passage is longer than the allowed verbatim reproduction limit. Exact, subtitle-safe excerpt (24 words):

> `[310.220–310.260]` Вы `[310.260–310.500]` можете `[310.500–311.160]` находиться `[311.160–311.860]` здесь `[311.860–312.160]` и `[312.160–312.520]` сейчас. `[313.140–313.360]` Вы `[313.360–313.660]` можете `[313.660–313.960]` делать `[313.960–314.400]` всё, `[314.460–314.600]` что `[314.600–314.760]` вы `[314.760–315.100]` хотите. `[315.640–315.860]` Только `[315.860–316.360]` почему? `[317.220–317.460]` Потому `[317.460–317.680]` что `[317.680–317.780]` у `[317.780–317.920]` вас `[317.920–318.320]` есть `[318.320–318.580]` вот `[318.580–318.820]` эта `[318.820–320.140]` внутренняя `[320.140–320.840]` дисциплина.

Semantic map of the full selected passage, without additional verbatim reproduction:

- `304.460–305.700`: time subjectively disappears;
- `307.060–312.520`: freedom to mentally occupy past, future and present viewpoints;
- `313.140–320.840`: the exact excerpt above — agency is linked to inner discipline;
- `321.060–322.580`: discipline is distinguished from coercion;
- `323.080–326.500`: failing agreements transfers the cost to other people.

### B1 — Радислав Гандапас

Exact transcript, 25 words:

> `[151.060–151.220]` Вне `[151.220–151.640]` зависимости `[151.640–151.840]` от `[151.840–152.080]` того, `[152.260–152.520]` какие `[152.520–152.700]` там `[152.700–153.400]` сдерживающие `[153.400–154.000]` факторы, `[154.140–154.440]` кому `[154.440–155.040]` приехала `[155.040–155.340]` мама, `[155.600–155.720]` какие `[155.720–155.900]` там `[155.900–156.380]` праздники `[156.380–156.500]` на `[156.500–156.720]` дворе, `[157.080–157.200]` они `[157.200–157.800]` понимают, `[157.840–157.940]` что `[157.940–158.120]` нужно `[158.120–158.580]` действовать `[158.580–158.740]` и `[158.740–159.240]` действуют `[159.240–159.420]` прямо `[159.420–159.840]` сейчас.

### B2 — Арсен Маркарян

Exact transcript, 19 words. Filler interjections are retained because subtitle timing depends on them:

> `[149.680–149.860]` Он `[149.860–150.320]` начинает `[150.320–150.800]` объяснять: `[150.840–151.000]` «О, `[151.000–151.540]` я устал. `[151.640–151.800]` О, `[151.800–152.080]` идёт `[152.080–152.520]` дождик. `[152.520–152.720]` Ой, `[152.720–152.840]` ну `[152.840–153.200]` начну `[153.200–153.580]` завтра». `[154.080–154.680]` Причина-то `[154.680–154.920]` на `[154.920–155.200]` самом `[155.200–155.460]` деле `[155.460–155.880]` другая `[155.880–156.100]` совершенно.

### B3 — Алексей Ситников

Exact transcript, 23 words:

> `[818.000–818.380]` И `[818.380–818.480]` вот `[818.480–818.660]` быть `[818.660–818.780]` в `[818.780–819.380]` гармонии, `[819.420–819.640]` собственно, `[819.700–819.860]` с `[819.860–820.580]` бессознательным — `[820.720–820.840]` то, `[820.840–820.980]` что `[820.980–821.800]` Фрейд `[821.820–822.120]` когда-то `[822.120–822.420]` сказал: `[824.160–824.540]` «Свобода — `[824.660–824.700]` это `[824.700–825.300]` способность `[825.300–825.780]` хотеть `[825.780–826.240]` то, `[826.260–826.380]` что `[826.380–826.680]` хочешь `[826.680–826.880]` на `[826.880–827.080]` самом `[827.080–828.000]` деле».

## Visual QC

| ID | Face / framing | Embedded captions | B-roll in interval | QC artifact |
|---|---|---|---|---|
| A | clear face; alternating medium/close interview angles | none observed | none | [`qc/A-khakamada-monologue-22.04s-1080p-contact.jpg`](qc/A-khakamada-monologue-22.04s-1080p-contact.jpg) |
| B1 | clear seated speaker; stable wide-medium frame | none observed | none | [`qc/B1-gandapas-8.78s-1080p-contact.jpg`](qc/B1-gandapas-8.78s-1080p-contact.jpg) |
| B2 | clear studio talking head; stable medium close-up | none observed | none | [`qc/B2-markaryan-6.50s-1080p-contact.jpg`](qc/B2-markaryan-6.50s-1080p-contact.jpg) |
| B3 | clear face; stable high-quality interview close-up | none observed | none | [`qc/B3-sitnikov-10.00s-1080p-contact.jpg`](qc/B3-sitnikov-10.00s-1080p-contact.jpg) |

## Independent V2 audit

- Rybakov (`sypmYQ_29JU`) was rejected for V3: the prior picture interval is B-roll (woman/family) with large embedded captions, not a clean speaker shot.
- Hartmann (`XFFoVLBk8ck`) was rejected for V3: the prior speech reveal depends on distressed-man/gym B-roll and source music rather than a clean continuous talking head.
- Focus Hartmann (`PoffvJhJ_gU`) remains a usable reserve, but the existing ingest is only 720p and its wide, older interview framing ranks below A.
- The prior Markaryan ledger/source was incorrectly treated as 1080p; local inspection showed 640×360. It was not reused. V3 was reacquired from the official 1080p HLS stream. The first proposed V3 window (`157.300–165.160`) was also discarded because editorial text appears inside it; the final B2 window ends before that graphic.

## Acquisition and provenance

- Downloader: `uvx yt-dlp 2026.08.19`.
- Video: YouTube HLS format `270` (1920×1080 H.264).
- Audio: original Russian HLS `234-1` where exposed; `234` on B3.
- Section extraction: `--download-sections` with `--force-keyframes-at-cuts`; MP4 merge via local FFmpeg.
- Source page state observed during acquisition: public official-channel uploads; YouTube metadata did not expose a reuse license. Therefore every clip remains `permission_required` until the producer records permission or a valid license.
- Browser-session fallback was unavailable on this machine (`agent.browsers.list()` returned no browsers), so no cookie/private-session source was used.

## Downstream contract

1. Use only the four files in `clips/` listed above.
2. Preserve speaker identity and source URL in any production ledger.
3. Do not infer publication permission from this package.
4. Human-listen every selected line before final subtitle burn-in, especially B2 filler words and A's unquoted portions.
5. If the edit needs a different sentence, return to source selection; do not extend beyond the verified ranges without a new visual/audio/rights audit.
