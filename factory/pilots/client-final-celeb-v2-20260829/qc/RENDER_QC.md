# Final render QC

## Toolchain

- HyperFrames project pin upgraded from `0.8.17` to `0.8.18`.
- Post-upgrade `check --strict --samples=18 --frame-check`: pass.
- Lint/runtime/motion: 0 errors, 0 warnings.
- Layout: 0 issues across 19 samples.
- Contrast: 6/6 checks pass WCAG AA.
- Final render: HyperFrames high quality, GPU encoder + browser GPU, 30 fps, portrait.

## Master

- File: `renders/celebrity-v2-final.mp4`
- Size: 42,628,483 bytes (40.65 MiB).
- Duration: 26.000 s.
- Video: H.264 High, 1080 × 1920, yuv420p BT.709 progressive, CFR 30/1, 780 frames.
- Audio: AAC-LC, 48 kHz, stereo, approximately 195 kb/s.
- Loudness: −13.9 LUFS integrated, 1.2 LU LRA, −2.5 dBFS peak.
- SHA-256: `CE88322825778637B4F83AF1E515D7AB0C2121A50F97BC51604F19A6AE3500C0`.

## Telegram copy

- File: `renders/celebrity-v2-telegram.mp4`
- Size: 5,751,111 bytes (5.48 MiB).
- Duration: 26.000 s.
- Video: H.264 High, 720 × 1280, yuv420p BT.709 progressive, CFR 30/1, 780 frames, libx264 CRF 21 / veryfast / faststart.
- Audio: AAC-LC, 48 kHz, stereo, approximately 160 kb/s.
- Loudness: −13.9 LUFS integrated, 1.2 LU LRA, −2.4 dBFS peak.
- SHA-256: `10FCC7862D6E04DD19F2A638D0904F2F6E4C85BD92D4E6B486AA0F5D9BBD1946`.

## Decode and visual checks

- Both outputs fully decode with FFmpeg without errors.
- Both outputs contain exactly 780 decoded video frames and an AAC audio stream.
- Both outputs have 780 unique frame hashes; no duplicate-frame/frozen-run evidence.
- Rendered master contact sheet reviewed: hook, named faces/items, charity sequence, proof point, and final payoff are present and correctly framed.
- Evidence: `qc/rendered-master-contact-sheet.jpg`.

## Delivery constraint

Telegram upload was intentionally not performed by this task.
