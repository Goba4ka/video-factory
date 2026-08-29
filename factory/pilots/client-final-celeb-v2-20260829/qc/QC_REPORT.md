# QC report — client-final-celeb-v2-20260829

## Build

- Canvas: 1080 × 1920, 30 fps, duration 25.99 s.
- Editorial structure: 28 hard-cut visual beats; three visual hits inside the first 1.79 s.
- Recognizable subjects: Basta, Egor Kreed, Sergey Burunov, Ekaterina Volkova.
- Captions: 28 speech-aligned chunks, generally 1–3 words, maximum 4, 60–68 px, optical center at 55%, no plaques.
- Audio: one frozen stereo master; no runtime carve or waveform automation.
- Final mix loudness: −13.9 LUFS integrated, 1.2 LU LRA, −2.4 dBFS true peak.

## Automated verification

- `hyperframes lint --verbose`: 0 errors, 0 warnings.
- `hyperframes check --strict --samples=18 --at-transitions --max-transition-samples=90 --frame-check --snapshots`: passed.
- Runtime: 0 errors, 0 warnings.
- Layout: 0 issues across 109 samples.
- Motion: 0 errors, 0 warnings.
- Contrast: 6/6 sampled text checks pass WCAG AA.
- Preview server: `http://127.0.0.1:3004/#project/client-final-celeb-v2-20260829`.
- Realtime player smoke: passed at 6.46 s; player state `Pause`, audio unmuted and advancing, active video `s09` advancing, zero console/page errors.

## Human visual review

- Reviewed 18 strategic snapshots spanning hook, named lots, charity beat, proof point, and payoff.
- Full-bleed faces remain dominant; stock footage is connective only.
- Caption baseline is stable, centered, unobstructed, and readable over both dark and light footage.
- No side labels, top labels, cards, or decorative info plaques.
- Realtime evidence frame: `qc/realtime-smoke.png`.

## Rights gate

`RIGHTS_MANIFEST.json` contains source pages, direct URLs, authors/providers, exact licenses or usage basis, retrieval date, and SHA-256 hashes. The build remains preview-only until commercial/publishing rights for the selected Fish Audio public voice are confirmed by the user. Wikimedia attribution and ShareAlike requirements also apply.
