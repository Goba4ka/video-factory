---
name: Shadow Cut Monologue
colors:
  matte: "#000000"
  foreground: "#F4F4F4"
  accent: "#D7B34A"
  outline: "#000000"
typography:
  caption:
    fontFamily: Oswald
    fontSize: 84px
    fontWeight: 700
    letterSpacing: 0.018em
    textTransform: uppercase
rounded:
  none: 0px
spacing:
  squareTop: 420px
  captionCenterY: 1065px
motion:
  energy: restrained
  easing:
    caption: power2.out
    camera: none
  duration:
    captionIn: 0.045
    captionOut: 0.060
  atmosphere:
    - high-contrast-monochrome
    - restrained-vignette
    - restrained-grain
    - faint-scanlines
---

## Overview

Не интерфейс и не рекламная упаковка: один квадрат реального интервью плавает в
абсолютно чёрном вертикальном поле. Визуальный регистр — строгая документальная
черно-белая запись с минимальной аналоговой фактурой. Субтитры находятся внутри
самого изображения и работают как короткие смысловые удары.

## Colors

- Matte — чистый `#000000`, выбран намеренно по утверждённому R09.
- Caption — `#F4F4F4`, не оптический pure white.
- Accent — приглушённое золото `#D7B34A`; только слова `СЕЙЧАС` и `ДИСЦИПЛИНА`.
- Outline/shadow — `#000000` для читаемости на лице, одежде и фоне.

## Typography

Oswald 700, uppercase, 76–88 px в зависимости от длины слова. Это единственный
экспрессивный шрифт. Не использовать второй шрифт, lower-third или metadata.

## Composition

- Canvas: 1080×1920.
- Picture square: 1080×1080 at global `y=420`.
- Source crop: native 1080×1080 from 1920×1080, `x=0,y=0`.
- Caption optical center: global `y=1065`; это грудь/нижняя середина square, не
  пустое нижнее поле.
- Единственные depth layers: black matte → treated footage → caption.

## Motion

- Hard camera pose 1: 1.00×, 0.00–6.50 s.
- Hard camera pose 2: 1.08× at 6.50 s.
- Hard camera pose 3: 1.14× at 12.50 s.
- Camera transform origin stays near the face; no drifting zoom or auto-track.
- Captions use 45–60 ms opacity/y settle only; no bounce, karaoke or glow.

## Do's and Don'ts

- Do preserve the exact square/matte geometry and the real speaker.
- Do keep grain, vignette and scanlines visible only as texture.
- Don't add blurred background, borders, labels, name, number, logo, CTA or end card.
- Don't add side/top copy or place captions in the empty lower matte.
- Don't use chromatic aberration, glitches, gradients or animated decoration.
