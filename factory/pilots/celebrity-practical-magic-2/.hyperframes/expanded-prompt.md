# Expanded production prompt

## Style

`Moonlit Editorial Dossier`: фон `#090A0E`, основной текст `#F2E9DA`, лунный
акцент `#D9DEFF`, редкая подтверждающая дата `#E4A853`, вторичный sage
`#758879`. Display — Oswald 700, data/captions — IBM Plex Mono 700. Настроение:
ночная журнальная обложка, киноафиша и проверяемое редакционное досье.

## Rhythm

`HOOK — hold — PROOF — cut — mystery — names — DATES — question — CTA`, девять
сцен на 57.2 секунды. Пиковые моменты: тезис «28 лет спустя», подтверждение
официального сайта и двойная дата проката. Финал замедляется и держит CTA.

## Global rules

- Вертикальный холст 1080×1920; безопасные поля: 72px по бокам, 96px сверху,
  250px снизу.
- Минимум два фокуса и три слоя в каждом кадре: атмосферный фон, факт,
  метаданные/правовая маркировка.
- Motion seek-safe: один paused GSAP timeline на композицию, только `fromTo`,
  `set`, transforms и opacity; никакого autoplay, infinite repeat или random.
- Full-bleed stock отделяется от утверждения плашкой `КИНОКОНТЕКСТ · STOCK`
  там, где зритель может принять кадр за хронику.
- Архивные портреты подписываются `АРХИВ · 2018` и `АРХИВ · 2006`.
- Субтитры крупные, дословные, двухрегистровые; один ключевой фрагмент может
  быть набран Oswald крупнее основного IBM Plex Mono.

## Scene 01 — return hook

**Concept.** Луна уже висит над кадром, будто открывается титр старого фильма.
Два архивных портрета входят с противоположных сторон и становятся парой.

**Mood.** Ночная афиша, тихая мистика, уверенный entertainment lead.

**Depth.** BG — реальная луна и тонкое лунное свечение; MG — тезис `28 ЛЕТ
СПУСТЯ`; FG — две датированные портретные карточки и регистрационная линия.

**Choreography.** Луна медленно PUSHES; заголовок LANDS сверху; портреты SLIDE
и TILT навстречу; архивные метки SNAP на место. Hard cut out.

## Scene 02 — current event

**Concept.** Визуальный язык меняется на карточку события: зритель кинотеатра
остаётся контекстом, а дата и Лондон становятся настоящим содержанием.

**Mood.** Festival bulletin, restrained and sourced.

**Depth.** BG — cinema stock; MG — крупная дата; FG — `ЕВРОПЕЙСКАЯ ПРЕМЬЕРА`,
источник RTE и маркировка stock.

**Choreography.** Дата STAMPS слева, линия DRAWS, заголовок RISES. Hard cut.

## Scene 03 — official proof

**Concept.** Две актрисы оформлены как зеркальные карточки подтверждения,
между ними — не таблоидная молния, а спокойный источник.

**Mood.** Museum dossier meets film press kit.

**Depth.** BG — ink plane + moon glow; MG — split portraits; FG — official
label and the return statement.

**Choreography.** Карточки BOOK-OPEN с mirrored rotateY, источник FADES,
заголовок LOCKS IN. Hard cut.

## Scene 04 — story engine

**Concept.** Пламя свечей становится физическим знаком проклятия; слова
приходят каскадом, как короткое заклинание.

**Mood.** Candlelit suspense, no horror clichés.

**Depth.** BG — candle stock; MG — `ДРЕВНЕЕ ПРОКЛЯТИЕ`; FG — короткая
поясняющая строка.

**Choreography.** Фон DRIFTS, слова WHIP UP по waterfall-правилу, строка
SLIDES from the side. Hard cut.

## Scene 05 — authorship

**Concept.** Книга и лупа превращают кадр в исследовательскую карточку.

**Mood.** Literary editorial, warm paper under moonlit chrome.

**Depth.** BG — book stock; MG — имя режиссёра; FG — источник экранизации.

**Choreography.** Медленный PUSH, имя REVEALS, книжная карточка WIPES in.
Hard cut.

## Scene 06 — cast

**Concept.** Три имени зажигаются одно за другим на пламени свечей.

**Mood.** Festival programme typography.

**Depth.** BG — alternate candle segment; MG — numbered cast list; FG —
small official-cast source.

**Choreography.** Имена CASCADE с бинарным появлением; цифры HOLD янтарным.
Hard cut.

## Scene 07 — release utility

**Concept.** Киноэкран остаётся живым, но полезная информация полностью
принадлежит двум огромным датам.

**Mood.** Premium release calendar.

**Depth.** BG — alternate cinema segment; MG — 09.09 and 10.09; FG — region
labels and official-site source.

**Choreography.** Даты SLIDE from opposing edges, divider DRAWS, labels
SETTLE. Hard cut.

## Scene 08 — emotional question

**Concept.** Луна возвращается, а три жанровых слова складывают формулу
оригинального фильма.

**Mood.** Nostalgic question, moonlit restraint.

**Depth.** BG — alternate moon segment; MG — question; FG — three genre chips.

**Choreography.** Question FOCUSES in; chips WATERFALL; glow BREATHES once.
Hard cut.

## Scene 09 — CTA

**Concept.** Портреты снова образуют пару, но теперь работают как подпись к
вопросу зрителю. CTA остаётся спокойным и редакционным.

**Mood.** Held magazine back cover.

**Depth.** BG — ink + restrained bloom; MG — paired portraits; FG — question,
CTA and channel promise.

**Choreography.** Pair SETTLES, CTA EXPANDS once, progress resolves and frame
HOLDS to the end.

## Recurring motifs

Hairline moon rules, mono source tags, dated archive labels, amber proof marks,
one crescent-like radial bloom per dark scene.

## Negative prompt

No red breaking-news banner, fake paparazzi flash, film/trailer frames without
rights, gradients in headline text, glossy web cards, tiny UI typography,
unlabeled archival imagery, implied documentary footage, rumors, private-life
claims, AI-generated celebrity likenesses or cloned celebrity voices.

