# QA report — motivation web montage

Final QA date: 2026-08-28  
Status: **PASS for internal review**

## Deliverables

| File | Duration | Frame | Video | Audio | Size | SHA-256 |
|---|---:|---:|---|---|---:|---|
| `output/motivation-web-montage-master.mp4` | 27.587 s | 1080×1920, 30 fps | H.264, 2,253,807 bit/s | AAC stereo, 48 kHz, 191,517 bit/s | 8,411,797 bytes | `BE8261BDE64C6A05DFF88B3BE15C580B4DB9E379DDABAE906F52BC0FB2F256AC` |
| `output/motivation-web-montage-telegram.mp4` | 27.400 s | 720×1280, 30 fps | H.264, 423,441 bit/s | AAC stereo, 48 kHz, 191,502 bit/s | 2,138,118 bytes | `9FE8D50FC9999A70ADAC2E24749A529748E0AF87DAB3156C05EC3454028670EA` |

The Telegram deliverable is below the 20 MB target. Its audio stream is copied from the approved master to avoid an additional lossy AAC generation and true-peak overshoot.

## Audio QA

Both deliverables measured identically with FFmpeg `ebur128=peak=true`:

- Integrated loudness: **−15.0 LUFS**
- Loudness range: **4.5 LU**
- True peak: **−1.3 dBFS**
- Sample rate: **48,000 Hz**
- Channels: **2**
- Silence detector (`−45 dB`, minimum 0.50 s): **no events**
- Fish Audio / TTS: **not used**

## Video QA

- Full decode, master: **PASS**
- Full decode, Telegram: **PASS**
- Black-frame detector (`d=0.10`, `pix_th=0.02`): **no events**
- Freeze detector (`−55 dB`, minimum 1.0 s): **no events**
- Contact-sheet review: **PASS**
- Russian captions stay inside the vertical safe area; the first Jocko caption was split into two lines to prevent clipping.
- Jocko footage uses a blurred 9:16 surround plus a fit-width foreground inset; the speaker remains readable and the source handle does not dominate the frame.
- Arnold reframe centers the face; white/red captions remain readable against the black plinth.

Visual evidence: `qc/contact-sheet.jpg`.

## HyperFrames validation

- HyperFrames version: **0.8.17** (latest reported by CLI)
- Files scanned: **4**
- Errors: **0**
- Warnings: **0**
- Info findings: **0**
- Lint result: **PASS**

The project preserves a valid HyperFrames composition and scene sources. The delivery render used the documented direct FFmpeg fallback because browser-runtime rendering was not reliable in the available session. Reproducible commands are in `render-direct.ps1` and `render-telegram.ps1`.

## Sources and rights

- All three speech excerpts were downloaded fresh from their listed online primary video pages for this project.
- The prohibited Downloads MP4 examples and the old `motivation-montage-20260828` project are not referenced as render sources.
- URL, channel, source timestamps, source hashes, clip hashes, local paths and procedural-music hash are recorded in `source-ledger.json`.
- YouTube license metadata was not declared for the three source pages. Copyright, reuse and likeness rights therefore remain **unverified**.
- This build is approved only as an **internal prototype** until usage rights are cleared for publication or paid client delivery.

## Editorial result

- Sequence: David Goggins → Jocko Willink → Arnold Schwarzenegger.
- Original speaker voices retained; no synthetic voiceover.
- Russian subtitles are meaning-matched translations of the selected English lines.
- Dark monochrome grade, controlled grain, red accents, short transition flashes and procedural dark music bed match the requested depressive-motivational format.
