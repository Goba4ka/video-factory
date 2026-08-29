# Audio preview V4 — 2026-08-30

Status: preview only. No composition HTML, source master, video render, rights
record, approval state, or publish state was changed. No network, TTS, or paid
provider call was made. V3 remains byte-identical.

V4 removes every stepped programme-volume expression used by V3. There is no
automation on the complete mix. All time-varying gain is on the music bed and
uses continuous piecewise-linear ramps of at least 0.40 seconds. Speech keeps
its source dynamics; sidechain attack/release is continuous; the limiter is the
last dynamics processor.

## Independent final-byte gate

Measurements below are a fresh FFmpeg 8.1.2 `loudnorm` first-pass analysis of
the final PCM files, not values reported while creating the premix.

| Candidate | Integrated | True peak | LRA | Duration | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| `client-final-celeb-v2-20260829/assets/audio/celebrity-audio-preview-v4.wav` | -14.80 LUFS | -2.08 dBTP | 2.30 LU | 25.9895 s | PASS |
| `client-final-motivation-a-v2-20260829/assets/audio/motivation-a-audio-preview-v4.wav` | -14.62 LUFS | -1.59 dBTP | 2.40 LU | 29.2000 s | PASS |
| `client-final-motivation-b-v2-20260829/assets/audio/motivation-b-audio-preview-v4.wav` | -14.97 LUFS | -1.50 dBTP | 2.10 LU | 31.2000 s | PASS |

Gate: integrated -15..-14 LUFS, true peak <= -1.2 dBTP, LRA >= 2 LU.
All candidates are 48 kHz, stereo, 24-bit PCM WAV.

## Reproduction environment

All commands ran from the repository root in PowerShell:

```powershell
$ff = 'C:\Users\ns277\Downloads\ffmpeg_dl\ffmpeg-8.1.2-essentials_build\bin\ffmpeg.exe'
$work = 'C:\Users\ns277\AppData\Local\Temp\vf-audio-preview-v4-20260830'
Set-Location -LiteralPath 'C:\Users\ns277\OneDrive\Документы\New project 2'
New-Item -ItemType Directory -Path $work -Force | Out-Null
```

## Celebrity

The only long-form musical move is a closing crescendo from 18.00 to 25.50 s.
It supports the editorial resolution rather than manufacturing LRA by changing
the complete programme level. All shorter lifts align with detected narration
gaps.

### Premix

```powershell
& $ff -hide_banner -loglevel error -y `
  -i 'factory/pilots/client-final-celeb-v2-20260829/assets/audio/voice-fish-v1-master.wav' `
  -i 'factory/pilots/client-final-celeb-v2-20260829/assets/audio/music-bed.wav' `
  -filter_complex "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[voice_mix][voice_sc];[1:a]aformat=sample_rates=48000:channel_layouts=stereo,equalizer=f=1800:t=q:w=0.9:g=-3,volume='if(lt(t,1.0),1.30,if(lt(t,2.0),1.30+(t-1.0)*(0.45-1.30)/1.0,if(lt(t,6.25),0.45,if(lt(t,6.70),0.45+(t-6.25)*(0.80-0.45)/0.45,if(lt(t,7.25),0.80,if(lt(t,7.75),0.80+(t-7.25)*(0.45-0.80)/0.50,if(lt(t,15.20),0.45,if(lt(t,15.65),0.45+(t-15.20)*(0.85-0.45)/0.45,if(lt(t,16.35),0.85,if(lt(t,16.85),0.85+(t-16.35)*(0.45-0.85)/0.50,if(lt(t,18.00),0.45,if(lt(t,25.50),0.45+(t-18.00)*(1.80-0.45)/7.50,1.80))))))))))))':eval=frame[bed];[bed][voice_sc]sidechaincompress=threshold=0.08:ratio=1.50:attack=25:release=300:makeup=1[ducked];[voice_mix][ducked]amix=inputs=2:weights='1 1':normalize=0:duration=first[premix]" `
  -map '[premix]' -ar 48000 -ac 2 -c:a pcm_s24le `
  "$work\celeb-premix-v4c.wav"
```

Bed ramps: 1.00 s down after the opening hit; 0.45/0.50 s around the
6.25-7.75 s gap; 0.45/0.50 s around the 15.20-16.85 s gap; 7.50 s closing
crescendo. Minimum ramp duration: 0.45 s.

### Analysis and linear pass 2

```powershell
& $ff -hide_banner -nostats `
  -i "$work\celeb-premix-v4c.wav" `
  -af "loudnorm=I=-14.8:LRA=7:TP=-1.6:print_format=json" `
  -f null NUL

& $ff -hide_banner -loglevel error -y `
  -i "$work\celeb-premix-v4c.wav" `
  -af "loudnorm=I=-14.8:LRA=7:TP=-1.6:measured_I=-14.53:measured_LRA=2.30:measured_TP=-1.81:measured_thresh=-24.53:offset=-0.27:linear=true:print_format=summary,alimiter=limit=0.831764:attack=5:release=50:level=false:latency=true" `
  -ar 48000 -ac 2 -c:a pcm_s24le `
  'factory/pilots/client-final-celeb-v2-20260829/assets/audio/celebrity-audio-preview-v4.wav'
```

