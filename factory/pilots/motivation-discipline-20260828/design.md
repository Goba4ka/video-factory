---
name: Shadow Cut — Human Resolve
colors:
  primary: "#08090B"
  on-primary: "#F1F0EB"
  surface: "#242528"
  muted: "#A8A7A1"
  accent: "#B51F2A"
typography:
  captions:
    fontFamily: Oswald
    fontSize: 66px
    fontWeight: 700
    lineHeight: 1.08
    letterSpacing: 0.005em
  thesis:
    fontFamily: Oswald
    fontSize: 112px
    fontWeight: 700
    lineHeight: 0.96
rounded:
  none: 0px
  media: 34px
spacing:
  captionInset: 88px
  safeBottom: 330px
  edge: 54px
motion:
  energy: moderate
  entrance: "power3.out"
  transition: "blur-crossfade"
  atmosphere:
    - canonical-vignette
    - deterministic-film-grain
---

## Overview

Тёмный кинематографичный минимализм, построенный вокруг человеческого лица и
ритма реплик. Не интерфейс, а собранный вертикальный короткометражный монтаж.

## Colors

Фон — тёпло-чёрный `#08090B`, текст — молочный `#F1F0EB`. Красный `#B51F2A`
используется только на словах «дисциплина» и в финальном тезисе.

## Typography

Один выразительный узкий гротеск Oswald. Субтитр обычно 1–4 слова и одна строка;
две строки допускаются только для короткой законченной фразы. Белая буква с
тонкой тёмной обводкой и мягкой тенью, без плашки.

## Elevation

Плоский кадр. Глубина — внутри исходного видео, канонической виньетки и
детерминированного зерна. Карточки, панели и UI-тени запрещены.

## Components

- Full-bleed speaker/B-roll video, перекадрированный в 9:16.
- Caption pulse в оптическом центре или на безопасной нижней трети.
- Один финальный thesis card на тёмном поле.

## Do's and Don'ts

- Do: hard cuts на речевых ударениях, один мягкий blur crossfade на смене тезиса.
- Do: один акцентный цвет за cue.
- Don't: боковые подписи, имена, HUD, таймкоды, логотипы, прогресс-бары.
- Don't: неон, градиентный текст, лишние glitch-переходы и «успешный успех».
