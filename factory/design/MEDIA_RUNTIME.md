# Media / audio / render runtime — Windows readiness and bootstrap

Audit date: **2026-08-27**  
Host: Windows 11 Pro x64  
Mode used for this audit: read-only. No package was installed, upgraded or authenticated; no project was initialized and no render was started.

## Verdict

| Capability | Status | Evidence / blocker |
|---|---|---|
| Native HyperFrames check/preview prerequisites | **Partial** | Node 22 and Chrome are ready; there is no initialized HyperFrames project or project pin. |
| Native local render | **Blocked** | `ffmpeg` and `ffprobe` are not on `PATH`. HyperFrames explicitly reports that render cannot proceed. |
| Deterministic HyperFrames CLI | **Blocked** | Only stale `_npx` cache entries exist. Newest cached CLI is `0.7.88`; doctor reports current `0.8.13`. No `package.json`, lockfile or `hyperframes.json` exists in the repository root. |
| Frozen licensed media ingest | **Ready at code level** | The factory has an approved-manifest-only downloader with SHA-256 and an atomic ledger. Production media still requires a passed RightsManifest. |
| Bundled SFX | **Ready** | `media-use --doctor` reports 19 bundled SFX assets. |
| HeyGen catalog / TTS / image / avatar path | **Blocked** | `heygen` is absent, no HeyGen environment-variable names or known auth directory was found, and auth status is unavailable. |
| Fish Audio TTS | **Ready for controlled generation** | DPAPI credential, owned Russian voice discovery, central two-dispatch ledger, immutable WAVs and VoiceManifest were integration-tested on 2026-08-28. Paid balance is empty; `s2.1-pro-free` passed for the pilot. Publication still requires voice-rights confirmation. |
| Native local TTS fallback | **Blocked / optional** | Kokoro is not installed. |
| Native transcription fallback | **Blocked / optional** | `whisper-cpp`, CMake and a C/C++ compiler are absent. |
| Local music generation fallback | **Blocked / optional** | MusicGen dependencies are not installed; with current free RAM it must not be installed or invoked automatically. |
| Docker rendering | **Blocked / optional** | Docker is absent. It is not required for native local render. |

The runtime must remain **render-closed** until required gates in this document pass. A cached CLI, a system Chrome, or a successful composition lint is not permission to render.

## Read-only inventory

### Required host tools

| Item | Observed state |
|---|---|
| Node.js | `v22.22.3` at `C:\Program Files\nodejs\node.exe`; satisfies HyperFrames `>=22`. |
| npm / npx | `11.8.0`; available. |
| Git | `2.52.0.windows.1`; available. |
| FFmpeg / FFprobe | Not found on `PATH` and not found in the common Chocolatey, Scoop, Program Files or WinGet FFmpeg locations checked. |
| Browser | System Chrome and Edge exist. Cached HyperFrames doctor also found Puppeteer Chrome Headless Shell `152.0.7977.54`. |
| HyperFrames | No global command. `_npx` cache contains `0.7.66`, `0.7.86`, `0.7.87` and `0.7.88`; doctor says `0.8.13` is current. Cache presence is not an installation or a project pin. |
| Python | Launcher sees CPython `3.13`; not required for HyperFrames itself. |
| WinGet | `v1.29.290`; available for an operator-approved bootstrap. |
| WSL | `wsl.exe` exists, but no Linux distribution/subsystem is installed. |
| CMake / compiler | `cmake`, `cl`, `gcc`, `make` and `ninja` are not on `PATH`. |

### Capacity snapshot

- CPU: 12 logical cores, Intel Core i5-10400F.
- RAM: 15.9 GB total; approximately 3.2–3.6 GB free during the audit.
- Disk C: approximately 97.6 GB free.
- GPU: NVIDIA GeForce GTX 1660 SUPER; WMI reported about 4 GB adapter RAM.