## Motivation A

Speech is energy-preserving dual mono (`0.707` per channel). The bed moves only
around the opening, the detected 6.85-7.60 s pause, and the conclusion. There is
no complete-programme gain arc.

### Premix

```powershell
& $ff -hide_banner -loglevel error -y `
  -i 'factory/pilots/client-final-motivation-a-v2-20260829/assets/audio/speaker-leveled-v2.m4a' `
  -i 'factory/pilots/client-final-motivation-a-v2-20260829/assets/audio/music-bed-leveled.m4a' `
  -filter_complex "[0:a]aresample=48000,pan=stereo|c0=0.707*c0|c1=0.707*c0,asplit=2[voice_mix][voice_sc];[1:a]aresample=48000,equalizer=f=1800:t=q:w=0.9:g=-3,volume='if(lt(t,1.0),0.90,if(lt(t,1.5),0.90+(t-1.0)*(0.74-0.90)/0.5,if(lt(t,6.45),0.74,if(lt(t,6.90),0.74+(t-6.45)*(1.00-0.74)/0.45,if(lt(t,7.50),1.00,if(lt(t,8.00),1.00+(t-7.50)*(0.74-1.00)/0.50,if(lt(t,26.80),0.74,if(lt(t,27.40),0.74+(t-26.80)*(1.00-0.74)/0.60,1.00))))))))':eval=frame[bed];[bed][voice_sc]sidechaincompress=threshold=0.12:ratio=1.25:attack=22:release=280:makeup=1[ducked];[voice_mix][ducked]amix=inputs=2:weights='1 1':normalize=0:duration=shortest[premix]" `
  -map '[premix]' -ar 48000 -ac 2 -c:a pcm_s24le `
  "$work\mot-a-premix-v4.wav"
```

Bed ramps: 0.50 s after the opening; 0.45/0.50 s around the pause; 0.60 s into
the conclusion. Minimum ramp duration: 0.45 s.

### Analysis and linear pass 2

```powershell
& $ff -hide_banner -nostats `
  -i "$work\mot-a-premix-v4.wav" `
  -af "loudnorm=I=-14.8:LRA=7:TP=-1.6:print_format=json" `
  -f null NUL

& $ff -hide_banner -loglevel error -y `
  -i "$work\mot-a-premix-v4.wav" `
  -af "loudnorm=I=-14.8:LRA=7:TP=-1.6:measured_I=-15.64:measured_LRA=2.20:measured_TP=-2.29:measured_thresh=-25.65:offset=0.02:linear=true:print_format=summary,alimiter=limit=0.831764:attack=5:release=50:level=false:latency=true,asetpts=N/SR/TB,atrim=duration=29.2" `
  -ar 48000 -ac 2 -c:a pcm_s24le `
  'factory/pilots/client-final-motivation-a-v2-20260829/assets/audio/motivation-a-audio-preview-v4.wav'
```

`asetpts`/`atrim` is structural duration handling after the final dynamics
processor; it does not change gain.

## Motivation B

The bed remains available at full level but is strongly and smoothly ducked by
speech (`attack=15`, `release=220`). This exposes the source speech dynamics and
lets music rise in actual gaps. A four-second closing bed crescendo is the only
long-form musical arc. There is no complete-programme automation.

### Premix

```powershell
& $ff -hide_banner -loglevel error -y `
  -i 'factory/pilots/client-final-motivation-b-v2-20260829/assets/audio/speech.m4a' `
  -i 'factory/pilots/client-final-motivation-b-v2-20260829/assets/audio/dark-tension-bed-dynamic.m4a' `
  -filter_complex "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[voice_mix][voice_sc];[1:a]aresample=48000,equalizer=f=1800:t=q:w=0.9:g=-3,volume='if(lt(t,1.0),0.90,if(lt(t,1.5),0.90+(t-1.0)*(0.75-0.90)/0.50,if(lt(t,3.80),0.75,if(lt(t,4.25),0.75+(t-3.80)*(1.00-0.75)/0.45,if(lt(t,5.35),1.00,if(lt(t,5.85),1.00+(t-5.35)*(0.75-1.00)/0.50,if(lt(t,8.20),0.75,if(lt(t,8.60),0.75+(t-8.20)*(0.95-0.75)/0.40,if(lt(t,9.05),0.95,if(lt(t,9.50),0.95+(t-9.05)*(0.75-0.95)/0.45,if(lt(t,25.30),0.75,if(lt(t,25.70),0.75+(t-25.30)*(0.95-0.75)/0.40,if(lt(t,26.20),0.95,if(lt(t,26.65),0.95+(t-26.20)*(0.75-0.95)/0.45,if(lt(t,27.00),0.75,if(lt(t,31.00),0.75+(t-27.00)*(1.20-0.75)/4.00,1.20))))))))))))))))':eval=frame[bed];[bed][voice_sc]sidechaincompress=threshold=0.03:ratio=4.00:attack=15:release=220:makeup=1[ducked];[voice_mix][ducked]amix=inputs=2:weights='1 1':normalize=0:duration=first[premix]" `
  -map '[premix]' -ar 48000 -ac 2 -c:a pcm_s24le `
  "$work\mot-b-premix-v4d.wav"
