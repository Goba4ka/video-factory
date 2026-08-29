# STYLE TRUTH V3 — forensic audit of 10 motivation references

Date: 2026-08-28  
Scope: the ten `o*.mp4` files explicitly supplied in the current dialogue. No production project was modified.

## Method and measurement convention

- Native stream data was read with `ffprobe`; every file is constant 30 fps H.264 with AAC stereo at 44.1 kHz.
- Each contact sheet contains 12 frames distributed across the complete source duration.
- Cut rhythm was measured with scene-difference passes at thresholds 0.03–0.18 and then manually de-clustered where flashes, scanlines or ghosting produced several detections for one transition.
- Subtitle bounding boxes were measured on native-resolution PNG frames. Coordinates use `[left, top, right, bottom)` source pixels. “Cap height” means the observed raster glyph height, not the unavailable font's authoring point size.
- Colour values are sampled from encoded pixels and are therefore delivery approximations, not original design tokens.

## Technical matrix

| ID | File | Duration | Native frame | Format role | Contact sheet |
|---|---|---:|---:|---|---|
| R01 | `ogjb1N3eMRef0MRYAIXAIvjN1D8uTHLgQ2AFIj.mp4` | 41.214 s | 576×1024 | text/B-roll montage | [12 frames](contacts/ogjb1N3eMRef0MRYAIXAIvjN1D8uTHLgQ2AFIj__contact.jpg) |
| R02 | `owGEDV4mqIA3hC0XROegufgW4QE2nBD6qQYRoF.mp4` | 26.816 s | 576×694 | speaker compilation / monologue reference | [12 frames](contacts/owGEDV4mqIA3hC0XROegufgW4QE2nBD6qQYRoF__contact.jpg) |
| R03 | `okDojoGYsNMeJBU1YfTfMLjIQ9OLIIUYGEgAjw.mp4` | 38.244 s | 1024×576 | cinematic dialogue montage | [12 frames](contacts/okDojoGYsNMeJBU1YfTfMLjIQ9OLIIUYGEgAjw__contact.jpg) |
| R04 | `o0DOqXEEIDQqX6hfCQFmHnumfB7VYEAxDBzRQ7.mp4` | 16.034 s | 1024×576 | rapid sports B-roll loop | [12 frames](contacts/o0DOqXEEIDQqX6hfCQFmHnumfB7VYEAxDBzRQ7__contact.jpg) |
| R05 | `oYiwFBE1RAhKBRNQi64iWIWAzKMfEmRlC2qoIB.mp4` | 60.651 s | 576×576 | multi-speaker quote montage | [12 frames](contacts/oYiwFBE1RAhKBRNQi64iWIWAzKMfEmRlC2qoIB__contact.jpg) |
| R06 | `oQM4Q3BqBg0IPERE1E5jfHFAN7jDhFmnvcjeME.mp4` | 16.400 s | 576×576 | rapid Russian multi-speaker montage | [12 frames](contacts/oQM4Q3BqBg0IPERE1E5jfHFAN7jDhFmnvcjeME__contact.jpg) |
| R07 | `o0I1EQLyjVjaAkMRfXCZ8uFfzpeIDQBAIhqbDd.mp4` | 44.834 s | 576×576 | single-speaker Goggins monologue | [12 frames](contacts/o0I1EQLyjVjaAkMRfXCZ8uFfzpeIDQBAIhqbDd__contact.jpg) |
| R08 | `oATPD9oIS8GRzQUgIGLqAe4GAcYeANXjefDRyA.mp4` | 21.067 s | 576×576 | single-speaker Goggins monologue + CTA | [12 frames](contacts/oATPD9oIS8GRzQUgIGLqAe4GAcYeANXjefDRyA__contact.jpg) |
| R09 | `ooehMGeIQAeept0QEQFVGgAKNcgsLARQGMnwC2.mp4` | 25.800 s | 576×1024 | single-speaker Markaryan monologue | [12 frames](contacts/ooehMGeIQAeept0QEQFVGgAKNcgsLARQGMnwC2__contact.jpg) |
| R10 | `owgeQKD3jYGHAGMTjWIr8LfrcADIdQ3AofmEC0.mp4` | 6.710 s | 576×1024 | luxury/night B-roll montage | [12 frames](contacts/owgeQKD3jYGHAGMTjWIr8LfrcADIdQ3AofmEC0__contact.jpg) |

