# Fish Audio runtime

Validated on Windows on 2026-08-28 against the current Fish Audio REST API.
This runtime is available only to narrated lanes. The `motivation` registry
structurally excludes `voice` and uses checksum-bound original `source_audio`;
do not call Fish Audio for motivation.

## Production contract

- Endpoint: `POST https://api.fish.audio/v1/tts`.
- Default production model: `s2.1-pro`; test fallback: `s2.1-pro-free`.
- Voice: the private Russian model owned by the active Fish workspace, selected
  through the user-scoped `FISH_REFERENCE_ID` setting.
- Raw asset: WAV, 44.1 kHz, 16-bit, mono PCM, `latency=normal`.
- Final video mix: resampled by the render stage to 48 kHz.
- One request always contains the complete approved narration.
- No hidden HTTP retry. Maximum two dispatched synthesis calls per stable
  `video_id` across all chats and working directories.

## Secret storage

The API key is not stored in the repository, OneDrive, command manifests or
factory logs. It is encrypted with Windows DPAPI for the current Windows user at:

`%LOCALAPPDATA%\VideoFactory\Secrets\fish_audio_api_key.dpapi`

The central usage ledger is also outside the project:

`%LOCALAPPDATA%\VideoFactory\State\fish_audio_usage.sqlite3`

An explicit process-level `FISH_API_KEY` can override the DPAPI value for a
temporary session. Never pass a key as a CLI argument.

## Commands

From the repository root:

```powershell
$env:PYTHONPATH = (Resolve-Path 'factory/src').Path
python -m video_factory fish-voices
python -m video_factory fish-auth
python -m video_factory fish-tts `
  --video-id job_000001 `
  --text-file factory/runs/2026-08-28/celebrity_news/job_000001/narration.txt `
  --output factory/runs/2026-08-28/celebrity_news/job_000001/voice.wav
# Generation 2 only, after QA has produced a concrete defect record:
python -m video_factory fish-tts `
  --video-id job_000001 `
  --text-file factory/runs/2026-08-28/celebrity_news/job_000001/narration-v2.txt `
  --output factory/runs/2026-08-28/celebrity_news/job_000001/voice.wav `
  --retry-reason pronunciation `
  --defect-reference factory/runs/2026-08-28/celebrity_news/job_000001/qa/voice-v1.json
python -m video_factory fish-tts-status job_000001
python -m video_factory validate-artifact voice_manifest path/to/voice.voice.json
```

`fish-voices` performs a non-generating authentication check. `fish-tts` reuses
an identical verified result without an API call. Every real dispatch reserves a
slot atomically before network I/O.

The second call's `--defect-reference` must point to a contract-valid
`VoiceDefect` JSON file bound to generation one's request hash, output hash (or
null for provider failure), status, job and retry category. A free-form note,
issue label or nonexistent path is rejected before the API call.

`fish-auth` prompts without echo and replaces the DPAPI-protected key. Use it
after rotating a key; never paste the replacement key into a chat or CLI flag.

## Failure semantics

- Third dispatch for one `video_id`: blocked before network I/O.
- `401/403/402/422`: no automatic retry; the reserved dispatch remains visible.
- timeout, connection reset or interrupted response: `failed_unknown`; it
  consumes a dispatch because the server may have received the POST.
- process crash: a reservation becomes `failed_unknown` after 20 minutes; only
  the one remaining slot may then run.
- generation two: blocked until generation one has finished, and rejected
  without both an allowed `retry_reason` and a concrete `defect_reference`.
- identical successful request: reuse only after immutable file existence,
  SHA-256 and PCM validation.
- cache corruption: mark the old generation `failed_unknown`; only the next
  available slot can regenerate it.

Each successful API response is written to an immutable path:

`<output-dir>/.fish_audio/<video-id>/vNN-<request-hash>.wav`

The requested output path is an active copy. A `*.voice.json` VoiceManifest
binds the text hash, request hash, voice/model, generation number, immutable WAV
hash, technical audio values and estimated cost.

## QA and rights

Before editor handoff, decode the whole WAV and compare an offline transcript to
the approved script, with special checks for names, dates, numbers, duplicate
phrases, omissions, long silence and clipping. A second full generation is
allowed only for a documented defect.

Authentication and a private model do not alone prove commercial publication
rights. Until the user confirms ownership or an applicable commercial license,
the VoiceManifest stays `user_confirmation_required` and the publisher remains
fail-closed. After that confirmation, the voice worker may use
`--voice-rights-status approved_owned_voice` or `approved_licensed_voice`; this
changes the manifest only and never spends another synthesis slot. Completing
the production voice task additionally requires a separate validated
`VoiceRightsApproval` in `result.voice_rights_approval`, attributable to the
person who confirmed ownership or the commercial license. An agent cannot
self-authorize by changing the VoiceManifest status.

## Current account check

- API authentication: passed.
- Owned private Russian TTS voices found: one.
- Paid API balance during the pilot: insufficient (`402`).
- Test fallback `s2.1-pro-free`: passed.
- Pilot raw voice duration: 47.579841 seconds.
- Pilot free-model API cost: `$0.00`.