```

Bed ramps: 0.50 s after the opening; 0.45/0.50 s around 4.25-5.35 s;
0.40/0.45 s around 8.60-9.05 s; 0.40/0.45 s around 25.70-26.20 s; and a
4.00 s closing crescendo. Minimum ramp duration: 0.40 s.

### Analysis and transparent gain/ceiling

The premix measured -16.48 LUFS, -2.30 dBTP, and 2.00 LU LRA. A dynamic
`loudnorm` pass was explicitly rejected because its measured output LRA was
0.90 LU. The accepted chain uses one constant +1.55 dB gain (`1.195` linear)
and a final -1.60 dBFS ceiling, preserving the source programme dynamics.

```powershell
& $ff -hide_banner -nostats `
  -i "$work\mot-b-premix-v4d.wav" `
  -af "loudnorm=I=-14.8:LRA=7:TP=-1.6:print_format=json" `
  -f null NUL

& $ff -hide_banner -loglevel error -y `
  -i "$work\mot-b-premix-v4d.wav" `
  -af "volume=1.195,alimiter=limit=0.831764:attack=5:release=50:level=false:latency=true,asetpts=N/SR/TB,apad=whole_dur=31.2,atrim=duration=31.2" `
  -ar 48000 -ac 2 -c:a pcm_s24le `
  'factory/pilots/client-final-motivation-b-v2-20260829/assets/audio/motivation-b-audio-preview-v4.wav'
```

`asetpts`/`apad`/`atrim` adds only the decoded silent tail required to retain the
31.20-second authoritative duration.

## Independent final-byte commands

These three commands produced the gate table at the top of this report:

```powershell
& $ff -hide_banner -nostats `
  -i 'factory/pilots/client-final-celeb-v2-20260829/assets/audio/celebrity-audio-preview-v4.wav' `
  -af "loudnorm=I=-14.8:LRA=7:TP=-1.6:print_format=json" -f null NUL

& $ff -hide_banner -nostats `
  -i 'factory/pilots/client-final-motivation-a-v2-20260829/assets/audio/motivation-a-audio-preview-v4.wav' `
  -af "loudnorm=I=-14.8:LRA=7:TP=-1.6:print_format=json" -f null NUL

& $ff -hide_banner -nostats `
  -i 'factory/pilots/client-final-motivation-b-v2-20260829/assets/audio/motivation-b-audio-preview-v4.wav' `
  -af "loudnorm=I=-14.8:LRA=7:TP=-1.6:print_format=json" -f null NUL
```

## Integrity

| Candidate | SHA-256 |
| --- | --- |
| Celebrity V4 | `2CDF093BBF4DF6EBB9FCE4CF3DCF7903C709535A325A44E2EC8BC42A10692830` |
| Motivation A V4 | `63FD76E18673AAEFB5D5ADC315E6E3A8157989897673361BA070F55673CC2D36` |
| Motivation B V4 | `EF91FDCA14D5804B50AF6F77AB4947101C0F1664B6B0C1CB088511E3F5922E43` |

V3 preservation check:

| Candidate | Unchanged V3 SHA-256 |
| --- | --- |
| Celebrity V3 | `AE03109AEF7E7CF041D5C555FACCAAB6D15837CD0784DC9CC46C703474E90E4F` |
| Motivation A V3 | `43000D26486CEA29995B9041A0327B9F41F4D3FA106432F9233B6086260AA747` |
| Motivation B V3 | `AF3C96EB80C8FBD96AF07A2E9BA277D69B2ECA7AE86A1FB69BED12E16DDABC5B` |

## Remaining limitation

These are measurement-qualified previews, not listening-approved masters. The
celebrity Fish voice timbre is unchanged; mixing cannot substitute a different
performer. Human listening approval is still required before any composition
binding or video render.