## Subtitle truth table

| ID | Native measured placement | Observed cue grammar | Type / effects | Colour system | Side or persistent inscriptions |
|---|---|---|---|---|---|
| R01 | Representative `действие`: `[161,493,415,537)`, centre `(50.0%, 50.3%)`, 44 px observed height. Placement deliberately varies inside the central square from upper third to lower-middle. | 1–8 words, 1–4 lines; mostly 1–3 words per visual beat. | Heavy neo-grotesk, lowercase; no visible hard stroke; 2–4 px black drop shadow. Occasional high-contrast serif statement card. | White; sampled yellow `#F3C610`; sampled red `#E91C1F`. Usually one coloured semantic word. | No side text. No permanent watermark. |
| R02 | Persistent rubric `Цитаты Арсена Маркаряна`: combined `[96,498,480,531)`, centre `(50.0%, 74.1%)`, 28–33 px observed height. | Not speech subtitles: exactly 3 words, one permanent line. | Bold condensed/grotesk; subtle dark shadow, no box. | White prefix + saturated red name, visually near `#E91520`. | Persistent bottom-centre rubric; no side inscription. |
| R03 | Representative red kinetic word `ненавижу`: `[644,216,902,239)`, centre `(75.5%, 39.5%)`, 23 px; other cards range roughly 23–52 px and move left/right/centre. | 1–4 words, 1–3 lines; fragments, not verbatim lower subtitles. | High-contrast serif/italic editorial typography mixed with heavier red display words; generally no outline. | White + dark red; sampled red `#A20004`. | Tiny persistent `VAENORG` watermark bottom-centre, about 11–14 px; no side title. |
| R04 | Persistent sentence `[351,276,672,302)`, centre `(50.0%, 50.2%)`, 26 px observed height. | Exactly 6 words, one line for the whole film: “The only limit is your mind.” | Medium clean sans; no box or hard stroke; 1–2 px soft dark shadow. | White only. | None. |
| R05 | Representative cue `[205,282,368,294)`, centre `(49.7%, 50.0%)`, 12 px cap height. | 1–4 words, one line; slow phrase fragments. | Very thin uppercase sans, no outline; tiny author glyph 15–30 px below the cue. | Off-white only. | Persistent tiny centre glyph, but no side text or name label. |
| R06 | Representative word `[225,275,352,299)`, centre `(50.1%, 49.8%)`, 24 px observed height. | One word per cue, one line; approximately 0.5–1.2 s per word. | Bold italic extended sans; about 2 px black outline plus soft shadow. | White only. | None. |
| R07 | Representative bottom cue `[118,475,457,503)`, centre `(49.9%, 84.9%)`, 28 px observed height; bottom edge at 87.3% frame height. | Usually 1–3 words, one line; occasional two-line cue. | Very bold geometric sans; 2–3 px black outline and compact shadow. | White only. | None. |
| R08 | Representative cues `[242,500,335,521)` and `[226,501,349,517)`, centre near `(50%, 88.5%)`, 16–21 px observed height. | 1–3 words, one line. | Small bold sans; light-grey/white fill, 2 px dark outline/shadow. | Speech captions neutral; final CTA uses sampled green `#108A29`. | No side text. Final 4.1 s is a centred `ЗОНА 1% / ссылка в профиле` card. |
| R09 | Representative white word `[224,514,351,532)`, centre `(49.9%, 51.1%)`, 18 px solid glyph height; glow spreads roughly another 10–18 px. | Usually one word; emphasis cards contain up to 4 words in 2–3 lines. | Bold uppercase sans; no hard stroke; strong horizontal bloom/glow bar and motion smear. | White plus sampled yellow `#F2E01F`, purple-magenta glow around `#D72BE2`, orange-red, and occasional neon green. | No editorial side text. `RØDE` is physical microphone branding, not an overlay. |
| R10 | Persistent sentence `[217,498,355,526)`, centre `(49.7%, 50.0%)`, 28 px total for two lines; about 10–12 px cap height per line. | Exactly 6 words, two lines, unchanged through the 6.7 s clip. | Small regular sans; no outline, only a 1 px dark shadow. | White only. | None. |

