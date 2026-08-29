$ErrorActionPreference = 'Stop'

$ffmpeg = 'C:\Users\ns277\bin\ffmpeg.exe'
$ffprobe = 'C:\Users\ns277\bin\ffprobe.exe'
$source = 'assets\source\oskar-hartmann-official.mp4'
$mix = 'assets\audio\final-mix.wav'
$master = 'renders\motivation-ru-focus-v2-master-1080x1920.mp4'
$telegram = 'renders\motivation-ru-focus-v2-telegram-720x1280.mp4'

$zoom = "if(lte(on,327),1+0.055*on/327,if(lte(on,337),1.055+0.05*(on-327)/10,if(lte(on,439),1.105-0.03*(on-337)/102,if(lte(on,702),1.075+0.05*(on-439)/263,1.125+0.045*(on-702)/149))))"
$videoFilter = "trim=start=28.68:end=57.05,setpts=PTS-STARTPTS,fps=30," +
  "scale=3414:1920:flags=lanczos,crop=1080:1920:1400:0," +
  "zoompan=z='$zoom':x='min(max(570-iw/(2*zoom),0),iw-iw/zoom)':y='min(max(620-ih/(2*zoom),0),ih-ih/zoom)':d=1:s=1080x1920:fps=30," +
  "hqdn3d=1.0:1.0:3.0:3.0,unsharp=5:5:0.42:5:5:0," +
  "eq=contrast=1.07:brightness=-0.025:saturation=0.84:gamma=0.98,colorbalance=rs=0.025:bs=-0.018,vignette=PI/5," +
  "drawbox=x=62:y=82:w=956:h=2:color=0xF2EFE8@0.28:t=fill," +
  "drawbox=x=63:y=109:w=9:h=9:color=0xD9B978@1:t=fill," +
  "drawtext=fontfile='assets/fonts/IBMPlexMono-Bold.ttf':textfile='assets/text/meta-top.txt':x=88:y=102:fontsize=24:fontcolor=0xF2EFE8:shadowx=2:shadowy=2:shadowcolor=black@0.65," +
  "drawtext=fontfile='assets/fonts/IBMPlexMono-Bold.ttf':text='MOTIVE / 02':x=w-tw-64:y=102:fontsize=22:fontcolor=0xF2EFE8@0.95:shadowx=2:shadowy=2:shadowcolor=black@0.95," +
  "drawbox=x=64:y=1822:w=46:h=2:color=0xD9B978@1:t=fill," +
  "drawtext=fontfile='assets/fonts/IBMPlexMono-Regular.ttf':textfile='assets/text/meta-bottom.txt':x=126:y=1807:fontsize=20:fontcolor=0xF2EFE8@0.58:shadowx=2:shadowy=2:shadowcolor=black@0.65," +
  "subtitles=filename='assets/text/captions-ru.ass':fontsdir='assets/fonts',format=yuv420p"

New-Item -ItemType Directory -Force renders | Out-Null

& $ffmpeg -hide_banner -y -i $source -i $mix -filter_complex "[0:v]$videoFilter[v]" `
  -map '[v]' -map '1:a:0' -t 28.37 -r 30 `
  -c:v libx264 -preset slow -crf 18 -profile:v high -level 4.2 -pix_fmt yuv420p `
  -c:a aac -b:a 192k -ar 48000 -ac 2 -movflags +faststart $master
if ($LASTEXITCODE -ne 0) { throw 'Master render failed.' }

& $ffmpeg -hide_banner -y -i $master -map 0:v:0 -map 0:a:0 `
  -vf 'scale=720:1280:flags=lanczos' -c:v libx264 -preset medium -crf 23 `
  -profile:v high -level 4.0 -pix_fmt yuv420p -c:a copy -movflags +faststart $telegram
if ($LASTEXITCODE -ne 0) { throw 'Telegram proxy render failed.' }

& $ffprobe -v error -show_entries format=filename,duration,size,bit_rate -show_entries stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $master
& $ffprobe -v error -show_entries format=filename,duration,size,bit_rate -show_entries stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $telegram
