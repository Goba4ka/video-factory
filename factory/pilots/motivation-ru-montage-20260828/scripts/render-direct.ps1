param(
  [string]$Ffmpeg = 'C:\Users\ns277\bin\ffmpeg.exe',
  [string]$Ffprobe = 'C:\Users\ns277\bin\ffprobe.exe'
)

$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Project 'dist'
$Intermediate = Join-Path $Dist 'motivation-ru-montage-v2-intermediate.mkv'
$Master = Join-Path $Dist 'motivation-ru-montage-v2-master-1080x1920.mp4'
$Telegram = Join-Path $Dist 'motivation-ru-montage-v2-telegram-720x1280.mp4'
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

Push-Location $Project
try {
  & $Ffmpeg -hide_banner -y `
    -i 'assets/clips/markaryan.mp4' `
    -i 'assets/source/rybakov/rybakov-full.mp4' `
    -i 'assets/source/hartmann/hartmann-full.mp4' `
    -i 'assets/clips/hartmann.mp4' `
    -i 'assets/audio/markaryan-dialogue.wav' `
    -i 'assets/audio/rybakov-dialogue.wav' `
    -i 'assets/audio/hartmann-dialogue.wav' `
    -i '.media/audio/bgm/bgm_001.mp3' `
    -filter_complex_script 'scripts/render.ffgraph' `
    -map '[vout]' -map '[aout]' `
    -c:v libx264 -preset medium -crf 18 -profile:v high -level 4.1 `
    -c:a pcm_s24le -ar 48000 -shortest $Intermediate
  if ($LASTEXITCODE -ne 0) { throw "Intermediate render failed: $LASTEXITCODE" }

  $SavedErrorPreference = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $Measure = (& $Ffmpeg -hide_banner -nostats -i $Intermediate -map 0:a:0 -af 'loudnorm=I=-14.7:TP=-1.6:LRA=7:print_format=json' -f null NUL 2>&1 | Out-String)
  $ErrorActionPreference = $SavedErrorPreference
  $JsonMatch = [regex]::Match($Measure, '\{[\s\S]*?\}')
  if (-not $JsonMatch.Success) { throw 'Could not parse loudnorm measurement JSON.' }
  $Loud = $JsonMatch.Value | ConvertFrom-Json
  $Normalize = "loudnorm=I=-14.7:TP=-1.6:LRA=7:measured_I=$($Loud.input_i):measured_TP=$($Loud.input_tp):measured_LRA=$($Loud.input_lra):measured_thresh=$($Loud.input_thresh):offset=$($Loud.target_offset):linear=true:print_format=summary"

  & $Ffmpeg -hide_banner -y -i $Intermediate -map 0:v:0 -map 0:a:0 `
    -c:v copy -af $Normalize -c:a aac -b:a 192k -ar 48000 -movflags +faststart $Master
  if ($LASTEXITCODE -ne 0) { throw "Master normalize/remux failed: $LASTEXITCODE" }

  & $Ffmpeg -hide_banner -y -i $Master -map 0:v:0 -map 0:a:0 `
    -vf 'scale=720:1280:flags=lanczos' -c:v libx264 -preset medium -crf 24 -profile:v high -level 4.0 `
    -c:a copy -movflags +faststart $Telegram
  if ($LASTEXITCODE -ne 0) { throw "Telegram transcode failed: $LASTEXITCODE" }

  & $Ffprobe -v error -show_entries format=filename,duration,size:stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $Master
  & $Ffprobe -v error -show_entries format=filename,duration,size:stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $Telegram
}
finally {
  Pop-Location
}