## Per-reference forensic notes

### R01 — strongest text/B-roll montage grammar

- **Composition:** black 9:16 canvas containing an approximately 508×507 px rounded-square image (`x≈34…542`, `y≈258…765`, corner radius ≈38 px). The visual occupies about 88% of frame width and 50% of frame height. Text stays inside this square or becomes a full black typographic card.
- **Shot types:** successful-life/solitude B-roll, animated imagery, long shots, medium figures, close details; no mandatory presenter.
- **Rhythm:** 14 major cuts / about 15 shots after transition de-clustering; mean shot 2.75 s. Shortest meaningful holds are about 1.0 s, longer statement shots 5–6 s.
- **Grade:** source-dependent colour with firm contrast, vignette and warm/cool cinematic pushes. The black matte, rounded square and semantic colour word unify otherwise inconsistent assets.
- **Truth:** this is the closest reference for the requested “text + quickly changing successful-life images” mode.

### R02 — speaker compilation, not a caption template

- **Composition:** near-portrait 576×694, speaker fills frame; medium close-up or close-up. Bottom vignette protects a permanent rubric.
- **Shot types:** three Markaryan sources: hooded selfie/night exterior → colourful studio microphone → clean pale interview close-up.
- **Rhythm:** two major cuts at 6.73 s and 17.17 s; segment holds 6.73 / 10.44 / 9.65 s.
- **Grade:** source colours remain visible; dominant dark violet studio and strong bottom vignette.
- **Truth:** useful for speaker recognisability and block length, but it does **not** demonstrate speech subtitles.

### R03 — cinematic editorial montage

- **Composition:** full 16:9 film frame; text deliberately occupies negative space beside faces and bodies, never a fixed lower third.
- **Shot types:** film dialogue medium/wide shots, gym and baseball B-roll.
- **Rhythm:** opening dialogue shots hold roughly 3.7–7 s; central sports escalation uses about ten 1.2–1.4 s shots; flash/glitch transition frames caused clustered detector hits and were counted once.
- **Grade:** strict high-contrast monochrome, crushed blacks, fine grain; only typography is red.
- **Truth:** excellent pacing and tonal reference, but its landscape delivery and moving serif placements are less reusable for automated Russian vertical captions.

### R04 — maximum-speed B-roll loop

- **Composition:** full 16:9 sports B-roll, centred one-line quote.
- **Shot types:** eight motifs—hikers, shoe detail, climber/silhouette, mountain runner, track runner, boxing silhouette, cyclist, gym detail.
- **Rhythm:** exact eight-shot cycle at about 0.50 s per shot, repeated four times across 16 s. Ghosted in-between frames create an intentional strobe/motion-blur pulse.
- **Grade:** cold/action colour, high clarity in highlights, heavy directional blur and temporal ghosting.
- **Truth:** the speed ceiling. Suitable for a 2–4 s acceleration passage, not for an entire spoken montage.

### R05 — slow multi-speaker quote reel

- **Composition:** square canvas with a horizontally composed talking-head band and black breathing room; text is centred directly over the face around 50% frame height.
- **Shot types:** about ten different medium/close talking heads.
- **Rhythm:** 10 source blocks; mean 6.07 s, median about 6.5 s, range roughly 3.0–10.4 s.
- **Grade:** unified monochrome, low saturation, soft vignette, slightly crushed blacks.
- **Truth:** useful for multi-speaker structure, but the 12 px English text is too small for Russian mobile delivery.

### R06 — strongest Russian speaker-montage caption grammar

