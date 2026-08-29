# QA report — MOTIVATION FOCUS / Markaryan

Date: 2026-08-28

## Editorial result

- Required source: `assets/source/arsen-markaryan.mp4`.
- Used source ranges: `00:00.000–00:10.000` and `00:12.100–00:21.650`.
- Removed source range: `00:10.000–00:12.100` (profanity).
- Cut before the later dirty tail; the first visible profanity frame was found near source `00:22.000` during manual contact-sheet inspection.
- Speech length after cut: 19.567 s. Final silent impact/hold extends the edit to 22.215 s.
- Voice is the real source voice. Fish Audio and all other TTS were not used.
- Treatment: monochrome contrast, subtle animated push-in, controlled grain/vignette, acid-yellow identity accent, source captions retained, original low-resolution source upscaled with Lanczos.
- BGM: locally synthesized 72 BPM low piano/sub bed with a 9–10 s riser and final impact. It contains no sampled third-party recording.

## Render validation

### Master

- File: `renders/MOTIVATION_FOCUS_MASTER.mp4`
- Duration: 22.215 s
- Video: H.264 High, 1080×1920, 30 fps, yuv420p
- Audio: AAC stereo, 48 kHz, 184.5 kb/s
- File size: 8,992,816 bytes
- Integrated loudness: -14.0 LUFS
- True peak: -1.6 dBFS
- SHA-256: `793A6EFCC6FE1B85074208EBA4D3EA7A5721D98448633F075AE05AED58119318`

### Telegram delivery

- File: `renders/MOTIVATION_FOCUS_TELEGRAM.mp4`
- Duration: 22.215 s
- Video: H.264 High, 720×1280, 30 fps, yuv420p
- Audio: AAC stereo, 48 kHz, 126.3 kb/s
- File size: 1,699,989 bytes (well below 20 MB)
- SHA-256: `157161689CAEE39285FD83DC889DB71FFAC827E24FF1531EE476F15A1607D1CE`

## Checks performed

- `hyperframes lint --json`: passed with 0 errors; one non-blocking track-density maintainability warning.
- HyperFrames runtime snapshot check: browser navigation timed out at its fixed 10 s limit after fonts and GPU initialized.
- HyperFrames renderer: could not discover FFmpeg/FFprobe on its process PATH. The complete linted HyperFrames source is retained in `index.html`; the delivery was rendered with the explicit local FFmpeg binaries.
- `ffprobe`: both deliverables contain the expected H.264 video and AAC 48 kHz stereo audio streams.
- Contact sheet visually inspected: framing, monochrome treatment, captions, cut continuity, and final Cyrillic card are in frame.
- Final frame separately inspected at 21 s.
- Audio measured with EBU R128 after final mastering.

## Evidence

- `snapshots/master-contact.jpg`
- `snapshots/master-final.jpg`
- `qc/source-clean-cut.mp4`

## Rights note

The supplied source is treated as client-provided/internal review media. Distribution rights for the speaker footage and original embedded caption treatment must be confirmed by the publisher before public release.
