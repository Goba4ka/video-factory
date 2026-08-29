# QA Report — Motivation Montage A

Date: 2026-08-28

## Deliverables

- `output/motivation-montage-master.mp4`
  - H.264 High / AAC stereo
  - 1080×1920, 30 fps, 48 kHz
  - Duration: 29.467 s
  - Size: 15,154,796 bytes
  - SHA-256: `9FF1124E92728DAE005A9B678F5F4AE462513F1E4584A358D7312D54C3EC4FA2`
- `output/motivation-montage-telegram.mp4`
  - H.264 High / AAC stereo
  - 720×1280, 30 fps, 48 kHz
  - Duration: 29.467 s
  - Size: 3,317,708 bytes (under 20 MB)
  - SHA-256: `EA536C9EA39BB76FE6B3FD6CF5EBC4F997264C15D564544A0886246CB165DCFB`

## Technical checks

- Full decode test passed on both files with zero FFmpeg errors.
- Final mix measured at -14.0 LUFS integrated, 1.8 LU LRA, -1.0 dBFS true peak.
- Contact sheet visually inspected: all four speakers/scenes present; captions are legible, inside the vertical safe area, and the opaque lower matte removes embedded source-caption collisions.
- Final contact sheet: `qc/contact-sheet.jpg`.
- HyperFrames lint: passed with 0 errors, 0 warnings, 0 infos across five composition files.
- HyperFrames runtime snapshot check did not complete because browser navigation exceeded 30,000 ms. The preserved HyperFrames project remains the editable source; final delivery was rendered through the deterministic FFmpeg fallback in `render-direct.ps1`.

## Editorial checks

- No Fish Audio or synthetic TTS used.
- Only the user-supplied MP4 material was used for voices, footage, and music bed.
- Structure: David Goggins → Арсен Маркарян → archival unidentified speaker → David Goggins payoff.
- Visual treatment: monochrome contrast, grain, vignette, slow push-ins, white/red Russian captions, two-frame cut flashes, source-caption suppression matte.
- Voice treatment: high-pass, low-mid cleanup, clarity lift, compression/loudness normalization; music is side-chain ducked beneath speech.

## Rights / attribution note

This is an internal approval demo. Rights to the supplied footage, voices, likenesses, and music have not been verified. The third speaker remains intentionally labeled as archival/unidentified; do not publish with a guessed attribution. Obtain the necessary permissions or replace the assets before public/commercial distribution.