- **Composition:** square, full-frame close/medium interviews; one kinetic word sits exactly at visual centre.
- **Shot types:** approximately six different speaker shots.
- **Rhythm:** major source changes around 3.87, 5.33, 7.93, 11.38 and 14.53 s; holds 1.5–3.9 s, while the word captions turn over faster.
- **Grade:** very dark monochrome, soft blur/vignette, strong black crush.
- **Truth:** best evidence that the requested montage should use fast, one-word Russian captions and intercut people every 1.5–4 s.

### R07 — strongest bottom-caption monologue grammar

- **Composition:** single Goggins medium shot on a square canvas; body occupies most of the image; caption is anchored near the bottom, not separated into a card.
- **Shot types:** one continuous speaker angle; no editorial cut detected.
- **Rhythm:** picture holds for the full 44.834 s; motion comes from body language and 1–3-word caption replacement.
- **Grade:** monochrome with visible horizontal scanlines, VHS noise, moderate vignette and hard contrast.
- **Truth:** proof that a monologue can feel intense without B-roll or constant punch-ins when texture and captions carry the rhythm.

### R08 — clean colour monologue with one punch-in

- **Composition:** Goggins medium shot for 4.97 s, then close-up until 16.97 s; final centred black CTA card.
- **Rhythm:** only two editorial changes: punch-in at 4.97 s and CTA at 16.97 s. Flash detections around 17 s are one CTA transition, not separate cuts.
- **Grade:** warm interview colour, rich skin tones, dark background, no distressed texture.
- **Truth:** useful for cut economy and bottom subtitle sizing; tonally less “depressive” than the dominant user direction.

### R09 — strongest Markaryan monologue look

- **Composition:** portrait black canvas with the speaker's square/near-square image concentrated in the middle; medium front angle changes to a tighter side angle and returns.
- **Rhythm:** major cuts at about 13.08 s and 19.10 s; holds 13.1 / 6.0 / 6.7 s. Caption turnover is much faster than picture turnover.
- **Grade:** almost monochrome with a restrained cool-violet tint, strong vignette and black crush. Selective caption glow supplies the only bright colour.
- **Truth:** closest reference to the requested Arsen/“depressive motivating” monologue.

### R10 — compressed luxury/night montage

- **Composition:** extremely dark full portrait frame; 6-word message remains centred while architecture, luxury car, jewellery and storm/scenery imagery change beneath it.
- **Rhythm:** major visual changes near 1.17, 1.83, 2.43, about 3.5 and 5.67 s; effective shot length roughly 0.6–1.2 s, with crossfades/slow camera moves rather than hard flashes.
- **Grade:** near-monochrome, dramatically underexposed, deep black crush, soft haze and reflective highlights.
- **Truth:** strongest compact benchmark for “successful success” imagery without a visible speaker.

## Ranked references

### Montage format

1. **R01 — `ogjb…`**: best complete grammar for a vertical text/B-roll motivation edit—intentional black matte, rounded-square media, semantic colour words, 1–6 s shot variation and coherent visual identity across mixed assets.
2. **R06 — `oQM…`**: best Russian multi-person speech montage—different real speakers every 1.5–4 s, very dark monochrome treatment and one-word centred captions.

Supporting benchmark: R04 defines the 0.5 s speed ceiling; R10 defines the darker luxury-B-roll micro-montage.

### Monologue format

1. **R09 — `ooeh…`**: closest subject and mood match—Markaryan, only three picture edits in 25.8 s, centre kinetic one-word captions and selective neon bloom over a crushed monochrome image.
2. **R07 — `o0I1…`**: strongest stable single-speaker template—one continuous Goggins shot, bottom 1–3-word Russian captions and VHS/scanline texture instead of decorative UI.

R08 is the clean alternative when colour footage and a single punch-in are preferred.

## Canonical V3 style rules

There are **two separate templates**. They should not be merged into one editorial design.

### Template A — montage

