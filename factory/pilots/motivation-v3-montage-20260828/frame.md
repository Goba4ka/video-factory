---
canvas: "1080x1920"
background: "#000000"
foreground: "#F5F3EE"
accent: "#F4D116"
outline: "#050505"
font_display: "Arial Narrow Bold"
font_weight: 900
caption_size: "82px"
caption_min: "74px"
caption_max: "84px"
square: "x=0 y=420 w=1080 h=1080"
grade: "mono-clean"
---

# V3 Montage — visual truth

**Concept angle:** не интерфейс и не постер, а найденный момент внутреннего
напряжения: лицо занимает нативный квадрат, слова появляются непосредственно
внутри высказывания, а пустой чёрный matte усиливает паузу.

## Frame geometry

- Focal element: лицо говорящего внутри нативного 1080×1080 crop.
- Supporting element: одна текущая subtitle cue поверх лица.
- Background: чистый `#000000` matte вокруг квадрата; без свечений, паттернов,
  размытой копии видео и декоративного UI.
- Square: `top: 420px`, без границы, скругления, тени или подписи.
- Edge anchors: только физические границы квадрата; никаких нарисованных
  направляющих.

## Typography

- `Arial Narrow Bold`, embedded, weight 900; fallback `Arial Black`, затем sans-serif.
- 82 px nominal (74 px for long cues), line-height 0.92, tracking -0.035em,
  uppercase, slight synthetic italic.
- Не больше трёх слов и одной строки на cue.
- Белый `#F5F3EE`; жёлтый `#F4D116` только для двух кульминационных cue.
- 5 px чёрная обводка + плотная локальная тень; никаких плашек.
- Центр cue — global `y=960`, внутри квадрата и ниже линии глаз; текст не
  перекрывает лоб и остаётся читаемым на груди/центре кадра.

## Media treatment

- Offline-baked CFR30 H.264/SDR picture proxies: saturation 0 with restrained
  contrast and blacks. Runtime `data-color-grading` intentionally removed so
  the first decoded frame at each hard cut is already monochrome.
- Никакого destructive vertical cover: горизонтальный источник остаётся
  1920×1080, квадрат вырезается через `overflow:hidden` без upscale.

## Motion

- Hard cuts only.
- Caption cue: 0.14–0.18s kinetic hit, затем спокойный hold.
- Три punch-in по 5–6%, без тряски и без постоянного Ken Burns.
- Никакой общей заставки и никакого затемнения в финале.