This is adequate for one conservative local 1080×1920 render worker after closing unnecessary browsers. It is not evidence for 10–15 renders/day. Start with `workers=1`; raise concurrency only after measured soak tests. Local MusicGen, LTX or other large-model jobs must not contend with rendering on this host.

### Doctor evidence

Newest cached HyperFrames `0.7.88` was invoked directly with Node, avoiding `npx` installation:

```powershell
node <cached-hyperframes>/bin/hyperframes.mjs doctor --json
```

Result: `ok: false`. Required failures were stale CLI, FFmpeg and FFprobe. Node, CPU, memory, disk and Chrome passed. Optional failures were whisper.cpp, Kokoro, MusicGen and Docker.

The official media-use doctor reported:

- 19 bundled SFX available;
- HeyGen executable/version/auth unavailable;
- FFmpeg and FFprobe unavailable;
- Node version acceptable.

## Important HeyGen constraint on Windows

The current official [HeyGen CLI documentation](https://developers.heygen.com/cli) states that native Windows support is not yet available and recommends WSL. It documents:

- Linux/macOS installation through the official install script;
- `heygen auth login --oauth` for browser OAuth;
- `heygen auth status` as the credential check;
- stored credentials under `~/.heygen/credentials` or `HEYGEN_API_KEY` for non-interactive environments.

Therefore the native Windows factory must **not** claim HeyGen readiness and must not invent a `heygen.cmd` WSL shim. Windows paths, output files, JSON streaming and OAuth need an explicit integration test first. Until then, a job requiring fresh HeyGen TTS/catalog media is `media_provider_blocked`; it may use only previously frozen licensed assets, bundled SFX, approved human narration, or a separately validated local provider.

## Automatable bootstrap plan

The following is a plan for a later operator-authorized bootstrap. These commands were **not run** during this audit.

### Stage 0 — immutable audit, no installation

Run this at the beginning of every worker session. It must exit non-zero on a missing required tool.

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Require-Command([string]$Name) {
    $resolved = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $resolved) { throw "Required command is missing: $Name" }
    return $resolved.Source
}

$nodePath = Require-Command 'node'
$npmPath = Require-Command 'npm'
$npxPath = Require-Command 'npx'
$ffmpegPath = Require-Command 'ffmpeg'
$ffprobePath = Require-Command 'ffprobe'

$nodeMajor = [int](& $nodePath -p "process.versions.node.split('.')[0]")
if ($nodeMajor -lt 22) { throw "Node.js 22+ is required" }

& $ffmpegPath -hide_banner -version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'ffmpeg version probe failed' }
& $ffprobePath -hide_banner -version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'ffprobe version probe failed' }

$encoders = (& $ffmpegPath -hide_banner -encoders 2>&1) -join "`n"
if ($encoders -notmatch '\blibx264\b') { throw 'FFmpeg lacks libx264' }
$filters = (& $ffmpegPath -hide_banner -filters 2>&1) -join "`n"
if ($filters -notmatch '\bloudnorm\b') { throw 'FFmpeg lacks loudnorm' }
```

Do not continue if any assertion fails. Do not silently substitute an unknown FFmpeg binary.

### Stage 1 — install only the required native encoder

This is a mutating step and requires explicit operator approval.

```powershell
winget show --id Gyan.FFmpeg --exact --source winget
if ($LASTEXITCODE -ne 0) { throw 'Expected WinGet package Gyan.FFmpeg is unavailable' }

winget install --id Gyan.FFmpeg --exact --source winget `
  --accept-package-agreements --accept-source-agreements
if ($LASTEXITCODE -ne 0) { throw 'FFmpeg installation failed' }
```

Open a fresh PowerShell session and rerun Stage 0. A WinGet success code without `ffmpeg`, `ffprobe`, `libx264` and `loudnorm` passing is still a failed bootstrap.

Node needs no action on the audited host. On another worker, install/upgrade it only when the major-version check fails:

```powershell
winget install --id OpenJS.NodeJS.LTS --exact --source winget `
  --accept-package-agreements --accept-source-agreements
