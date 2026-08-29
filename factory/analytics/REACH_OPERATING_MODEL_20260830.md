# Reach operating model: 10–15 shorts в день

Проверено по официальным источникам платформ: **2026-08-30**. Этот документ
дополняет [FEEDBACK_LOOP.md](FEEDBACK_LOOP.md) и не меняет
`feedback_policy.json`. Миллионные просмотры нельзя гарантировать: фабрика
увеличивает вероятность охвата через измеримые эксперименты, но распределение
также зависит от интереса к теме, конкуренции, сезонности и персонализации
конкретного зрителя.

## Единица производства и сравнения

- План — **10–15 оригинальных masters/день** по пяти lanes, обычно 2–3 на lane.
  Кросспост одного master на три платформы остаётся одним видео и тремя
  независимыми наблюдениями.
- Дневной портфель следует политике 60/30/10: при 10 masters — 6 проверенных,
  3 смежных, 1 дальний эксперимент; при 15 — 9 / 4–5 / 1–2. Округление
  выравнивается на недельном горизонте.
- Сравнивать только одинаковые `platform + account_id + lane + duration_band +
  canonical_snapshot_hour`; ранняя когорта — последние 10, рабочая — 30–50
  публикаций не старше 90 дней. Межплатформенный общий benchmark запрещён.
- Любой показатель нормировать на нативный знаменатель той же платформы.
  Не подставлять `views`, `reach` или `plays` в `engaged_views`, если источник
  не даёт совместимого определения. Отсутствующая метрика остаётся `null`, а
  автоматическая оценка — `insufficient_cohort`.

## Что измерять на каждой платформе

| Платформа | Hook | Hold | Value / conversion | Важное различие |
| --- | --- | --- | --- | --- |
| TikTok | доступный в TikTok Studio ранний retention/продолжение просмотра; если такого поля нет — только ручная диагностика первых секунд | average watch time и watched-full/finish rate, когда доступны | shares, likes, comments, favorites/saves и follows на 1,000 совместимых просмотров | TikTok официально называет завершение длинного видео сильным сигналом, а watch time — сигналом рекомендаций; captions, sounds и hashtags дают контекст. Не публиковать дубликаты. Creator Search Insights использовать для спроса и content gaps, но не как доказательство будущего охвата. |
| Instagram Reels | падение в самом начале retention chart; average watch time относительно длины | total/average watch time и moment-by-moment retention | shares, saves, follows и comments на 1,000 plays/reach, каждый на собственном нативном знаменателе | Reels Plays включают initial plays **и replays**, поэтому raw plays не равны уникальному reach и не сопоставимы с YouTube engaged views. Для доступных аккаунтов Trial Reels дают метрики примерно через 24 ч и используют первые 72 ч для решения о распространении подписчикам. |
| YouTube Shorts | `Stayed to watch` / `How many chose to view` против swiped away | engaged views, average view duration и average percentage viewed | likes, shares, comments и subscribers на 1,000 engaged views | С 2025-03-31 raw Shorts views считают старт или replay без минимального watch time; для качества использовать сохранённый YouTube показатель `Engaged views`. YouTube прямо называет chose-to-view, AVD и APV сигналами ранжирования Shorts. |

Официальные платформы не публикуют стабильные веса формулы. Поэтому внутренний
score остаётся когортным percentile score:

`0.35 hook + 0.30 hold + 0.20 value + 0.15 conversion`.

Raw views показывать рядом, но не использовать как единственное основание для
решения. Любой recommendation restriction, copyright claim/strike, removal,
ошибка фактов, прав или медицинской безопасности важнее score.

## Окна наблюдения и действие

| Возраст | Что фиксировать | Разрешённое действие |
| --- | --- | --- |
| **1 ч** | доступность поста, policy/copyright events, raw distribution, первые hook/hold-поля | `hold`; немедленный safety hold при событии. Не переписывать формат по малой выборке. |
| **6 ч** | динамику distribution, hook, AVD/APV/completion, ранние shares/saves/comments | `hold`; подготовить диагностическую гипотезу, но не менять вес topic pack. |
| **24 ч** | полный canonical snapshot; retention curve, ранние value/conversion; у Instagram — Trial Reels result, если функция доступна | `hold`; допускается выбрать следующий однофакторный тест, но не объявлять winner. |
| **72 ч** | основной snapshot и percentile ranks внутри сопоставимой когорты | `scale` или `iterate` по правилам ниже; при недостаточной когорте — `hold`. |
| **168 ч** | late-tail distribution, итоговые value/conversion и повторяемость архетипа | подтвердить `scale`, продолжить `iterate` или `retire`; один поздний всплеск не меняет систему сам. |

### Правила решений

