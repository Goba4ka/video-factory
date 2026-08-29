$ErrorActionPreference = 'Stop'
$ff = 'C:\Users\ns277\bin\ffmpeg.exe'
$filter = @'
[0:v]trim=start=0:end=3.84,setpts=(PTS-STARTPTS)/0.96,fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,hue=s=0,eq=contrast=1.18:brightness=-0.055:gamma=0.96,scale=w='trunc(1080*(1+0.035*t/4)/2)*2':h='trunc(1920*(1+0.035*t/4)/2)*2':eval=frame,crop=1080:1920:(iw-1080)/2:(ih-1920)/2,unsharp=5:5:0.45:5:5:0,noise=alls=3.5:allf=t+u,vignette=PI/5,setsar=1[v0];
[1:v]trim=start=12.18:end=17.12,setpts=(PTS-STARTPTS)/0.96,fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,hue=s=0,eq=contrast=1.18:brightness=-0.055:gamma=0.96,scale=w='trunc(1080*(1+0.035*t/5.145833)/2)*2':h='trunc(1920*(1+0.035*t/5.145833)/2)*2':eval=frame,crop=1080:1920:(iw-1080)/2:(ih-1920)/2,unsharp=5:5:0.45:5:5:0,noise=alls=3.5:allf=t+u,vignette=PI/5,setsar=1[v1];
[2:v]trim=start=20.10:end=29.70,setpts=(PTS-STARTPTS)/0.96,fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,hue=s=0,eq=contrast=1.18:brightness=-0.055:gamma=0.96,scale=w='trunc(1080*(1+0.032*t/10)/2)*2':h='trunc(1920*(1+0.032*t/10)/2)*2':eval=frame,crop=1080:1920:(iw-1080)/2:(ih-1920)/2,unsharp=5:5:0.45:5:5:0,noise=alls=3.5:allf=t+u,vignette=PI/5,setsar=1[v2];
[0:v]trim=start=7.06:end=16.96,setpts=(PTS-STARTPTS)/0.96,fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,hue=s=0,eq=contrast=1.18:brightness=-0.055:gamma=0.96,scale=w='trunc(1080*(1+0.035*t/10.3125)/2)*2':h='trunc(1920*(1+0.035*t/10.3125)/2)*2':eval=frame,crop=1080:1920:(iw-1080)/2:(ih-1920)/2,unsharp=5:5:0.45:5:5:0,noise=alls=3.5:allf=t+u,vignette=PI/5,setsar=1[v3];
[v0][v1][v2][v3]concat=n=4:v=1:a=0,drawbox=x=0:y=1370:w=iw:h=550:color=black:t=fill,drawbox=x=0:y=0:w=iw:h=6:color=0xB5222A@0.9:t=fill,drawbox=x=0:y=0:w=iw:h=ih:color=white@0.62:t=fill:enable='between(t,4,4.067)',drawbox=x=0:y=0:w=iw:h=ih:color=white@0.62:t=fill:enable='between(t,9.146,9.213)',drawbox=x=0:y=0:w=iw:h=ih:color=white@0.62:t=fill:enable='between(t,19.146,19.213)',subtitles=assets/captions.ass[vout];
[0:a]atrim=start=0:end=3.84,asetpts=PTS-STARTPTS,atempo=0.96,highpass=f=70,equalizer=f=250:t=q:w=1.2:g=-2,equalizer=f=3000:t=q:w=1:g=1.5,loudnorm=I=-16:LRA=7:TP=-1.5[a0];
[1:a]atrim=start=12.18:end=17.12,asetpts=PTS-STARTPTS,atempo=0.96,highpass=f=70,equalizer=f=250:t=q:w=1.2:g=-2,equalizer=f=3000:t=q:w=1:g=1.5,loudnorm=I=-16:LRA=7:TP=-1.5[a1];
[2:a]atrim=start=20.10:end=29.70,asetpts=PTS-STARTPTS,atempo=0.96,highpass=f=70,equalizer=f=250:t=q:w=1.2:g=-2,equalizer=f=3000:t=q:w=1:g=1.5,loudnorm=I=-16:LRA=7:TP=-1.5[a2];
[0:a]atrim=start=7.06:end=16.96,asetpts=PTS-STARTPTS,atempo=0.96,highpass=f=70,equalizer=f=250:t=q:w=1.2:g=-2,equalizer=f=3000:t=q:w=1:g=1.5,loudnorm=I=-16:LRA=7:TP=-1.5[a3];
[a0][a1][a2][a3]concat=n=4:v=0:a=1[voice0];
[voice0]asplit=2[voice_sc][voice_mix];
[3:a]atrim=start=0:end=29.458333,asetpts=PTS-STARTPTS,volume=0.88[bed];
[bed][voice_sc]sidechaincompress=threshold=0.02:ratio=8:attack=10:release=400:knee=2:makeup=1[ducked];
[voice_mix][ducked]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:LRA=7:TP=-1.0[aout]
'@

& $ff -y -hide_banner -loglevel warning -stats `
  -i assets\source\goggins.mp4 `
  -i assets\source\markaryan.mp4 `
  -i assets\source\anonymous-speaker.mp4 `
  -i assets\audio\dark-bed.m4a `
  -filter_complex $filter `
  -map '[vout]' -map '[aout]' -t 29.458333 -r 30 `
  -c:v libx264 -preset medium -crf 17 -profile:v high -level 4.2 -pix_fmt yuv420p `
  -c:a aac -b:a 192k -ar 48000 -movflags +faststart `
  output\motivation-montage-master.mp4

if ($LASTEXITCODE -ne 0) { throw "FFmpeg render failed with exit code $LASTEXITCODE" }
