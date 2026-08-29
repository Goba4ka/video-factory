param([string]$Ffmpeg = 'C:\Users\ns277\bin\ffmpeg.exe')
$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Project 'dist'
$Mix = Join-Path $Dist 'motivation-ru-montage-v2-audio-mix.wav'
$Aac = Join-Path $Dist 'motivation-ru-montage-v2-audio-final.m4a'
$Master = Join-Path $Dist 'motivation-ru-montage-v2-master-1080x1920.mp4'
$Telegram = Join-Path $Dist 'motivation-ru-montage-v2-telegram-720x1280.mp4'
$MasterFixed = Join-Path $Dist 'motivation-ru-montage-v2-master-fixed.mp4'
$TelegramFixed = Join-Path $Dist 'motivation-ru-montage-v2-telegram-fixed.mp4'

Push-Location $Project
try {
  & $Ffmpeg -hide_banner -y `
    -i 'assets/audio/markaryan-dialogue.wav' `
    -i 'assets/audio/rybakov-dialogue.wav' `
    -i 'assets/audio/hartmann-dialogue.wav' `
    -i '.media/audio/bgm/bgm_001.mp3' `
    -filter_complex_script 'scripts/mix-audio.ffgraph' -map '[aout]' -c:a pcm_s24le -ar 48000 $Mix
  if ($LASTEXITCODE -ne 0) { throw "Audio mix failed: $LASTEXITCODE" }

  $SavedErrorPreference = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $Measure = (& $Ffmpeg -hide_banner -nostats -i $Mix -af 'loudnorm=I=-14.7:TP=-1.6:LRA=7:print_format=json' -f null NUL 2>&1 | Out-String)
  $ErrorActionPreference = $SavedErrorPreference
  $JsonMatch = [regex]::Match($Measure, '\{[\s\S]*?\}')
  if (-not $JsonMatch.Success) { throw 'Could not parse loudnorm JSON.' }
  $Loud = $JsonMatch.Value | ConvertFrom-Json
  $Normalize = "loudnorm=I=-14.7:TP=-1.6:LRA=7:measured_I=$($Loud.input_i):measured_TP=$($Loud.input_tp):measured_LRA=$($Loud.input_lra):measured_thresh=$($Loud.input_thresh):offset=$($Loud.target_offset):linear=true"

  & $Ffmpeg -hide_banner -y -i $Mix -af $Normalize -c:a aac -b:a 192k -ar 48000 $Aac
  if ($LASTEXITCODE -ne 0) { throw "AAC normalization failed: $LASTEXITCODE" }
  & $Ffmpeg -hide_banner -y -i $Master -i $Aac -map 0:v:0 -map 1:a:0 -c copy -shortest -movflags +faststart $MasterFixed
  if ($LASTEXITCODE -ne 0) { throw "Master audio remux failed: $LASTEXITCODE" }
  & $Ffmpeg -hide_banner -y -i $Telegram -i $Aac -map 0:v:0 -map 1:a:0 -c copy -shortest -movflags +faststart $TelegramFixed
  if ($LASTEXITCODE -ne 0) { throw "Telegram audio remux failed: $LASTEXITCODE" }
  Move-Item -LiteralPath $MasterFixed -Destination $Master -Force
  Move-Item -LiteralPath $TelegramFixed -Destination $Telegram -Force
}
finally { Pop-Location }

