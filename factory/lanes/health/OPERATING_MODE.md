# Операционный режим производственной линии «Здоровье»

**Lane:** `health`  
**Статус:** armed / ожидание команды  
**Ключевая команда запуска:** `начинаем`

## 1. Триггер

Команда пользователя **«начинаем»** запускает новый производственный цикл health. Регистр букв и окружающие пробелы несущественны, но запуск не выводится из похожих фраз и не происходит автоматически.

До этой команды запрещены поиск slate для конкретного цикла, написание сценариев, заморозка медиа, генерация, монтаж и рендер.

## 2. Обязательный контракт каждого цикла

Перед любым исследованием и производством перечитать полностью:

1. `factory/lanes/health/TOPIC_PACK.json`;
2. `factory/lanes/health/EDITORIAL_PLAYBOOK.md`;
3. `factory/lanes/health/SOURCE_POLICY.md`;
4. `factory/lanes/health/MEDICAL_SAFETY.md`.

Если файлы отсутствуют, не читаются, противоречат друг другу или не позволяют безопасно продолжить, цикл блокируется fail-closed. Более строгая граница имеет приоритет.

## 3. Slate и подтверждение пользователя

После команды запуска:

1. Перепроверить актуальные и evergreen-темы по источникам, допустимым `SOURCE_POLICY.md`.
2. Проверить свежесть, применимость, юрисдикцию и отсутствие дублей.
3. Показать короткий slate из **3–5 тем**. Для каждой темы указать:
   - короткое название и угол подачи;
   - почему тема evergreen или почему актуальна сейчас;
   - 1–3 канонических источника;
   - риск `green` или `yellow`;
   - явную границу совета.
4. Получить от пользователя ответ `да`/`нет` по темам.

`Да` означает разрешение поставить выбранную тему в registry-controlled pipeline,
но не заменяет human medical, rights, preview, final-review и publication decisions.
`Нет` исключает тему из текущего цикла. Если одобрено меньше двух, предлагается
замена, а производство не начинается.

## 4. Производство

Для каждой одобренной темы провести один вертикальный ролик длительностью
60–80 секунд по точному `health.roles` из `factory/lanes/registry.json`. На один цикл —
**2–3 контрольных MP4**.

Канонический DAG:

`research -> medical_review (qualified human) -> media_discovery -> rights (human) -> media -> script -> voice -> editor -> bgm -> audio_mix -> compiler -> preview_review (human) -> render -> qc_auto_evidence -> caption_transcript -> captions_analyzer -> facts_analyzer -> policy_analyzer -> dedup_analyzer -> visual_analyzer -> qc_evidence_gate -> qc -> final_review (human) -> publisher`

Роли не переставляются и не пропускаются. Музыка проходит отдельный
rights-bound `bgm`, затем `audio_mix`; compiler получает одну program mix и глухой
B-roll. Рендер требует checksum-bound human PreviewApproval. Весь post-render
evidence DAG и human final review привязываются к одному render SHA-256.

Для видео обязательны актуальные инструкции skills:

- `hyperframes` как входная точка;
- `media-use` для поиска, лицензирования, генерации и заморозки медиа;
- релевантные HyperFrames core/creative/animation/CLI skills;
- `general-video` либо более специализированный workflow, если его условия подходят лучше.

Конкретный набор перечитывается в начале производственной стадии. Skill не может ослабить медицинские, privacy, rights или публикационные ограничения этого режима.

## 5. Каталог цикла

Все новые материалы складываются только в:

`factory/runs/YYYY-MM-DD/health/<run_id>/`

Внутри каждого ролика должны сохраняться как минимум:

- `research/claim_ledger.json` — тезисы, источники, ограничения и дата проверки;
- `safety/medical_review.json` — решение qualified human с identity,
  qualification, timestamp, note и binding к точному artifact;
- `media/media_discovery_manifest.json`;
- `rights/rights_manifest.json` и checksum-bound human approval с полным
  списком `asset_id`;
- `media/frozen_media_manifest.json`;
- `script/SCRIPT.md` или эквивалентный утверждённый сценарий;
- `captions/captions.srt` или эквивалентный caption artifact;
- `audio/voice_manifest.json`, `audio/voice_rights_approval.json`,
  `audio/bgm_manifest.json`, `audio/program_audio_manifest.json`;
- `compiler/render_project_manifest.json` и `compiler/preview_approval.json`;
- `render/render_manifest.json`;
- `qc/qc_auto_evidence_manifest.json`, `qc/caption_transcript_manifest.json`,
  `qc/analyzers/`, `qc/qc_evidence_bundle.json`;
- `qc/qc_report.json`;
- `review/final_review.json`;
- `checksums/SHA256SUMS.txt`;
- `renders/<slug>_control.mp4` — контрольный, не опубликованный MP4.

Дополнительные рабочие файлы HyperFrames, storyboard, аудио, промежуточные проверки и snapshots остаются в том же run-каталоге.

## 6. Media и rights

- Замораживать только owned, licensed, public-domain, permission-based или совместимый Creative Commons контент.
- Для каждого файла должна существовать проверяемая запись в rights ledger до финального render gate.
- Discovery-манифест не является разрешением; media freeze блокируется
  без human approval exact RightsManifest SHA-256 и всех `asset_id`.
- Не использовать поисковый preview, watermark, непроверенный repost, неизвестную музыку, случайный UGC или «fair use» по умолчанию.
- Условия атрибуции и ограничения на переработку переносятся в ledger и manifest.
- AI-генерация маркируется и не используется как доказательство медицинского результата, симптома или реального пациента.
- Если права не подтверждены, ассет не попадает в контрольный MP4.

## 7. Fail-closed safety gates

Следующие гейты обязательны и не могут быть перекрыты качественным score:

### Medical

Проверяются персональная диагностика, препараты/дозировки, обещания результата,
причинность, уязвимые группы, юрисдикция, экстренные признаки и явная граница
совета. `medical_review` выполняет только квалифицированный атрибутированный
человек; автономный agent может лишь подготовить evidence. Любой незакрытый
медицинский риск блокирует тему или render.

### Privacy

Запрещены лишние персональные и медицинские данные, идентификация частных лиц, неразрешённые лица/голоса, реальные документы пациентов и вывод о здоровье по внешности. Согласие и минимизация данных обязательны.

### Военно-исторический контекст

Если тема, визуал или источник пересекается с войной, военной историей, действующими конфликтами, оружием, жертвами или пропагандой, требуется отдельная проверка контекста, фактов, достоинства, прав и риска вреда. Неясность означает блокировку, а не творческое допущение.

### Rights и technical QC

Отсутствие rights evidence, claim traceability, корректных captions, любого
producer/analyzer report, immutable QCEvidenceBundle, final QC или checksum-bound human
final review блокирует выдачу MP4 как готового результата.

## 8. Публикация

Контрольные MP4 предназначены только для внутреннего review. Запрещено загружать, планировать или публиковать ролики во внешние соцсети, облачные публичные страницы и каналы без отдельного явного разрешения пользователя на конкретную публикацию.

Одобрение темы, сценария или контрольного MP4 не равно разрешению на публикацию.

## 9. Текущее состояние

Режим зафиксирован. Производственный цикл не запущен. Следующее ожидаемое действие пользователя: **«начинаем»**.
