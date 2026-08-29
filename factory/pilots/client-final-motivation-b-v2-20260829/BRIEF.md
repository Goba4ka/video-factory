# BRIEF — Motivation B V2

- Flow: automation / general-video.
- Output: 31.2s vertical Russian motivational interview edit, 1080×1920, 30 fps.
- Source: Nikolay Tsiskaridze interview excerpt from the approved local master. Preserve the original Russian speech.
- Hook: an immediate speaker close-up and the line «Строгий педагог — в профессии. В жизни я друг.» within the first two seconds.
- Visual edit: manual per-segment crops from the horizontal master. The active speaker must be framed intentionally; use a two-shot only when it communicates context. No accidental half-faces, side labels, or decorative UI.
- Captions: Russian, phrase/word-synced, 2–4 words per phrase, 80–96 px, maximum two rows, lower-middle safe area. Active phrase emphasis must follow speech.
- Music: contemporary dark motivational / cinematic trap / restrained phonk, rights-cleared, no Scott Buckley. Speech remains dominant. Target overall -14 LUFS; bed about -20 to -17 LUFS under speech and rises only in pauses. Voice carve 0.30.
- Rhythm: dense but readable cuts; a purposeful punch/reframe/impact every 1–2 seconds and restrained SFX only where they help the speech.
- Performance: pre-rendered H.264 CFR visual proxy, simple deterministic DOM, muted video plus separate audio tracks.
- QA: strict HyperFrames check, representative snapshots, and realtime preview status/HTTP verification. Do not render a final MP4 before user approval.

