# Media ledger — motivation-v3-monologue-20260828

## Video / original speech

- Media OS id: `video_001`
- Frozen path: `.media/video/video_001.mp4`
- Original: `factory/research/v3-sources/khakamada-1080-304.460-326.500.mp4`
- SHA-256: `F027FD2FBBEA97F7AD1ADE177B1D0F9E80F68E3872FA3D8ACDCE32D201FB8E06`
- Source properties: 1920×1080, H.264, 30 fps; original AAC speech.
- Used range: local 00:00.000–00:18.700 only.
- Picture operation: native 1080×1080 crop at `x=0,y=0`; no upscale before the 1.08×/1.14× authored punch-ins.
- Audio operation: same source range on a separate `speech` audio element.
- Rights: supplied/selected production source; publication clearance remains the producer's responsibility.

## Music

- Media OS id: `bgm_001`
- Frozen path: `.media/audio/bgm/bgm_001.wav`
- Original audit candidate: `factory/research/motivation-references-v3-audio/candidates/reference/reference-bed-01-dark-bass-70bpm.wav`
- SHA-256: `B3EC8889EE72046A7AB3BCDE656A487A13C2AB809271A74A01B38170827E5D22`
- Properties: PCM24, 48 kHz, stereo; 70.79 BPM estimate; source cut -18.85 LUFS-I / -5.60 dBTP.
- Used range: 00:00.000–00:18.700.
- Mix intent: dynamic speech carve plus slow recovery; music stays behind speech and resolves under the last word.
- Rights: **permission_required**. Test/approval use only until the owner confirms publication rights.

## Font

- Path: `assets/fonts/Oswald-Variable.ttf`
- SHA-256: `5B38C246E255A12F5712D640D56BCCED0472466FC68983D2D0410EC0457C2817`
- Use: Oswald 700 for on-screen captions and ASS portability.

## Treatment

- Primary lane: dark portrait polish.
- Realtime canonical `data-color-grading`: saturation -1, contrast/blacks shaping, vignette, deterministic grain, faint scanlines.
- No CSS/SVG imitation of the supported media treatment.
- Reference target: `factory/research/v3-sources/qc/khakamada-v3-style.png` and approved prototype contact frames.

## Subtitle truth

- Authoritative human-readable file: `captions.ass`.
- HyperFrames mirrors the same 22 cue windows as timed DOM clips for preview, layout/contrast inspection and deterministic render.
- Accent is restricted to `СЕЙЧАС` and `ДИСЦИПЛИНА`.

## Final outputs

- Master target: 1080×1920, 30 fps, H.264 CRF 16, AAC 192k, -14.5 LUFS-I, encoded true peak ≤ -1.2 dBTP.
- Telegram target: 720×1280, 30 fps, H.264 CRF 20, AAC 160k, encoded true peak ≤ -1.2 dBTP.
- `render.ps1` uses two-pass loudnorm with a pre-AAC target of -2.0 dBTP and rechecks the encoded files.
