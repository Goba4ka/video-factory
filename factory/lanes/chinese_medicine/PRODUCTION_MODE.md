# Production mode — `chinese_medicine`

Статус: **ARMED / WAITING**  
Ключевая команда пользователя: **«начинаем»**

## Обязательный контракт

После команды «начинаем» перед любым исследованием или производством обязательно полностью прочитать и применять:

1. `TOPIC_PACK.json`
2. `EDITORIAL_PLAYBOOK.md`
3. `SOURCE_POLICY.md`
4. `MEDICAL_SAFETY.md`

Эти файлы являются fail-closed контрактом линии. Противоречащая им идея, claim, сценарий, ассет или монтаж блокируются.

## Протокол команды «начинаем»

### Gate 1 — slate

- Найти и перепроверить актуальные либо evergreen-темы.
- Для медицинских утверждений использовать иерархию из `SOURCE_POLICY.md`: актуальные клинические руководства, WHO/национальные регуляторы, систематические обзоры и качественные первичные исследования.
- Показать пользователю короткий slate из 3–5 тем.
- Для каждой темы дать: хук, суть, 1–3 основных источника, evidence label, медицинский/редакционный риск и причину выбора.
- Не начинать производство до явного ответа пользователя по темам (`да` / `нет` или эквивалентный выбор).

### Gate 2 — registry-controlled производство

После подтверждения тем провести 2–3 ролика по точному
`chinese_medicine.roles` из `factory/lanes/registry.json`. Пользовательское
одобрение темы не заменяет human medical, rights, preview и final-review gates.

Канонический DAG:

`research -> medical_review (qualified human) -> media_discovery -> rights (human) -> media -> script -> voice -> editor -> bgm -> audio_mix -> compiler -> preview_review (human) -> render -> qc_auto_evidence -> caption_transcript -> captions_analyzer -> facts_analyzer -> policy_analyzer -> dedup_analyzer -> visual_analyzer -> qc_evidence_gate -> qc -> final_review (human) -> publisher`

- Обязательная точка входа для каждого видео: skill `hyperframes`.
- Для композиции, анимации, CLI/render loop и медиа использовать предписанные HyperFrames skills после маршрутизации основным skill.
- Для любого изображения, видео, музыки, SFX, голоса, captions/transcription или иного media asset обязательно использовать skill `media-use` и его ledger/freeze workflow.
- Каждый ролик — отдельная проверяемая производственная единица; запрещено скрывать незавершённый gate общим статусом пакета.
- Каждая роль получает checksum-bound output непосредственного
  predecessor; перескакивать через роли или менять их местами запрещено.

### Gate 3 — медиа и права

- Замораживать локально только owned, licensed, public-domain или совместимый Creative Commons/permission-контент с item-level доказательством прав.
- Публичная ссылка не является лицензией.
- Не использовать скачанные ролики платформ, новости, кино/ТВ, водяные знаки, `NC` в монетизируемом производстве или `ND` для изменённого монтажа.
- Для каждого ассета сохранить origin URL, автора/правообладателя, license basis, условия коммерческого использования и модификации, атрибуцию, дату доступа, локальный rights receipt и checksum.
- `media_discovery` готовит кандидатов и provenance, но не даёт разрешение.
- Media freeze начинается только после атрибутированного человеческого
  одобрения exact RightsManifest SHA-256 и всех `asset_id`.
- Любой неясный asset имеет статус blocked и не попадает в render.

### Gate 4 — обязательные артефакты каждого ролика

- IdeaCard / зафиксированный выбор темы;
- source bundle и ClaimLedger;
- MediaDiscoveryManifest;
- RightsManifest и его checksum-bound human approval;
- FrozenMediaManifest;
- финальный сценарий;
- VoiceManifest и VoiceRightsApproval;
- captions/subtitles;
- ShotList;
- BgmManifest и ProgramAudioManifest;
- HyperFrames project/composition;
- ProjectManifest и human PreviewApproval;
- RenderManifest;
- QCAutoEvidenceManifest, CaptionTranscriptManifest, пять semantic
  QCAnalyzerReport и QCEvidenceBundle;
- QCReport с выполненными blocking checks;
- checksum-bound human final-review decision;
- контрольный MP4;
- SHA-256 checksums контрольного MP4 и критических manifests.

### Gate 5 — безопасность

- Медицинские, приватностные и военно-исторические safety gates работают fail-closed.
- Никаких диагнозов, дозировок, отмены/замены лечения, персональных назначений, детокс-обещаний или замены врача.
- Красные флаги всегда сопровождаются прямым советом немедленно обратиться за срочной профессиональной помощью.
- Каждый ролик требует атрибутированного qualified-human
  `medical_review` с identity, qualification, timestamp, note и привязкой к точному
  артефакту; автономный agent может лишь подготовить evidence.
- Чувствительные персональные данные, уязвимые люди и реальные пациенты не используются без законного основания, согласия и отдельного privacy review.
- Военно-исторические claims требуют надёжных первичных/академических источников, точного контекста, отсутствия оперативно опасных деталей и отдельной проверки достоинства/вреда.

### Gate 6 — доставка, не публикация

- Контрольный MP4 считается готовым к review только после полного
  evidence QC DAG и checksum-bound human `final_review`.
- Запрещено публиковать, загружать, планировать или отправлять ролики во внешние социальные сети без отдельного явного разрешения пользователя после review.

## Каталог запуска

Все файлы производства складывать только в:

`factory/runs/YYYY-MM-DD/chinese_medicine/`

Рекомендуемая вложенность:

```text
factory/runs/YYYY-MM-DD/chinese_medicine/
  slate/
  <video-id>/
    research/
    script/
    media/
    rights/
    hyperframes/
    captions/
    manifests/
    qc/
    renders/
    checksums/
  RUN_STATUS.md
```

Запрещено изменять общие schemas/CLI, чужие lane-папки или внешние publish-настройки в рамках этого режима без отдельного явного поручения.

## Текущее состояние

Производство не начато. Slate не сформирован. Медиа не загружались. Внешних публикаций не было. Линия ожидает точную команду пользователя: **«начинаем»**.