- **Hold** — snapshot моложе 72 ч, нет пяти пригодных comparables, отсутствуют
  обязательные `engaged_views + hook + hold`, либо идёт platform review.
- **Scale** — на 72 ч hook выше медианы когорты и хотя бы один из hold/value/
  conversion выше медианы, safety events отсутствуют. Только после human
  approval; максимум два follow-up, каждый с новым фактом, ракурсом, текстом и
  footage. Это eligibility, не обещание результата.
- **Iterate** — пригодная когорта есть, safety чистый, но условие scale не
  выполнено. Выбрать одну причину по retention/engagement и изменить ровно один
  major dimension; максимум две проверяемые гипотезы.
- **Retire** — только на 168 ч и только для архетипа, а не отдельной темы:
  минимум пять сопоставимых outputs, не менее четырёх из последних пяти ниже
  медианы по hook и ни один не выполнил условие scale. Human editor ставит
  30-дневную паузу/нулевой вес; факты и исходные артефакты не удаляются.
- Любой `limited/claim/strike/removed` переводит вариант в safety hold независимо
  от reach. Права, medical/factual gates и human publish approval метриками не
  переопределяются.

## Очередь однофакторных экспериментов

Каждый master получает `experiment_id`, `control_id`, одно изменяемое поле и
предсказание вида «метрика X на 72 ч поднимется относительно p50 когорты».
Одновременные почти идентичные перезаливы запрещены: сравниваются разные
оригинальные истории одного архетипа либо Instagram Trial Reel, если доступен.

| Фактор | Контролируемое изменение | Основная метрика | Что заморозить |
| --- | --- | --- | --- |
| Hook | первая фраза **или** первый визуальный reveal в 0–2 с | TikTok ранний retention; Instagram first retention drop/AVD; YouTube stayed-to-watch | тема, длительность, captions, music, cut density |
| Captions | размер phrase chunk, позиция или один способ акцента | APV/completion и retention dips; затем shares/saves | тот же hook, текст по смыслу, voice, footage rhythm, music |
| Music | одна лицензированная mood family **или** заранее заданный mix level | hold/APV, затем value; не raw views | hook, captions, edit, длительность и loudness master |
| Cut density | один заранее записанный диапазон cuts/min или median shot length | retention curve, AVD/APV и completion | hook, captions, music, narration и сюжет |

При 10–15 masters/день запускать не более одного major experiment на master и
не смешивать результаты пяти lanes. Победивший элемент не копируется буквально:
масштабируется причинная гипотеза, а не исходная фраза, музыка или sequence.

## Официальные основания

- TikTok: [How TikTok recommends videos #ForYou](https://newsroom.tiktok.com/how-tiktok-recommends-videos-for-you?lang=en) — interactions, completion, video metadata и отсутствие прямого преимущества прошлых viral posts; [5 tips for TikTok creators](https://newsroom.tiktok.com/5-tips-for-tiktok-creators?lang=en) — watch time, раннее удержание и многодневное распределение; [TikTok Studio](https://support.tiktok.com/en/using-tiktok/creating-videos/tiktok-studio) — доступные группы analytics; [Creator Search Insights](https://support.tiktok.com/en/using-tiktok/growing-your-audience/creator-search-insights) — search demand, content gaps и search analytics.
- Instagram/Meta: [Reels watch-time insights](https://about.fb.com/news/2023/04/instagram-reels-trending-audio-and-gifts-updates/) — total/average watch time и hook diagnosis; [Replays and retention chart](https://about.fb.com/news/2023/11/new-ways-to-create-content-on-instagram/) — Plays включают replays и доступен retention chart; [Trial Reels](https://about.fb.com/news/2024/12/trial-reels-try-content-non-followers-first-see-what-perfoms-best/) — non-follower test, метрики около 24 ч и оценка первых 72 ч; [Meta 2026 recommendations update](https://about.fb.com/news/2026/01/2026-ai-drives-performance/) — рост доли оригинальных Instagram recommendations.
- YouTube: [Shorts analytics tips](https://support.google.com/youtube/answer/12942217?co=YOUTUBE._YTVideoType%3Dshorts&hl=en) — shown in feed и viewed versus swiped away; [Shorts search and discovery](https://support.google.com/youtube/answer/11914225?co=YOUTUBE._YTVideoType%3Dshorts&hl=en) — chose-to-view, AVD, APV, likes и satisfaction; [metric definitions](https://support.google.com/youtube/answer/12220281?co=GENIE.Platform%3DDesktop&hl=en-GB) — engaged views/stayed-to-watch/AVD/APV; [Shorts view-count change](https://support.google.com/youtube/answer/10059070?hl=en-uk) — raw views против engaged views после 2025-03-31.
