$ErrorActionPreference = 'Stop'
$ff = 'C:\Users\ns277\bin\ffmpeg.exe'
$filter = @'
[0:v]trim=duration=5,setpts=PTS-STARTPTS,fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:480:0,hue=s=0,eq=contrast=1.20:brightness=-0.055:gamma=0.96,scale=w='trunc(1080*(1+0.040*t/5)/2)*2':h='trunc(1920*(1+0.040*t/5)/2)*2':eval=frame,crop=1080:1920:(iw-1080)/2:(ih-1920)/2,unsharp=5:5:0.50:5:5:0,noise=alls=3.4:allf=t+u,vignette=PI/5,setsar=1[v0];
[1:v]trim=duration=17,setpts=PTS-STARTPTS,fps=30,split=2[jbg0][jfg0];
[jbg0]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=28,hue=s=0,eq=contrast=1.12:brightness=-0.16:gamma=0.92[jbg];
[jfg0]scale=1000:-2:flags=lanczos,hue=s=0,eq=contrast=1.18:brightness=-0.045:gamma=0.96,unsharp=5:5:0.55:5:5:0[jfg];
[jbg][jfg]overlay=x=(W-w)/2:y=360,drawbox=x=38:y=358:w=1004:h=568:color=0xB5222A@0.44:t=2,noise=alls=3.2:allf=t+u,vignette=PI/5,setsar=1[v1];
[2:v]trim=duration=5.4,setpts=PTS-STARTPTS,fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:820:0,hue=s=0,eq=contrast=1.20:brightness=-0.050:gamma=0.96,scale=w='trunc(1080*(1+0.045*t/5.4)/2)*2':h='trunc(1920*(1+0.045*t/5.4)/2)*2':eval=frame,crop=1080:1920:(iw-1080)/2:(ih-1920)/2,unsharp=5:5:0.48:5:5:0,noise=alls=3.4:allf=t+u,vignette=PI/5,setsar=1[v2];
[v0][v1][v2]concat=n=3:v=1:a=0,drawbox=x=0:y=1360:w=iw:h=560:color=black:t=fill,drawbox=x=0:y=0:w=iw:h=6:color=0xB5222A@0.92:t=fill,drawbox=x=0:y=0:w=iw:h=ih:color=white@0.62:t=fill:enable='between(t,5,5.067)',drawbox=x=0:y=0:w=iw:h=ih:color=white@0.62:t=fill:enable='between(t,22,22.067)',subtitles=assets/captions.ass[vout];
[0:a]atrim=duration=5,asetpts=PTS-STARTPTS,highpass=f=72,equalizer=f=250:t=q:w=1.2:g=-2,equalizer=f=3000:t=q:w=1:g=1.6,loudnorm=I=-16:LRA=7:TP=-1.5[a0];
[1:a]atrim=duration=17,asetpts=PTS-STARTPTS,highpass=f=72,equalizer=f=250:t=q:w=1.2:g=-2.5,equalizer=f=3000:t=q:w=1:g=1.8,loudnorm=I=-16:LRA=7:TP=-1.5[a1];
[2:a]atrim=duration=5.4,asetpts=PTS-STARTPTS,highpass=f=72,equalizer=f=250:t=q:w=1.2:g=-2,equalizer=f=3000:t=q:w=1:g=1.6,loudnorm=I=-16:LRA=7:TP=-1.5[a2];
[a0][a1][a2]concat=n=3:v=0:a=1[voice0];
[voice0]asplit=2[voice_sc][voice_mix];
[3:a]atrim=duration=27.4,asetpts=PTS-STARTPTS,volume=0.90[bed];
[bed][voice_sc]sidechaincompress=threshold=0.02:ratio=8:attack=10:release=420:knee=2:makeup=1[ducked];
[voice_mix][ducked]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14.5:LRA=7:TP=-1.2[aout]
'@

& $ff -y -hide_banner -loglevel warning -stats `
  -i assets\clips\goggins.mp4 `
  -i assets\clips\jocko.mp4 `
  -i assets\clips\arnold.mp4 `
  -i assets\audio\procedural-dark-bed.wav `
  -filter_complex $filter -map '[vout]' -map '[aout]' -t 27.4 -r 30 `
  -c:v libx264 -preset medium -crf 17 -profile:v high -level 4.2 -pix_fmt yuv420p `
  -c:a aac -b:a 192k -ar 48000 -movflags +faststart `
  output\motivation-web-montage-master.mp4

if ($LASTEXITCODE -ne 0) { throw "FFmpeg render failed with exit code $LASTEXITCODE" }
