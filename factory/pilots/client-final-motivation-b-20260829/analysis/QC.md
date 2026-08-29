# QC — preview build

## Automated gate

- Command: `npx hyperframes check --json --snapshots --at=0.2,30.5 --timeout=90000`
- Result: `ok=true`.
- Lint: 0 errors, 0 warnings, 0 findings.
- Runtime: 0 errors, 0 warnings, 0 findings.
- Layout: 0 errors, 0 warnings, 0 findings.
- Motion: 0 errors, 0 warnings, 0 findings.
- Contrast: 0 errors, 0 warnings, 0 findings; 2/2 sampled text states passed.
- Machine-readable output: `analysis/check.json`.

## Visual proof

- `snapshots/frame-00-at-0.2s.png`: full-screen speaker, clean white hook caption,
  no badge or contrast annotation.
- `snapshots/frame-01-at-30.5s.png`: horizontal-master 9:16 reframe; both eyes, full nose
  and face contour remain in frame.

## Audio proof

- Original Russian speech only; no Fish/TTS.
- Music starts at `data-volume=0.08` and carries one generated `data-fx-carve` attribute,
  a 4-node carve chain, and four measured automation lanes.
- Duplicate hand-authored carve attribute removed; literal `data-fx-carve` count in
  `index.html` is 1.

## Release status

- Preview is ready for review.
- Final MP4 render was intentionally not started.
- Public/commercial release remains blocked pending interview rightsholder permission.
