# Reference quality bar

The factory does not claim that a render is "reference quality" unless both the
automatic checks and the editorial review pass. The two supplied references
define two valid pacing profiles, not one mandatory template.

## Shared acceptance criteria

- Canvas: 1080x1920, 9:16, square pixels, BT.709, 30 fps target.
- Duration: normally 60-80 seconds.
- The central paradox or conflict is understandable by 1.5 seconds; hard upper
  limit 3 seconds.
- One self-contained message; no listicle padding or generic CTA.
- Original Russian narration; claims map to the claim ledger.
- Captions use one or two lines, 2-7 words per event, high contrast, and stay in
  the center safe zone away from platform chrome.
- Every timeline asset exists in the rights manifest with an approved decision.
- No foreign-platform watermark, black frame, frozen tail, clipped speech, or
  missing caption event.
- Final integrated loudness target: -16 to -14 LUFS, true peak <= -1 dBTP.
- A contact sheet and QC report are generated for every final candidate.

## Pacing profiles

### `human_contrast_fast`

Based on the David Beckham reference: a recognizable person, status or public
expectation, an unexpected ordinary behavior, evidence, personal explanation,
and a human payoff.

- 165-190 Russian words.
- 140-160 words/minute.
- 22-32 shots; median shot length 1.8-3.2 seconds.
- 50-70 caption events, usually 2-5 words.
- 10-25% of caption events may use one semantic accent color.

### `wonder_mystery_slow`

Based on the 52-hertz whale reference: atmospheric setup, immediate paradox,
one numerical comparison, evidence, competing explanations, and a poetic but
fact-safe ending.

- 115-145 Russian words.
- 95-120 words/minute.
- 7-14 shots; median shot length 5-11.5 seconds.
- 38-55 caption events, usually 3-7 words.
- Visual motion inside the shot must remain perceptible when cuts are slow.

## Editorial gate

Automatic compliance is necessary but insufficient. The human editor answers:

1. Would the first frame stop a feed scroll without misleading the viewer?
2. Does each visual add evidence, specificity, or emotion rather than merely
   matching a keyword?
3. Is there a real narrative turn around 35-60% of the runtime?
4. Does the final line pay off the opening instead of dissolving into a generic
   moral?
5. Is this recognizably different from the last 30 outputs in hook, structure,
   and visual silhouette?

A "no" blocks publication even if every technical check passes.

## V3 gate from the latest motivation and lane-master audit

Technical 1080x1920 compliance is not a style pass. The latest audit found
11-16 Mbps masters around -14.5 LUFS with acceptable peaks, but also found
repeatable editorial defects. The following rules are therefore hard review
items for every new server render:

- no side labels, source badges, topic badges, or permanent top straps;
- the first semantic image starts at 0.0 seconds; no logo/title-card intro;
- speaker-led motivation keeps the recognizable face/body at 70-85% of the
  useful picture area and does not hide it behind long B-roll passages;
- the hook/verdict is understandable by 2.0 seconds; a 3.7-second reveal is too
  late for the control profile;
- captions use at most two lines and normally 3-5 Russian words per card;
  default type is 64-84 px at 1080-wide output and must not exceed 90 px without
  an explicit reference match;
- full-bleed source media must be at least 720p and preferably 1080p. Lower
  resolution archive is framed, treated as archive, or replaced; 648x480 is not
  enlarged edge-to-edge;
- the music bed stays approximately 8-12 LU below intelligible speech during
  voiced sections. A measured 18-29 LU gap is considered inaudible music, not a
  successful mix;
- cuts in the first six seconds must carry continuous visual or speaker motion;
  a static opening longer than 0.35 seconds needs a deliberate story reason;
- commercial music, speech clips, fonts, and imagery require item-level rights
  evidence. A platform URL is not a license.

The latest six masters remain useful technical controls, but they are not a
visual-style gold set until these defects are corrected. Semantic/visual QC must
evaluate this section in addition to FFmpeg stream, loudness, black, freeze, and
silence checks.
