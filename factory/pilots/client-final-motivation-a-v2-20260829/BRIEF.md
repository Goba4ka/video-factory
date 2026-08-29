---
workflow: general-video
flow: automation
storyboard: no
message: "Настоящее понимание работы приходит не за месяцы, а за годы"
destination: short-form-social
aspect: 1080x1920
language: ru
audience: russian-speaking motivation audience
length: 29.2s
angle: original-speaker truth
---

## Intent

Пересобрать клиентский мотивационный вертикальный ролик после обратной связи о
плохих субтитрах, лагах и слабой музыке. Визуальный регистр — сдержанный,
депрессивно-мотивирующий, без плакатных блоков и интерфейсного декора.

## Assets

- `assets/video/speaker-cfr-baked.mp4` — единственный H.264 CFR видеомастер с физически запечённым кадрированием и монохромным look.
- `assets/audio/final-mix.m4a` — готовый speech-forward master: оригинальная русская речь плюс лицензируемый phonk-bed.
- `assets/audit/transcript.json` — Faster-Whisper small/ru word timestamps, 89 слов.

## Customizations

- Субтитры переключаются по реальным word timestamps; одна короткая микрофраза, 1–4 слова.
- Caption style: Oswald 700, 56 px, белый/серый, тонкий чёрный stroke/shadow, центр около 54% высоты.
- Только hard punch reframes по смысловым и музыкальным акцентам; никакого постоянного zoom crawl.
- Никаких runtime color-grading shaders и `data-automation`; duck/EQ/fades/master loudness запечены в аудиофайл.

## Notes

- Fish Audio/TTS не используется.
- Без верхних/боковых подписей, логотипов, бейджей и прогресс-бара.
- Право на коммерческую публикацию фрагмента спикера: HOLD до разрешения правообладателя.
- Музыка: Pixabay Content License; Content ID registered, поэтому ledger и подтверждение источника сохраняются.
