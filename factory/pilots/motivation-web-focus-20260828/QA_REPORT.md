# QA REPORT — MOTIVATION WEB FOCUS

Date: 2026-08-28  
Project: `motivation-web-focus-20260828`  
Status: **PASS FOR CLIENT REVIEW**

## Editorial result

- Duration: 26.08 seconds.
- Format: 9:16 vertical, dark monochrome, controlled grain, slow push-ins, acid-yellow emphasis.
- Speaker: Jocko Willink, original voice only.
- Russian captions: manually timed and burned into both deliverables.
- Caption-transition contact sheet was reviewed after removing all overlapping dialogue windows.
- No Fish Audio, synthetic narration, cloned voice, or TTS was used.

## Primary-source provenance

- Official/public source: `Jocko Motivation - Where Does Discipline Come From? (from Jocko Podcast)`.
- Channel: Jocko Podcast.
- URL: <https://www.youtube.com/watch?v=_tE8kE8IfiY>
- Channel URL: <https://www.youtube.com/channel/UCkqcY4CAuBFNFho6JgygCnA>
- Used range: `00:55.520–01:21.600`.
- Local source SHA-256: `FAC4306311B5820E975AA2A7A122BCCB1867BBC24F7AF116522C66BB1E5EE620`.
- Rights note: the page carries the Standard YouTube license and does not grant an explicit commercial-reuse license. The source is verified primary/official, but public commercial redistribution should be cleared with the rights holder.

## Supporting media

- Rain runner B-roll: Pexels, creator Evgenij Mikhailov, Pexels License.
- Night track B-roll: Pexels, creator Mman, Pexels License.
- Music bed: original local procedural synthesis at 70 BPM; no samples or third-party recordings.
- Full URLs, direct asset URLs, license notes, and source hashes are recorded in `MEDIA_LEDGER.json`.

## Deliverables

### Master

- File: `renders/MOTIVATION_WEB_FOCUS_MASTER.mp4`
- SHA-256: `96DFB68C659E2500E7D2C07EEC6A6311E9A698990BA11E03B614EF3880341F6E`
- Size: 68,143,568 bytes.
- Video: H.264, 1080×1920, 30 fps, 781 decoded frames.
- Audio: AAC, stereo, 48 kHz.
- Container duration: 26.080 s.

### Telegram copy

- File: `renders/MOTIVATION_WEB_FOCUS_TELEGRAM.mp4`
- SHA-256: `C3D21443BF2E87A2E418643F3CECB28868EBFF54DEE82B824DF4ED024BCE1367`
- Size: 4,041,374 bytes (under the 20 MB delivery ceiling).
- Video: H.264, 720×1280, 30 fps, 781 decoded frames.
- Audio: original master AAC stream copied without a second lossy encode, stereo, 48 kHz.
- Container duration: 26.080 s.

## Automated QA

- HyperFrames 0.8.17 lint: PASS — 0 errors, 0 warnings, 0 info findings.
- HyperFrames check: PASS — runtime, layout, motion, and contrast all clean; 300 motion samples; 25/25 contrast checks.
- Black detection: no black events in master or Telegram copy.
- Freeze detection (`-50 dB`, 0.75 s): no freeze events in master or Telegram copy.
- Silence detection (`-45 dB`, 0.5 s): closing low-level hold from about 23.13 s to the end; the spoken sentence has completed and the visual payoff remains on screen. This is an intentional close, not missing source audio.
- Integrated loudness: master `-14.51 LUFS`; Telegram `-14.51 LUFS`.
- Loudness range: master `5.90 LU`; Telegram `5.90 LU`.
- True peak: master `-1.44 dBTP`; Telegram `-1.44 dBTP`.
- Forbidden-source scan: no references to any previously supplied local clips or earlier pilot project.

## Visual inspection

- `snapshots/master-contact.jpg`: full-program contact sheet reviewed.
- `snapshots/transition-contact.jpg`: every caption handoff reviewed; no doubled or stacked subtitles remain.
- `snapshots/master-final.jpg`: closing frame reviewed; caption safe area, Cyrillic rendering, contrast, and non-black ending confirmed.
- HyperFrames check snapshots: `snapshots/frame-00-at-1.4s.png` through `snapshots/frame-04-at-24.6s.png`.

## Production note

The editable HyperFrames source is the project root (`index.html`, `index.motion.json`, `BRIEF.md`, `SCRIPT.md`, `STORYBOARD.md`, `CAPTIONS.json`). The delivery encodes were assembled deterministically with local FFmpeg from the frozen ledger assets, preserving the original speaker audio.
