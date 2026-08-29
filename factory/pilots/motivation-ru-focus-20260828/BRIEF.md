---
workflow: general-video
flow: automation
storyboard: no
message: "Ты уже знаешь достаточно — перестань собирать знания и начни делать"
destination: social-vertical
aspect: 1080x1920
language: ru
audience: "русскоязычные зрители мотивационного short-form контента"
length: 22-30s
angle: intimate-dark-monologue
---

## Intent

Один сильный русскоязычный спикер из нового официального интернет-первоисточника. Оригинальная речь должна ощущаться как личный вызов: меньше потребления информации, больше действия. Визуально — дорогой, тёмный, интимный cinematic focus с лицом в кадре, редкими панч-инами и эмоциональным crescendo.

## Assets

- Новый официальный ролик Оскара Хартманна — A-roll и оригинальная русская речь; точный URL и диапазон фиксируются в `MEDIA_LEDGER.json`.
- Scott Buckley, “The Long Dark” — лицензируемый реальный музыкальный трек CC BY 4.0; обязательная атрибуция фиксируется в ledger и credits.

## Customizations

- Русские burned-in субтитры с минималистичным акцентом на ключевых словах.
- Контролируемый кроп лица, slow push-in и два смысловых punch-in, без искусственного face tracking.
- Voice-forward mix с carve/ducking музыки, эмоциональным ростом, целевой громкостью −14.5…−15 LUFS и true peak не выше −1.2 dBTP.

## Notes

- Не использовать Fish Audio, TTS, Jocko, материалы предыдущего focus-пилота или старые пользовательские референсы.
- Для речи ставить `permission_required`, если страница не содержит явной reuse-лицензии.
- Не отправлять результат в Telegram: только подготовить master и Telegram-копию для финального QA root.
