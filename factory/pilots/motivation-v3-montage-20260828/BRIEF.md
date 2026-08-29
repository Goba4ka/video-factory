---
workflow: general-video
flow: automation
storyboard: no
message: "Перестань объяснять бездействие — действуй прямо сейчас и выбирай то, чего действительно хочешь"
destination: short-form-feed
aspect: 1080x1920
language: ru
audience: Russian-speaking adults seeking restrained motivational content
length: 25.28s
angle: three-speaker hard-cut montage
---

## Intent

Собрать повторяемый V3-шаблон мотивационной нарезки по визуальной грамматике
R06 + R01: три цельных русскоязычных высказывания, два hard cut между
спикерами, короткие дословные cue и оригинальная речь без синтетической
озвучки.

## Assets

- `assets/source/speaker-01.mp4` — Арсен Маркарян, verified 1920×1080 source range B2, 6.50s.
- `assets/source/speaker-02.mp4` — Радислав Гандапас, verified 1920×1080 source range B1, 8.78s.
- `assets/source/speaker-03.mp4` — Алексей Ситников, verified 1920×1080 source range B3, 10.00s.
- `assets/audio/bed.wav` — fixed Ref04 wide-pulse loop, 97 BPM.

## Customizations

- Narrative order: B2 excuses/«начну завтра» → B1 acting despite constraints →
  B3 freedom as wanting what one truly wants.
- Three uninterrupted speaker blocks: 6.50s + 8.78s + 10.00s = 25.28s.
- Native 1080×1080 subject-aware crops from 1920×1080 sources on a pure black
  1080×1920 matte; square top is `y=420`, with no upscale.
- Exact source wording only. Captions are 1–3 words, one line, 90–112px, placed
  inside the square over the upper body/face.
- High-contrast monochrome, mild canonical grain/vignette, one restrained
  punch-in inside each speaker block.
- No Fish Audio, TTS, cloning, or generated replacement speech.

## Notes

- No names, side/top labels, rubric, numbering, metadata, borders, CTA,
  end-card, caption boxes, or blur-wallpaper.
- Human listen is required before subtitle burn-in sign-off.
- All three video sources and the reference bed remain `permission_required`;
  local QC render is allowed, publication is not.

