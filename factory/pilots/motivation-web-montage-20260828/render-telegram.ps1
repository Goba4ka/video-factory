$ErrorActionPreference = 'Stop'
$ff = 'C:\Users\ns277\bin\ffmpeg.exe'
$master = 'output\motivation-web-montage-master.mp4'
$target = 'output\motivation-web-montage-telegram.mp4'
$temp = 'output\motivation-web-montage-telegram.tmp.mp4'

& $ff -y -hide_banner -loglevel warning -stats `
  -i $master `
  -map 0:v:0 -map 0:a:0 `
  -vf 'scale=720:1280:flags=lanczos' `
  -c:v libx264 -preset medium -crf 23 -profile:v high -level 4.0 -pix_fmt yuv420p `
  -c:a copy -movflags +faststart `
  $temp

if ($LASTEXITCODE -ne 0) { throw "Telegram render failed with exit code $LASTEXITCODE" }
Move-Item -LiteralPath $temp -Destination $target -Force
