---
workflow: general-video
flow: automation
storyboard: no
message: "Личные вещи звёзд превращаются в реальную помощь, а не в очередной инфоповод"
destination: tiktok-reels
aspect: 1080x1920
language: ru-RU
audience: "русскоязычные зрители 18–44, интересующиеся знаменитостями"
length: 25-35s
angle: "curiosity-gap charity news"
voice: piper-ru_RU-denis-medium
---

## Intent

Контрольный вертикальный ролик о свежей и безопасной новости знаменитостей:
Баста, Егор Крид и ещё более двухсот медийных людей передали вещи на
благотворительный VK Звёздный маркет 29–30 августа 2026 года. Хук строится на
контрасте «вещи звёзд продают, но деньги заберут не звёзды».

## Assets

- `assets/images/basta-vkfest.jpg` — лицо Басты, Wikimedia Commons CC BY-SA 4.0.
- `assets/images/egor-kreed-vkfest-cropped.jpg` — портрет Егора Крида, Wikimedia Commons CC BY-SA 4.0.
- `assets/images/ani-lorak-cropped.jpg` — портрет Ани Лорак, Wikimedia Commons CC BY-SA 4.0.
- `assets/images/sergey-burunov-cropped.jpg` — портрет Сергея Бурунова, Wikimedia Commons CC BY-SA 2.0.
- `assets/images/clothing-racks.jpg` — визуальный контекст ресейла, Wikimedia Commons CC BY-SA 4.0.
- `assets/audio/convergence.mp3` — музыкальная подложка Scott Buckley, CC BY 4.0.

## Customizations

- Крупные лица занимают 70–85% видимой площади.
- Субтитры — максимум две строки, 3–5 слов в чанке, без служебных верхних и боковых плашек.
- Хук читается с нулевой секунды; цифры `200+`, `2000+`, `4,6 млн ₽` используются только как доказательство.

## Notes

- Старый Fish-голос запрещён и не используется.
- Новая озвучка: локальный Piper `ru_RU-denis-medium`; датасет модели CC0.
- Только подтверждённые факты из официальных пресс-релизов VK, без слухов и оценок личной жизни.
- Финальный рендер возможен только после утверждения Studio preview.