```

Then open a fresh shell and require Node major version `>=22`.

### Stage 2 — resolve and pin HyperFrames once

Only bootstrap may use `@latest`. Capture the resolved version, then use that exact version for every command and persist the project-generated lockfile. `doctor --json` always exits zero, so its `.ok` field is the gate.

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$doctorRaw = & npx --yes hyperframes@latest doctor --json
$doctor = $doctorRaw | ConvertFrom-Json
if ($doctor.ok -ne $true) {
    $failed = @($doctor.checks | Where-Object { $_.ok -ne $true } | ForEach-Object { $_.name })
    throw "HyperFrames doctor failed: $($failed -join ', ')"
}

$hyperframesVersion = [string]$doctor._meta.version
if ($hyperframesVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw 'Doctor did not return a stable semantic version'
}
```

Write `$hyperframesVersion`, Node/FFmpeg versions and the complete doctor JSON to a versioned runtime report. Never choose a cached version merely because it is already present.

### Stage 3 — initialize one render project only after topic approval

Do not initialize in the factory root. Each approved video receives its own clean project directory. Before this stage require:

- `production_authorized: true`;
- a passed canonical RightsManifest;
- a complete frozen-media ledger whose hashes match local files;
- an approved script and shotlist.

```powershell
$env:HYPERFRAMES_SKIP_SKILLS = '1'
$renderProject = 'pilots\<job-id>\render'

if (Test-Path -LiteralPath $renderProject) {
    throw "Render project already exists: $renderProject"
}

& npx --yes "hyperframes@$hyperframesVersion" init $renderProject `
  --non-interactive --example blank --resolution portrait --skill=general-video
if ($LASTEXITCODE -ne 0) { throw 'HyperFrames init failed' }

