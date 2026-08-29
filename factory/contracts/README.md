# Video Factory contracts

These JSON Schemas are the stable hand-off boundary between specialist agents.
Every artifact is versioned, auditable, and intentionally independent of a
specific model provider.

Required production chain:

Narrated lanes:

`IdeaCard -> ClaimLedger -> RightsManifest -> VoiceManifest -> ShotList -> RenderManifest -> QCReport -> PublishManifest`

Motivation lane:

`IdeaCard -> ClaimLedger -> RightsManifest -> SourceAudioManifest -> ShotList -> RenderManifest -> QCReport -> PublishManifest`

Fish retry evidence uses `VoiceDefect`. Any approved voice status also requires a
separate attributable `VoiceRightsApproval`; a VoiceManifest cannot self-approve.

Rules:

- IDs are immutable after creation.
- Every factual claim must reference at least one source ID.
- Every timeline asset must reference one rights entry.
- Every generated voice must bind the approved text hash, model/reference ID,
  immutable WAV hash and generation number; no video may exceed two Fish Audio
  dispatches.
- Motivation never uses generated voice. Its source-audio artifact binds the
  original video range, exact transcript, rights state and three checksums while
  requiring `original_audio_only=true` and `tts=false`. `internal_prototype`
  audio cannot pass publication.
- A publish manifest may only be created from a passed QC report.
- `needs_human_review` is fail-closed: missing or uncertain evidence sets it to
  `true`.
- URLs and license text are evidence, not permission by themselves. The rights
  agent records a decision and the human editor accepts ambiguous cases.

The current schema version is `1.0.0`. Producers must preserve unknown fields
when reading and writing artifacts so contracts can evolve without data loss.
