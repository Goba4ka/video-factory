# QC report — motivation-v3-montage-20260828

Status: TECHNICAL PASS / PUBLICATION RIGHTS PENDING

## Deliverables

- `dist/motivation-v3-montage-master.mp4`
- `dist/motivation-v3-montage-telegram-720x1280.mp4`
- `dist/motivation-v3-montage-raw.mp4`

## Technical checks

| Check | Master | Telegram |
| --- | --- | --- |
| Container / codecs | MP4 / H.264 + AAC | MP4 / H.264 + AAC |
| Frame | 1080x1920 | 720x1280 |
| Frame rate | CFR 30 fps | CFR 30 fps |
| Duration | 25.300 s | 25.300 s |
| Audio | 48 kHz stereo | 48 kHz stereo |
| Integrated loudness | -14.55 LUFS | -14.57 LUFS |
| True peak | -1.26 dBTP | -1.26 dBTP |
| File size | 14,007,623 bytes | 1,535,964 bytes |
| Full decode | PASS, no errors | PASS, no errors |

HyperFrames lint/check: 0 errors, 0 warnings. Text contrast: 4/4 checks pass WCAG AA.

## Editorial and boundary checks

- Original speaker voices only; no TTS and no voice cloning.
- Speaker order: Markaryan 0.00–6.50 s, Gandapas 6.50–15.28 s, Sitnikov 15.28–25.28 s.
- Captions use the verified source transcript and are burned into the square picture area.
- Exact sequential frames n194–202 and n457–464 were inspected after the final render.
- Both hard cuts pass: stable monochrome picture, no black frame, color wash, exposure flash, or head crop.
- Gandapas picture-only proxy holds the first stable frame for 0.20 s, then resumes the source picture at matching time; original audio remains untouched.
- Independent raw scan: 759 decoded frames; no black segment >=0.10 s, freeze >=1.50 s, or silence >=0.20 s at -50 dB.

Visual evidence:

- `qc/boundary-contact-v3.png`
- `qc/boundary-frames/`
- `qc/contact-sheet.png`

## Publication gate

All source speaker clips and the music bed are marked `permission required` in `MEDIA_LEDGER.md` / `source-ledger.json`. The files are approved for local review only until usage rights are confirmed.
