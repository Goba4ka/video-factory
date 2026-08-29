# QA report — motivation-ru-montage-v2

Date: 2026-08-28

## Delivery files

| File | Technical profile | Size | SHA-256 |
|---|---|---:|---|
| `dist/motivation-ru-montage-v2-master-1080x1920.mp4` | H.264 High, 1080×1920, 30 fps, AAC 48 kHz stereo, 30.501 s | 8,308,361 B | `BF13141D44D16AC053C1DA30C5596911F4C9734AB3A94455799E923C8A9DB00A` |
| `dist/motivation-ru-montage-v2-telegram-720x1280.mp4` | H.264 High, 720×1280, 30 fps, AAC 48 kHz stereo | 2,538,621 B | `4FFDDC3B6212E1EE8C82C75CF4CD74971FB665BF30CDD14E85AB7F996E394996` |

## Automated checks

- HyperFrames lint: PASS — 0 errors, 0 warnings.
- HyperFrames runtime/layout/motion check: PASS — 0 errors, 0 warnings, 0 layout issues across 9 samples.
- Contrast: PASS — 35/35 text checks meet WCAG AA.
- HyperFrames snapshots: PASS — 5 frames at 1.0, 10.8, 17.5, 24.8, 29.7 seconds.
- Decode/probe: PASS — SAR 1:1, DAR 9:16, 30 fps, AAC 48 kHz stereo.
- Blackdetect: PASS — no unintended full-black interval ≥0.20 s.
- Silencedetect: PASS — no interval below −50 dB lasting ≥0.50 s after final audio revision.
- Loudness: −14.71 LUFS integrated; −2.22 dBTP true peak; 1.80 LU LRA. PASS against target −14.5…−15.0 LUFS and TP ≤ −1.2 dBTP.
- Music end-open verification: final 1.35 s mean −16.5 dBFS, max −5.0 dBFS; music is intentionally audible under the end card.

## Visual review

- Арсен Маркарян: identifiable face visible throughout the first block.
- Игорь Рыбаков: identifiable face visible throughout the second block; original lower subtitle strip is cropped out of the sharp panel.
- Оскар Хартманн: identifiable face visible at block opening and later beats.
- No competing second subtitle layer is visible in the authoritative master contact sheet.
- Captions remain inside vertical safe margins and retain off-white/red hierarchy at Telegram scale.
- Review artifacts: `qc/master-contact-v2.jpg` and `qc/hyperframes-snapshots/contact-sheet.jpg`.

## Audio review

- Rybakov and Hartmann dialogue were center-extracted with FFmpeg `dialoguenhance` and FFT-denoised before the licensed bed was added, suppressing the original edited-video music.
- Nightfall is sidechain-carved under speech plus a low-level parallel floor, preventing the bed from disappearing in micro-pauses.
- The final 1.49 seconds contain no speech; Nightfall opens by design and fades at delivery end.

## Rights gate

- Scott Buckley — “Nightfall”: CC BY 4.0; attribution stored in `RIGHTS.md` and `source-ledger.json`.
- All three speech/video excerpts: `permission_required` before publication or commercial use.

