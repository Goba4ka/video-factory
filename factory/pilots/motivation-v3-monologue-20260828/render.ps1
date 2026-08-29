param(
  [switch]$SkipHyperFrames
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Ffmpeg = 'C:\Users\ns277\bin\ffmpeg.exe'
$Ffprobe = 'C:\Users\ns277\bin\ffprobe.exe'
$env:Path = "C:\Users\ns277\bin;$env:Path"
$RenderDir = Join-Path $ProjectRoot 'renders'
$QcDir = Join-Path $ProjectRoot 'qc'
$Intermediate = Join-Path $RenderDir 'motivation-v3-monologue-hf-intermediate.mp4'
$Master = Join-Path $RenderDir 'motivation-v3-monologue-master.mp4'
$Telegram = Join-Path $RenderDir 'motivation-v3-monologue-telegram.mp4'

New-Item -ItemType Directory -Force -Path $RenderDir | Out-Null
New-Item -ItemType Directory -Force -Path $QcDir | Out-Null

if (-not $SkipHyperFrames) {
  Push-Location $ProjectRoot
  try {
    npx hyperframes render . --output $Intermediate --quality high --fps 30 --crf 14 --strict
    if ($LASTEXITCODE -ne 0) { throw 'HyperFrames render failed.' }
  }
  finally {
    Pop-Location
  }
}

if (-not (Test-Path -LiteralPath $Intermediate)) {
  throw "Missing HyperFrames intermediate: $Intermediate"
}

# Windows PowerShell surfaces FFmpeg's normal stderr progress as NativeCommandError
# under Stop. From here on, rely on explicit LASTEXITCODE checks instead.
$ErrorActionPreference = 'Continue'

function Measure-Loudness {
  param([Parameter(Mandatory = $true)][string]$InputPath)

  $log = (& $Ffmpeg -hide_banner -nostats -i $InputPath `
    -map 0:a:0 -af 'loudnorm=I=-14.5:TP=-2.0:LRA=7:print_format=json' `
    -f null NUL 2>&1 | Out-String)

  $start = $log.LastIndexOf('{')
  $end = $log.LastIndexOf('}')
  if ($start -lt 0 -or $end -le $start) {
    throw "Could not parse loudnorm analysis for $InputPath"
  }
  return ($log.Substring($start, $end - $start + 1) | ConvertFrom-Json)
}

$InputMeasure = Measure-Loudness -InputPath $Intermediate

function Encode-Delivery {
  param(
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [Parameter(Mandatory = $true)][int]$Crf,
    [Parameter(Mandatory = $true)][string]$AudioBitrate,
    [string]$VideoFilter = ''
  )

  $preTargetTp = -2.0
  $encodedMeasure = $null

  for ($attempt = 1; $attempt -le 3; $attempt++) {
    $audioFilter = "loudnorm=I=-14.5:TP=${preTargetTp}:LRA=7:" +
      "measured_I=$($InputMeasure.input_i):measured_TP=$($InputMeasure.input_tp):" +
      "measured_LRA=$($InputMeasure.input_lra):measured_thresh=$($InputMeasure.input_thresh):" +
      "offset=$($InputMeasure.target_offset):linear=true:print_format=summary"

    $args = @('-hide_banner', '-y', '-i', $Intermediate, '-map', '0:v:0', '-map', '0:a:0')
    if ($VideoFilter) { $args += @('-vf', $VideoFilter) }
    $args += @(
      '-af', $audioFilter,
      '-t', '18.70', '-r', '30',
      '-c:v', 'libx264', '-preset', 'slow', '-crf', "$Crf",
      '-profile:v', 'high', '-pix_fmt', 'yuv420p',
      '-c:a', 'aac', '-b:a', $AudioBitrate, '-ar', '48000', '-ac', '2',
      '-movflags', '+faststart', $OutputPath
    )

    & $Ffmpeg @args
    if ($LASTEXITCODE -ne 0) { throw "Delivery encode failed: $OutputPath" }

    $encodedMeasure = Measure-Loudness -InputPath $OutputPath
    $encodedTp = [double]$encodedMeasure.input_tp
    if ($encodedTp -le -1.2) { break }

    $preTargetTp -= 0.8
    if ($attempt -eq 3) {
      throw "Encoded true peak remains above -1.2 dBTP: $encodedTp dBTP ($OutputPath)"
    }
  }

  $encodedI = [double]$encodedMeasure.input_i
  if ([math]::Abs($encodedI - (-14.5)) -gt 0.65) {
    throw "Encoded loudness outside tolerance: $encodedI LUFS-I ($OutputPath)"
  }

  return [pscustomobject]@{
    path = $OutputPath
    lufs_i = [double]$encodedMeasure.input_i
    true_peak_dbtp = [double]$encodedMeasure.input_tp
    lra_lu = [double]$encodedMeasure.input_lra
    pre_aac_target_tp = $preTargetTp
  }
}

$MasterMeasure = Encode-Delivery -OutputPath $Master -Crf 16 -AudioBitrate '192k'
$TelegramMeasure = Encode-Delivery -OutputPath $Telegram -Crf 20 -AudioBitrate '160k' -VideoFilter 'scale=720:1280:flags=lanczos'

$ProbeMaster = & $Ffprobe -v error -show_entries format=duration,size,bit_rate -show_entries stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $Master | ConvertFrom-Json
$ProbeTelegram = & $Ffprobe -v error -show_entries format=duration,size,bit_rate -show_entries stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $Telegram | ConvertFrom-Json

$Report = [pscustomobject]@{
  generated_at = (Get-Date).ToString('o')
  master = [pscustomobject]@{
    path = $Master
    loudness = $MasterMeasure
    probe = $ProbeMaster
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Master).Hash
  }
  telegram = [pscustomobject]@{
    path = $Telegram
    loudness = $TelegramMeasure
    probe = $ProbeTelegram
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Telegram).Hash
  }
}

$Report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $QcDir 'delivery-report.json') -Encoding utf8
$Report | ConvertTo-Json -Depth 10
