$ErrorActionPreference = 'Stop'

$ffmpeg = 'C:\Users\ns277\bin\ffmpeg.exe'
$ffprobe = 'C:\Users\ns277\bin\ffprobe.exe'
$source = '..\..\research\v3-sources\khakamada-1080-304.460-326.500.mp4'
$bed = '..\..\research\motivation-references-v3-audio\candidates\reference\reference-bed-01-dark-bass-70bpm.wav'
$master = 'renders\motivation-v3-monologue-prototype-master.mp4'
$telegram = 'renders\motivation-v3-monologue-prototype-telegram.mp4'

New-Item -ItemType Directory -Force -Path 'renders' | Out-Null

$video = "trim=duration=18.70,setpts=PTS-STARTPTS,fps=30," +
  "crop=1080:1080:0:0," +
  "hue=s=0,eq=contrast=1.14:brightness=-0.035:gamma=0.95," +
  "vignette=PI/5,noise=alls=3:allf=t+u," +
  "zoompan=z='if(lt(on,195),1.0,if(lt(on,375),1.07,1.13))':x='0':y='0':d=1:s=1080x1080:fps=30," +
  "unsharp=5:5:0.30:5:5:0," +
  "drawgrid=w=iw:h=5:t=1:c=black@0.085," +
  "pad=1080:1920:0:420:black," +
  "subtitles=filename='captions.ass':fontsdir='assets/fonts',format=yuv420p"

$audio = "[0:a]atrim=duration=18.70,asetpts=PTS-STARTPTS," +
  "highpass=f=75,lowpass=f=15500,equalizer=f=260:t=q:w=1:g=-1.5," +
  "equalizer=f=3200:t=q:w=1:g=1.5," +
  "acompressor=threshold=0.08:ratio=2.5:attack=10:release=140:makeup=1.4," +
  "volume=1.08,asplit=2[voice_sc][voice_mix];" +
  "[1:a]atrim=duration=18.70,asetpts=PTS-STARTPTS," +
  "afade=t=in:st=0:d=0.25,afade=t=out:st=18.10:d=0.60," +
  "volume=0.36,equalizer=f=2500:t=q:w=1.2:g=-3[music];" +
  "[music][voice_sc]sidechaincompress=threshold=0.028:ratio=5:attack=30:release=400:makeup=1[ducked];" +
  "[voice_mix][ducked]amix=inputs=2:duration=first:dropout_transition=0:weights='1 0.92'," +
  "loudnorm=I=-14.0:TP=-2.0:LRA=7," +
  "alimiter=limit=0.78:attack=5:release=70:level=disabled[aout]"

& $ffmpeg -hide_banner -y -i $source -i $bed -filter_complex "[0:v]$video[vout];$audio" `
  -map '[vout]' -map '[aout]' -t 18.70 -r 30 `
  -c:v libx264 -preset slow -crf 16 -profile:v high -level 4.2 -pix_fmt yuv420p `
  -c:a aac -b:a 192k -ar 48000 -ac 2 -movflags +faststart $master
if ($LASTEXITCODE -ne 0) { throw 'Prototype master render failed.' }

& $ffmpeg -hide_banner -y -i $master -map 0:v:0 -map 0:a:0 `
  -vf 'scale=720:1280:flags=lanczos' -c:v libx264 -preset slow -crf 20 `
  -profile:v high -level 4.0 -pix_fmt yuv420p -c:a aac -b:a 160k -movflags +faststart $telegram
if ($LASTEXITCODE -ne 0) { throw 'Prototype Telegram render failed.' }

& $ffprobe -v error -show_entries format=filename,duration,size,bit_rate -show_entries stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $master
& $ffprobe -v error -show_entries format=filename,duration,size,bit_rate -show_entries stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $telegram
