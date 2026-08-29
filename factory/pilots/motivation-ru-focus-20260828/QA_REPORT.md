# QA report — motivation RU focus V2

Finalized: 2026-08-28 17:46 (Europe/Simferopol)

## Deliverables

- Master: `renders/motivation-ru-focus-v2-master-1080x1920.mp4`
  - SHA-256: `A95E41CEE4810E0568ABC333989C6714B7B37D02F83E2F403B2A61A50EAA49BB`
  - H.264 High, 1080×1920, yuv420p, 30 fps, 851 frames
  - AAC stereo, 48 kHz, 192 kb/s nominal
  - Duration 28.370 s; size 10,545,411 bytes
- Telegram proxy: `renders/motivation-ru-focus-v2-telegram-720x1280.mp4`
  - SHA-256: `73466C42E6C5C6A9396793FCB8DA16A02962BBE87657BAC7E8E38F94F1BB2D8B`
  - H.264 High, 720×1280, yuv420p, 30 fps, 851 frames
  - Duration 28.370 s; size 3,507,998 bytes
  - AAC packet-stream hash equals the master: `d27a4c4d8c5281dd9716df7e8032a87d89fb70306db724fe4d0b975ec034d484`

## HyperFrames source contract

- `hyperframes lint`: PASS, 0 errors; one non-blocking density warning for seven caption elements on track 30.
- `hyperframes check . --snapshots --samples 15`: PASS.
- Runtime: 0 errors/warnings.
- Layout: 15/15 temporal samples pass; no overflow or clipping findings.
- Motion: 300 samples pass; all assertions pass.
- Contrast: 25/25 sampled text checks pass.
- Keyframe diagnostic: five continuous camera strokes found across 00:00–00:28.37; onion-skin artifact saved at `snapshots/camera-keyframes.jpg`.
- Previously reported nested-timing and undeclared-font errors were fixed before the final render.

## Audio QC

- Master integrated loudness: **−14.83 LUFS**.
- Master true peak: **−1.30 dBTP**.
- Master LRA: **2.50 LU**.
- Target gate: −14.5…−15.0 LUFS; TP ≤ −1.2 dBTP — PASS.
- Processed voice stem: −16.76 LUFS, −2.93 dBTP.
- Duck/carved music stem: −23.43 LUFS overall.
- Music 20.50–26.30 s: −20.47 LUFS.
- Music 26.30–28.37 s: −17.50 LUFS, confirming the requested audible final crescendo.
- Silence scan at −45 dB / 0.50 s: no events.
- No TTS, voice cloning, or Fish Audio was used.

## Video QC

- Full decode: PASS.
- Black detect (`d=0.20`, `pix_th=0.10`): no events.
- Freeze detect (`−50 dB`, `d=2.0`): no events.
- Final contact sheet visually reviewed: `snapshots/master-contact-final.jpg`.
- Final close frame visually reviewed: `snapshots/master-final-approved.jpg`.
- Face remains inside the frame through the final push-in; all seven Russian caption beats are legible in the vertical safe zone.

## Rights and provenance

- Speech/picture source: official verified Oskar Hartmann channel, excerpt 00:28.680–00:57.050.
  - URL: https://www.youtube.com/watch?v=PoffvJhJ_gU
  - Source page does not publish a reusable license. Status: **`permission_required`** before commercial/customer publication.
  - Source SHA-256: `5EA55507EE8887C9B21E7F5B2E0028ACAFC0A51B461401494C600F4D935AFC51`.
- Music: Scott Buckley, “The Long Dark”, official composer page, excerpt 02:45.000–03:13.370.
  - URL: https://www.scottbuckley.com.au/library/the-long-dark/
  - License: CC BY 4.0, commercial use allowed with attribution.
  - Required credit: `'The Long Dark' by Scott Buckley - released under CC-BY 4.0. www.scottbuckley.com.au`
  - Source SHA-256: `CD48B94164C17DFE86685C22D26E4F4FC96BC693F3B6F5ED7A53947A502E09CF`.
- Full machine-readable ledger: `MEDIA_LEDGER.json`; human credit block: `CREDITS.txt`.
- Production source scan contains no prior-personality media or prior-pilot assets. The only old-name match is the explicit prohibition line in `BRIEF.md`.

## Release decision

Technical/visual/audio: **APPROVED**.

Rights gate: **HOLD FOR SPEECH/PICTURE PERMISSION**. Music and fonts are cleared under their recorded licenses.
