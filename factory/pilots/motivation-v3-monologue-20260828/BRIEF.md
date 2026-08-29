---
workflow: general-video
flow: automation
storyboard: no
message: "Время исчезает; свобода возможна только через внутреннюю дисциплину сейчас"
destination: telegram
aspect: 1080x1920
language: ru
audience: русскоязычная аудитория коротких мотивационных видео
length: 18.7s
angle: monologue
style_preset: shadow-cut-monologue
---

## Intent

Сделать сдержанный, депрессивно-мотивирующий вертикальный монолог из реального
интервью Ирины Хакамады. Спикер и её речь — единственный смысловой центр; монтаж
должен усиливать мысль, не превращая кадр в рекламный шаблон.

## Assets

- `factory/research/v3-sources/khakamada-1080-304.460-326.500.mp4` — единственный видеоматериал; использовать локальные 00:00.000–00:18.700.
- `factory/research/v3-sources/qc/khakamada-v3-style.png` — утверждённый контрольный кадр R09.
- `factory/research/motivation-references-v3-audio/candidates/reference/reference-bed-01-dark-bass-70bpm.wav` — exact music bed; права `permission_required`.

## Customizations

- Native square crop 1080×1080 из исходного 1920×1080: `x=0,y=0`, без upscale.
- Square расположен в чистом чёрном matte 1080×1920 примерно на `y=420`.
- High-contrast B&W, сдержанные vignette, grain и scanline.
- Два discreet hard punch-in около 6.5 s и 12.5 s; максимум 1.14×, лицо остаётся в кадре.
- Субтитры 1–3 слова прямо поверх нижней/средней части square, global `y≈1030–1130`.
- Oswald 700, 76–88 px, белый с чёрным outline/shadow; muted yellow только для `СЕЙЧАС` и `ДИСЦИПЛИНА`.
- Speech duck/carve; master loudness target -14.5 LUFS-I, true peak ≤ -1.2 dBTP.

## Notes

- Никаких blurred backgrounds.
- Никаких боковых или верхних надписей, рамок, имени, номера, логотипа, CTA или end card.
- Никакого Fish/TTS: используется только оригинальная речь источника.
- Финальный master: 1080×1920, H.264 CRF 16–17.
- Telegram copy: 720×1280, H.264 CRF 20–21.
- Финальный render явно разрешён пользователем в задаче.
