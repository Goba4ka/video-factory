# Motivation A V2 — final QC

QC completed: `2026-08-29`.

## Composition gate

- HyperFrames project scripts upgraded and pinned to `0.8.18` before render.
- `hyperframes check --strict --snapshots`: **PASS**.
- Lint: `0` errors, `0` warnings.
- Runtime: `0` errors, `0` warnings.
- Layout: `0` issues across ten requested samples.
- Motion assertions: **PASS**, 300 samples.
- Contrast: `5/5` sampled caption states passed.
- Final `28.8s` snapshot visually inspected: speaker close-up remains visible under the last caption; no black frame.
- Runtime HTML contains no `data-automation` and no runtime `data-color-grading`.

## Rendered master

- File: `renders/motivation-a-v2-final.mp4`.
- SHA-256: `5FDE36822C67675CF295D346C7A9D15E4ED18FB633086BF11BBFA77FA8C4BCB8`.
- Size: `35,688,082` bytes (`34.03 MiB`).
- Duration: `29.200s` video / `29.180s` audio.
- Video: H.264 High, `1080x1920`, yuv420p, CFR `30/1`, `876` frames.
- Audio: AAC-LC, 48 kHz, stereo, 192 kb/s.
- Integrated loudness after mono-to-stereo compensation: `-14.58 LUFS`; true peak `-4.35 dBTP`; LRA `0.90 LU`.
- Full video+audio decode with `-xerror`: **PASS**.

## Telegram copy

- File: `renders/motivation-a-v2-telegram.mp4`.
- SHA-256: `0B537244D1E09AFE43A263F0580C23A98049805DFF605088426841A54C2FD826`.
- Size: `4,818,090` bytes (`4.59 MiB`).
- Duration: `29.200s` video / `29.180s` audio.
- Video: H.264 High, `720x1280`, yuv420p, CFR `30/1`, `876` frames, CRF 21 / veryfast.
- Audio: AAC-LC, 48 kHz, stereo, 160 kb/s.
- Integrated loudness: `-14.62 LUFS`; true peak `-4.12 dBTP`; LRA `0.90 LU`.
- Full video+audio decode with `-xerror`: **PASS**.

## Audio correction note

The HyperFrames render expanded the mono baked mix to stereo, which measured
approximately 3 dB louder in integrated loudness. The master was corrected by
stream-copying the rendered video and attenuating/re-encoding only the audio by
3 dB. The pre-correction render is retained as
`assets/audit/motivation-a-v2-final-before-audio-qc.mp4` for audit/recovery.

## Rights gate

- Music: Pixabay Content License; Content ID registered. Keep `SOURCES.md` for any claim workflow.
- Speaker footage/speech: **HOLD / permission required for commercial publication**. No license grant was found in the supplied workspace.

## Review surface

- Studio preview: `http://127.0.0.1:3005/#project/client-final-motivation-a-v2-20260829`.
