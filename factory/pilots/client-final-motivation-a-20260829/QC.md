# QC — Client Final Motivation A

## Delivery state

- Status: preview-ready; final render intentionally not run before approval.
- Format: 1080×1920, 30 fps, 29.278 seconds.
- Language: Russian.
- Voice: original Russian speaker audio; no Fish Audio or synthetic voice.
- Structure: one coherent statement, 16 caption chunks, 3–5 words per chunk, maximum two lines.

## Editorial and visual review

- Full-height portrait composition with the speaker occupying the dominant visual area.
- Opening wide angle is reframed to keep the speaker large; the source cut hands off to a restrained close-up push.
- No top label, side copy, logo, progress bar, decorative UI, or unrelated B-roll.
- Monochrome `mono-clean` treatment with restrained vignette and grain.
- Off-white captions with selective red emphasis; accent spans use an opaque dark backing for reliable contrast.
- Visual inspection completed at 0.4, 3.0, 7.9, 18.9, and 28.7 seconds. No clipping, overflow, face obstruction, or annotation overlays found.

## Audio review

- Speech remains the primary signal.
- Music bed: Scott Buckley — `Intervention (No Piano Melody)`.
- Base music level: 0.085, with fade-in/fade-out and four-band voiceover carve.
- Voiceover carve strength: 0.25; music is reduced around speech presence bands and does not mask the speaker.

## Automated HyperFrames gate

Command:

```powershell
npx hyperframes check --snapshots --at 0.4,3.0,7.9,18.9,24.6,28.7 --timeout 60000 --json
```

Result:

- Overall: PASS.
- Lint: 0 errors, 0 warnings, 0 info findings.
- Runtime: 0 errors, 0 warnings, 0 info findings.
- Layout: 0 findings across six sampled timestamps.
- Motion: 300 samples, 0 findings.
- Contrast: 10/10 checks passed.
- Snapshot findings: none.

## Rights gate

- Speaker source: public YouTube upload from the podcast `Основатели`, exact extract and SHA-256 documented in `SOURCES.md`.
- Speaker footage status: **HOLD / permission required for commercial publication**. A public upload does not itself grant reuse rights.
- Music: CC BY 4.0. Attribution in `SOURCES.md` must accompany publication.
- Frozen local assets and hashes are documented in `SOURCES.md`.

## Approval gate

The composition, assets, clean check, and snapshots are ready for review. Final rendering should be run only after approval and after resolving the speaker-footage publication right.
