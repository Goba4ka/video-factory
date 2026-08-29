# Design — Close Truth V2

Concept angle: зритель находится на расстоянии одного разговора от спикера; кадр
не украшает мысль, а усиливает её точным ритмом и близостью.

## Frame truth

- Canvas: 1080×1920, 30 fps.
- Focal element: лицо и жесты спикера, 80–100% полезной высоты кадра.
- Background: только реальная студия из исходника; look уже запечён в CFR proxy.
- Supporting detail: одна текущая микрофраза; никакого дополнительного chrome.
- Edge anchors: отсутствуют намеренно — пользователь запретил боковые/верхние плашки.

## Typography

- Family: `Oswald`, weight 700; bundled HyperFrames font.
- Caption size: 56 px; короткая длинная фраза может уменьшаться до 50 px.
- Maximum: two visual lines, но предпочтителен один ряд.
- Position: центр caption box на 54% высоты кадра.
- Color: `#F1F0EC`; secondary `#D1D0CC`.
- Separation: `0.8px` dark stroke plus compact black shadow, no background rectangle.

## Motion

- Source-derived hard punches approximately every 1–2 seconds.
- Camera states are instant scale/reframe poses on one GPU-promoted wrapper.
- Caption changes are hard voice-synchronous switches; temporal accuracy wins over decoration.

## Do not

- No red poster blocks, oversized typography, gradients, badges, labels, side copy, or animated grain.
- No continuous camera drift, blur transitions, word glow, or runtime video shader.