foreach ($required in @('package.json', 'package-lock.json', 'hyperframes.json', 'index.html')) {
    if (-not (Test-Path -LiteralPath (Join-Path $renderProject $required))) {
        throw "Scaffold is incomplete: $required"
    }
}
```

`HYPERFRAMES_SKIP_SKILLS=1` prevents scaffold-time skill mutation. The agent already owns skill routing outside the render project.

### Stage 4 — media/audio provider gates

Run the official local doctor after FFmpeg installation:

```powershell
$mediaSkill = 'C:\Users\ns277\.codex\skills\media-use'
& node (Join-Path $mediaSkill 'scripts\resolve.mjs') --doctor
if ($LASTEXITCODE -ne 0) { throw 'media-use doctor failed' }
```

For the current native Windows lane, this will remain blocked while HeyGen is absent. Do not weaken the gate by ignoring the exit code.

Provider policy:

1. **Default native lane:** frozen approved assets + bundled SFX; no unverified music or generated voice fallback.
2. **HeyGen lane:** blocked until a supported Windows CLI exists, or a dedicated WSL worker passes the same doctor and end-to-end file/JSON tests.
3. **WSL experiment:** requires separate approval, `wsl --install -d Ubuntu`, possible reboot, the official HeyGen installer inside Ubuntu, human `heygen auth login --oauth`, `heygen auth status`, and a disposable test project. It must not share production credentials or write production media until verified.
4. **Local Kokoro:** optional. Install only into a dedicated, version-locked virtual environment and require a deterministic WAV smoke test plus FFprobe validation before enabling it. Do not install opportunistically on a render worker.
5. **Local transcription:** optional. Install CMake and a compiler only when ASR is required; build whisper.cpp once, record the binary/model hashes, then rerun both doctors.
6. **Music:** if no explicitly worldwide multi-platform licensed track exists, render without BGM. Platform-library audio is added only to that platform-specific version.

### Stage 5 — deterministic check, preview gate and render

Inside the initialized project:

```powershell
Push-Location $renderProject
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }

    $checkRaw = npm run check -- --strict --snapshots --json
    if ($LASTEXITCODE -ne 0) { throw 'HyperFrames check failed' }
    $check = $checkRaw | ConvertFrom-Json
    if ($check.ok -ne $true) { throw 'HyperFrames check returned ok=false' }

    npm run preview
    if ($LASTEXITCODE -ne 0) { throw 'Preview failed to start' }
}
finally {
    Pop-Location
}
```

Stop after preview. A human must review the assembled timeline. Passing `check` is not render authorization.

After an explicit approval tied to the current project/artifact hashes:

```powershell
Push-Location $renderProject
try {
    npm run render -- --quality high --output out.mp4
    if ($LASTEXITCODE -ne 0) { throw 'Render failed' }
    if (-not (Test-Path -LiteralPath 'out.mp4')) { throw 'Render output is missing' }
    if ((Get-Item -LiteralPath 'out.mp4').Length -le 0) { throw 'Render output is empty' }

    $probeRaw = ffprobe -v error -show_streams -show_format -of json out.mp4
    if ($LASTEXITCODE -ne 0) { throw 'FFprobe verification failed' }
    $probe = $probeRaw | ConvertFrom-Json
    $video = @($probe.streams | Where-Object { $_.codec_type -eq 'video' })
    $audio = @($probe.streams | Where-Object { $_.codec_type -eq 'audio' })
    if ($video.Count -ne 1) { throw 'Expected exactly one video stream' }
    if ($audio.Count -ne 1) { throw 'Expected exactly one audio stream' }
    if ($video[0].width -ne 1080 -or $video[0].height -ne 1920) {
        throw 'Output is not 1080x1920 portrait'
    }
}
finally {
    Pop-Location
}
```

The final QC job must additionally gate duration, 30 fps target, H.264/AAC, integrated loudness `-16…-14 LUFS`, true peak `<= -1 dBTP`, caption completeness/safe zones, black/frozen tails, rights-hash equality and contact-sheet review. A render without these checks remains `qc_pending`.

Default semantic QC is intentionally stricter than the editor preview. Caption
word timing hard-fails when p95 absolute drift exceeds `0.25 s` or any matched
word exceeds `0.45 s`. In speaker-required lanes, the largest accepted speaker
face in each sampled frame must cover at least `2.5%` of the full frame in at
least `80%` of speaker frames, and its median area must be at least `4.5%`.
This permits brief establishing shots without accepting an edit whose speaker
stays small. A face with model confidence `>= 0.70` touching the `1.5%` crop
margin is a hard failure even when it is not marked as the active speaker, so
an interviewer or second participant cannot be silently cut out.

## Fail-closed operating rules

1. No topic approval → no HyperFrames project.
2. No passed RightsManifest and frozen hash ledger → no asset enters the composition.
3. No FFmpeg/FFprobe or missing required codec/filter → no render attempt.
4. No project pin/lockfile → no check, preview or render in automation.
5. `doctor --json` with `.ok != true` → worker unavailable; do not parse the process exit code as success.
6. Missing provider auth → `media_provider_blocked`; never fall back to scraped or “no copyright” media.
7. No HeyGen-native Windows support → no improvised shim in production.
8. Low free RAM → one render worker, no concurrent local model generation.
9. `check` passes → preview only. Human approval is still required before render.
10. Render exists → still not publishable until FFprobe, audio, caption, factual, rights and human QC pass.
11. Never print, serialize or commit auth tokens. Runtime reports contain provider name, CLI version and boolean auth status only.

## Readiness acceptance criteria

The Windows native runtime becomes `READY_FOR_APPROVED_PILOT_RENDER` only when all are true:

- Node `>=22`, FFmpeg, FFprobe, `libx264` and `loudnorm` pass Stage 0;
- current HyperFrames doctor returns `.ok: true` and its resolved version is recorded;
- browser path exists;
- an approved project has a generated package lock and HyperFrames pin;
- media-use doctor passes for every provider selected by that job;
- all timeline media resolve to frozen files whose SHA-256 matches the approved ledger;
- `npm ci` and strict HyperFrames check pass;
- the human approves the final preview.

Until that point the correct state is `runtime_blocked`, not degraded rendering.