1. Cut real people or successful-life B-roll every **0.6–3.0 s**; allow a 4–6 s hold only for a decisive spoken setup.
2. For speaker montage, use **1 word per cue**, centred inside the picture, with a 0.5–1.2 s turnover. Intercut speakers every **1.5–4 s**.
3. For text/B-roll montage, use 1–8 words in 1–4 lines, but one semantic word carries the colour.
4. Use footage as the composition. A pure black matte or rounded-square media window is valid; blurred duplicate backgrounds, thin frames and dashboard-like chrome are not part of this reference set.
5. Default grade: high-contrast monochrome or near-monochrome, crushed blacks, vignette/grain. Colour enters through one word or through selectively warm B-roll.

### Template B — monologue

1. Keep the speaker visually dominant. Use **one principal angle**, with no more than 1–3 meaningful punch-ins/cuts across 20–45 s.
2. Subtitle with **1–3 words per cue**. Choose one of two reference-proven placements:
   - bottom: centre around **85–89%** frame height, 16–28 px cap height on a 576 px square;
   - kinetic centre: centre around **50–52%** frame height, 18–24 px solid cap height plus restrained glow.
3. Captions sit over footage. Do not allocate a separate caption card or large dead lower panel.
4. Default grade: monochrome/near-monochrome with scanline, grain, vignette or cool-violet tint. Warm clean colour is an explicit R08 variant.
5. Do not add persistent top-left names, episode numbers, side metadata or corner UI. If identification is needed, use one short bottom-centre rubric once.

## What V2 violated

The comparison target is `factory/pilots/motivation-ru-montage-20260828`.

1. **It invented an editorial UI absent from the references.** V2 adds top-left speaker numbering, top-right `MOTIVE / …`, a red rule, a thin red picture frame and a bottom signature. Nine of ten references have no editorial side/top metadata; the exceptions use a bottom-centre rubric or tiny bottom watermark.
2. **It made the footage too small.** V2's sharp panel is only `1020×574` at `y=292…866` on a 1080×1920 frame—about 30% of frame height. R01's square alone occupies about 50% of portrait height; monologue references let the speaker dominate the square/full frame.
3. **It separated captions from the picture.** V2 reserves an opaque lower band from `y=1238` to the bottom and places phrase cards there. The references place text directly over the subject/B-roll, usually at 50–52% or 85–89% height.
4. **It used phrase blocks instead of kinetic cue grammar.** V2 frequently displays 4–7 words across two lines. The strongest Russian montage/monologue references R06, R07, R08 and R09 use one word or 1–3 words per cue.
5. **Its montage rhythm was structurally slow.** V2 holds three uninterrupted speaker blocks for 10.241 / 6.570 / 12.200 s. Reference montages intercut people every 1.5–6.5 s and B-roll every 0.5–3 s; R06 is the closest requested model.
6. **It used blurred duplicate-video wallpaper.** No audited reference uses a blurred copy of the same footage as a vertical background. They use full footage, a clean black matte, letterboxing or a deliberately rounded square.
7. **Its colour language was too “designed corporate.”** The graphite/off-white/red system is controlled, but the dominant examples are crushed monochrome, scanline/VHS, grain, cool-violet tint or selective neon glow. V2 reads as an editorial documentary package rather than raw/depressive motivation.
8. **It over-explained speaker identity.** Each V2 block introduces a numbered full name and taxonomy. References trust face recognition; R02's bottom rubric is the only prominent identity treatment and remains far simpler.
9. **It confused montage and monologue templates.** Long unbroken speaker blocks follow monologue pacing, while the three-speaker compilation claims montage structure. V3 must choose either fast intercut montage grammar or one-speaker monologue grammar.
10. **Its negative space was inactive.** The gap between the top panel and bottom caption band becomes dead UI space. In the references, black space frames a square image or typography intentionally; it does not separate picture from meaning.

## Final production truth

For the next approval test, make one **R01/R06-derived montage** and one **R09/R07-derived monologue**. Preserve their different pacing and caption systems. Do not reuse V2's framed-panel, top metadata or lower-card visual system.

