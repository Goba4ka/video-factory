# Статус lane «История войн»

Дата проверки: 2026-08-27  
Статус: **готово к человеческому editorial review**

## Созданный пакет

Все материалы находятся только в `factory/lanes/war_history/`:

- `TOPIC_PACK.json` — версия `war_history@1.0.0`, границы темы, 7 hook archetypes, 6 сценарных форматов, source/evidence/visual rules, риск-таксономия и production gates;
- `EDITORIAL_PLAYBOOK.md` — критерии темы, разнообразие пула, драматургия 60–80 секунд, русский стиль, числа, цитаты, историографические споры, визуальный язык и финальный чек-лист;
- `SOURCE_POLICY.md` — иерархия источников, минимальная доказательность по типам утверждений, provenance, переводы, числа, пропаганда как источник, права и условия блокировки;
- `SAFETY.md` — запрет пропаганды, героизации, дегуманизации, графики и современных dual-use инструкций; green/yellow/red review;
- `candidate_pool.json` — 24 русскоязычные идеи с хуком, сообщением, периодом, визуальной гипотезой, риском, research notes и 50 starter sources.

Общие `factory/contracts/`, `factory/src/`, CLI, схемы и чужие lane-папки не изменялись.

## Состав пула

- Идей: **24** (требование: минимум 20).
- Starter sources: **50**, не менее двух на идею.
- Все source URL используют `https://`.
- Source types в пуле: только `primary`, `official`, `academic`.
- Периоды: Римская Британия, Средневековье, XVI–XVII века, XIX век, Первая и Вторая мировые войны, послевоенная память.
- Углы: повседневность, материальная культура, медицина, труд, гражданские права, беженцы, археология, архивы, сохранение искусства и мемориализация.
- Красных кандидатов нет. Чувствительные истории помечены `yellow` и требуют человеческого review.

Основные институциональные семейства источников: British Museum / Roman Inscriptions of Britain, Bayeux Museum, Royal Armouries / The Met, Mary Rose Museum, Vasa Museum, The National Archives (UK), Wellcome Collection, NOAA, U.S. National Park Service, Imperial War Museums, National Army Museum, Europeana, Smithsonian, GCHQ / Bletchley Park, U.S. National Archives, USHMM, Hiroshima Peace Memorial Museum и Hiroshima National Peace Memorial Hall.

Starter source означает только фактическую осуществимость. Перед сценарием обязателен полный claim ledger; перед монтажом — отдельное item-level rights clearance. Публичная музейная страница не считается лицензией изображения.

## Машинная проверка

Проверки выполнены после создания файлов.

| Проверка | Результат |
|---|---:|
| `TOPIC_PACK.json` через PowerShell `ConvertFrom-Json` | PASS |
| `candidate_pool.json` через PowerShell `ConvertFrom-Json` | PASS |
| Оба JSON через Python `json.tool` | PASS |
| Загрузка пула текущим `video_factory.validators.load_ideas` | PASS, 24 идеи |
| Уникальность candidate ID | PASS, 0 дублей |
| Уникальность source ID | PASS, 0 дублей |
| Обязательные поля кандидатов | PASS, 0 пропусков |
| Ссылки на существующие hook archetypes | PASS |
| Ссылки на существующие script variants | PASS |
| Значения `risk` и `rights_feasibility` | PASS |
| Не менее двух источников на идею | PASS |
| HTTPS для всех 50 starter sources | PASS |
| Placeholder и insecure-link scan | PASS, 0 совпадений |

## Интеграционная заметка

Миграция общей фабрики завершена 2026-08-27: `idea_card.schema.json`, `daily_batch.schema.json`, lane registry и auto role routing теперь включают `war_history`. Выбранный кандидат перед производством нормализуется в канонический IdeaCard, после чего обязательны claim ledger, sensitivity review и item-level rights manifest.

## Следующий gate

Человек-тематический редактор выбирает кандидатов из `shortlist`/`hold`. Для выбранной идеи исследователь открывает все starter sources, добавляет независимую академическую проверку там, где это отмечено в `research_notes`, строит claim ledger и запускает sensitivity, dual-use и rights review. Автоматической публикации пакет не разрешает.
