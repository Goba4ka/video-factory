# Video Factory contracts

These JSON Schemas are the stable hand-off boundary between specialist agents.
Every artifact is versioned, auditable, and intentionally independent of a
specific model provider.

Required production chain
-------------------------

`factory/lanes/registry.json` is the only authority for role order. Producers
must not infer a shorter chain from the list of schemas in this directory. The
current common role DAG is:

`research -> [lane review] -> media_discovery -> rights (human) -> media -> script -> voice/source_audio -> editor -> bgm -> audio_mix -> compiler -> preview_review (human) -> render -> qc_auto_evidence -> caption_transcript -> captions_analyzer -> facts_analyzer -> policy_analyzer -> dedup_analyzer -> visual_analyzer -> qc_evidence_gate -> qc -> final_review (human) -> publisher`

The registry selects `sensitivity_review`, `privacy_review`, `medical_review`,
or no lane review, and selects `voice` for narrated lanes or `source_audio` for
motivation. `medical_review`, `rights`, `preview_review`, `final_review`, and
publication authorization are attributable human decisions; autonomous
workers may prepare evidence but may not complete those gates.

The principal artifact milestones for narrated lanes are:

`IdeaCard -> ClaimLedger + SafetyGateReport (when required) -> MediaDiscoveryManifest -> RightsManifest + human approval -> FrozenMediaManifest -> ScriptPackage -> VoiceManifest + VoiceRightsApproval -> ShotList -> BgmManifest -> ProgramAudioManifest -> ProjectManifest -> PreviewApproval -> RenderManifest -> QCAutoEvidenceManifest + CaptionTranscriptManifest + QCAnalyzerReports -> QCEvidenceBundle -> QCReport -> checksum-bound human final review -> PublishManifest`

Motivation replaces `VoiceManifest + VoiceRightsApproval` with
`SourceAudioManifest`; it never enqueues generated voice or TTS. Artifact
names describe hand-offs, not permission to skip an intervening registry role.

Fish retry evidence uses `VoiceDefect`. Any approved voice status also requires a
separate attributable `VoiceRightsApproval`; a VoiceManifest cannot self-approve.

Rules:

- IDs are immutable after creation.
- Every factual claim must reference at least one source ID.
- Every timeline asset must reference one rights entry.
- `MediaDiscoveryManifest` is candidate evidence, never permission. A human
  reviewer must approve the exact canonical `RightsManifest` SHA-256 and every
  reviewed `asset_id` before `FrozenMediaManifest` can be produced.
- The selected BGM is frozen as a separate job-scoped 48 kHz stereo PCM WAV.
  `BgmManifest` binds its source bytes, frozen WAV, local license-evidence bytes,
  exact `RightsManifest`, and attributable human approval by SHA-256. A missing,
  stale, incomplete, or substituted approval/evidence file fails closed.
- `ProgramAudioManifest` binds the authoritative spoken-audio manifest without
  replacing it, binds the exact BGM manifest, and records the deterministic
  FFmpeg recipe. The current profile applies a -9 dB music pre-gain, speech-keyed
  sidechain ducking, and two-pass loudness normalization to -15 LUFS / -1 dBTP.
- HyperFrames receives only the checksum-bound program mix as its audio track;
  every B-roll element remains muted. The dry `VoiceManifest` or
  `SourceAudioManifest` remains the authority for spoken content and timing.
- Every generated voice must bind the approved text hash, model/reference ID,
  immutable WAV hash and generation number; no video may exceed two Fish Audio
  dispatches.
- Motivation never uses generated voice. Its source-audio artifact binds the
  original video range, exact transcript, rights state and three checksums while
  requiring `original_audio_only=true` and `tts=false`. `internal_prototype`
  audio cannot pass publication.
- `PreviewApproval` binds the exact project tree and project manifest before
  rendering. Post-render QC is an evidence DAG: the combined automatic scan,
  word-level transcript, five semantic analyzer reports, immutable evidence
  bundle, and final QC report all bind the same render SHA-256.
- A publish manifest may only be created from a passed QC report and a separate
  checksum-bound human final-review decision for the exact destination
  metadata.
- `needs_human_review` is fail-closed: missing or uncertain evidence sets it to
  `true`.
- URLs and license text are evidence, not permission by themselves. The
  autonomous worker prepares the audit; an attributable human approves or
  blocks the exact RightsManifest and reviewed asset list in every case.

The current schema version is `1.0.0`. Producers must preserve unknown fields
when reading and writing artifacts so contracts can evolve without data loss.
