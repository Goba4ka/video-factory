# Audio preview V3 — 2026-08-29

Status: preview only. These files are not referenced by any composition, are not
approved masters, and were not rendered or published. No network or TTS call was
made. Measurements use FFmpeg 8.1.2 `loudnorm` first-pass analysis of the final
PCM bytes.

## Gate result

| Candidate | Integrated | True peak | LRA | Duration | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| `client-final-celeb-v2-20260829/assets/audio/celebrity-audio-preview-v3.wav` | -14.22 LUFS | -1.50 dBTP | 3.80 LU | 25.9895 s | PASS |
| `client-final-motivation-a-v2-20260829/assets/audio/motivation-a-audio-preview-v3.wav` | -14.29 LUFS | -1.49 dBTP | 3.70 LU | 29.2000 s | PASS |
| `client-final-motivation-b-v2-20260829/assets/audio/motivation-b-audio-preview-v3.wav` | -14.51 LUFS | -1.48 dBTP | 2.10 LU | 31.2000 s | PASS |

Gate: integrated -15..-14 LUFS, true peak <= -1.2 dBTP, LRA >= 2 LU.
All candidates are 48 kHz, stereo, 24-bit PCM WAV.

## What changed

- Celebrity: existing `voice-fish-v1-master.wav` plus `music-bed.wav`. The bed
  gets a broad -3 dB carve at 1.8 kHz, light sidechain duck
  (`threshold=0.08`, `ratio=1.5`), pause/hook lifts, and a measured programme
  arc. No voice-tone EQ was guessed.
- Motivation A: `speaker-leveled-v2.m4a` plus `music-bed-leveled.m4a`.
  Mono speech is converted to energy-preserving dual mono (`0.707` per
  channel), the bed gets the same 1.8 kHz carve, light duck
  (`threshold=0.12`, `ratio=1.25`), pause lifts, and a programme arc.
- Motivation B: `speech.m4a` plus `dark-tension-bed-dynamic.m4a`. The bed gets
  the carve, light duck (`threshold=0.08`, `ratio=1.45`), explicit lifts in
  detected speech gaps, and a programme arc. The final silent tail is padded to
  the authoritative 31.2-second duration.

The previous programme masters measured as follows with the same analyzer:

| Existing master | Integrated | True peak | LRA |
| --- | ---: | ---: | ---: |
| Celebrity `final-mix.wav` | -13.92 LUFS | -2.45 dBTP | 1.00 LU |
| Motivation A `final-mix.m4a` | -14.51 LUFS | -1.43 dBTP | 1.00 LU |
| Motivation B `final-mix.m4a` | -14.23 LUFS | -0.92 dBTP | 2.10 LU |

## Two-pass loudness record

All commands below ran from
`C:\Users\ns277\OneDrive\Документы\New project 2` in PowerShell. These are the
exact local executable and intermediate directory used:

```powershell
$ff = 'C:\Users\ns277\Downloads\ffmpeg_dl\ffmpeg-8.1.2-essentials_build\bin\ffmpeg.exe'
$work = 'C:\Users\ns277\AppData\Local\Temp\vf-audio-preview-v3-20260829'
Set-Location -LiteralPath 'C:\Users\ns277\OneDrive\Документы\New project 2'
New-Item -ItemType Directory -Path $work -Force | Out-Null
```

### Celebrity: premix, pass 1, pass 2

The exact premix command was:

```powershell
& $ff -hide_banner -loglevel error -y `
  -i 'factory/pilots/client-final-celeb-v2-20260829/assets/audio/voice-fish-v1-master.wav' `
  -i 'factory/pilots/client-final-celeb-v2-20260829/assets/audio/music-bed.wav' `
  -filter_complex "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[voice_mix][voice_sc];[1:a]aformat=sample_rates=48000:channel_layouts=stereo,equalizer=f=1800:t=q:w=0.9:g=-3,volume='if(lt(t,2),0.94,if(between(t,6.65,7.30),0.98,if(between(t,15.65,16.40),0.98,if(gt(t,22.90),1.0,0.78))))':eval=frame[bed];[bed][voice_sc]sidechaincompress=threshold=0.08:ratio=1.5:attack=20:release=260:makeup=1[ducked];[voice_mix][ducked]amix=inputs=2:weights='1 1':normalize=0:duration=first[mix];[mix]volume='if(lt(t,5.8),1.0,if(lt(t,12.8),0.78,if(lt(t,19.8),0.68,if(lt(t,22.9),0.82,1.0))))':eval=frame[premix]" `
  -map '[premix]' -ar 48000 -ac 2 -c:a pcm_s24le `
  "$work\celeb-premix-final-v2.wav"
```

Pass 1 and the exact measured pass 2 were:

```powershell
& $ff -hide_banner -nostats `
  -i "$work\celeb-premix-final-v2.wav" `
  -af "loudnorm=I=-14.5:LRA=7:TP=-1.5:print_format=json" `
  -f null NUL

& $ff -hide_banner -loglevel error -y `
  -i "$work\celeb-premix-final-v2.wav" `
  -af "loudnorm=I=-14.5:LRA=7:TP=-1.5:measured_I=-15.95:measured_LRA=3.80:measured_TP=-2.84:measured_thresh=-25.95:offset=0.43:linear=true:print_format=summary,alimiter=limit=0.841395:attack=5:release=50:level=false:latency=true" `
  -ar 48000 -ac 2 -c:a pcm_s24le `
  'factory/pilots/client-final-celeb-v2-20260829/assets/audio/celebrity-audio-preview-v3.wav'
```

The music-volume automation is `0.94` before 2.00 s, `0.98` in the detected
6.65-7.30 s and 15.65-16.40 s gaps, `1.00` after 22.90 s, and `0.78`
otherwise. The programme arc is `1.00` to 5.80 s, `0.78` to 12.80 s, `0.68`
to 19.80 s, `0.82` to 22.90 s, then `1.00`.

### Motivation A: premix, pass 1, pass 2

The exact premix command was:

```powershell
& $ff -hide_banner -loglevel error -y `
  -i 'factory/pilots/client-final-motivation-a-v2-20260829/assets/audio/speaker-leveled-v2.m4a' `
  -i 'factory/pilots/client-final-motivation-a-v2-20260829/assets/audio/music-bed-leveled.m4a' `
  -filter_complex "[0:a]aresample=48000,pan=stereo|c0=0.707*c0|c1=0.707*c0,asplit=2[voice_mix][voice_sc];[1:a]aresample=48000,equalizer=f=1800:t=q:w=0.9:g=-3,volume='if(lt(t,1.30),0.94,if(between(t,6.85,7.60),1.0,if(gt(t,27.20),1.0,0.82)))':eval=frame[bed];[bed][voice_sc]sidechaincompress=threshold=0.12:ratio=1.25:attack=18:release=260:makeup=1[ducked];[voice_mix][ducked]amix=inputs=2:weights='1 1':normalize=0:duration=shortest[mix];[mix]volume='if(lt(t,7.0),1.0,if(lt(t,17.0),0.82,if(lt(t,24.0),0.72,if(lt(t,27.2),0.84,1.0))))':eval=frame[premix]" `
  -map '[premix]' -ar 48000 -ac 2 -c:a pcm_s24le `
  "$work\mot-a-premix-final-v2.wav"
```

Pass 1 and the exact measured pass 2 were:

```powershell
& $ff -hide_banner -nostats `
  -i "$work\mot-a-premix-final-v2.wav" `
  -af "loudnorm=I=-14.5:LRA=7:TP=-1.5:print_format=json" `
  -f null NUL

& $ff -hide_banner -loglevel error -y `
  -i "$work\mot-a-premix-final-v2.wav" `
  -af "loudnorm=I=-14.5:LRA=7:TP=-1.5:measured_I=-16.58:measured_LRA=4.00:measured_TP=-2.16:measured_thresh=-26.58:offset=0.58:linear=true:print_format=summary,alimiter=limit=0.841395:attack=5:release=50:level=false:latency=true,asetpts=N/SR/TB,atrim=duration=29.2" `
  -ar 48000 -ac 2 -c:a pcm_s24le `
  'factory/pilots/client-final-motivation-a-v2-20260829/assets/audio/motivation-a-audio-preview-v3.wav'
```

The music-volume automation is `0.94` before 1.30 s, `1.00` in the detected
6.85-7.60 s gap and after 27.20 s, and `0.82` otherwise. The programme arc is
`1.00` to 7.00 s, `0.82` to 17.00 s, `0.72` to 24.00 s, `0.84` to 27.20 s,
then `1.00`. `asetpts` resets the encoded PCM timestamps and `atrim` fixes the
preview duration to 29.20 s.

### Motivation B: premix, pass 1, pass 2

The exact premix command was:

```powershell
& $ff -hide_banner -loglevel error -y `
  -i 'factory/pilots/client-final-motivation-b-v2-20260829/assets/audio/speech.m4a' `
  -i 'factory/pilots/client-final-motivation-b-v2-20260829/assets/audio/dark-tension-bed-dynamic.m4a' `
  -filter_complex "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[voice_mix][voice_sc];[1:a]aresample=48000,equalizer=f=1800:t=q:w=0.9:g=-3,volume='if(lt(t,1.40),0.90,if(between(t,4.18,5.45),1.0,if(between(t,8.50,9.12),0.96,if(between(t,25.60,26.30),0.98,if(gt(t,29.45),1.0,0.70)))))':eval=frame[bed];[bed][voice_sc]sidechaincompress=threshold=0.08:ratio=1.45:attack=18:release=280:makeup=1[ducked];[voice_mix][ducked]amix=inputs=2:weights='1 1':normalize=0:duration=first[mix];[mix]volume='if(lt(t,4.18),1.0,if(lt(t,9.12),0.82,if(lt(t,17.0),0.72,if(lt(t,25.60),0.84,1.0))))':eval=frame[premix]" `
  -map '[premix]' -ar 48000 -ac 2 -c:a pcm_s24le `
  "$work\mot-b-premix-final-v2.wav"
```

Pass 1 and the exact measured pass 2 were:

```powershell
& $ff -hide_banner -nostats `
  -i "$work\mot-b-premix-final-v2.wav" `
  -af "loudnorm=I=-14.5:LRA=7:TP=-1.5:print_format=json" `
  -f null NUL

& $ff -hide_banner -loglevel error -y `
  -i "$work\mot-b-premix-final-v2.wav" `
  -af "loudnorm=I=-14.5:LRA=7:TP=-1.5:measured_I=-17.20:measured_LRA=2.30:measured_TP=-2.03:measured_thresh=-27.20:offset=0.45:linear=true:print_format=summary,alimiter=limit=0.841395:attack=5:release=50:level=false:latency=true,asetpts=N/SR/TB,apad=whole_dur=31.2,atrim=duration=31.2" `
  -ar 48000 -ac 2 -c:a pcm_s24le `
  'factory/pilots/client-final-motivation-b-v2-20260829/assets/audio/motivation-b-audio-preview-v3.wav'
```

The music-volume automation is `0.90` before 1.40 s, `1.00` at 4.18-5.45 s
and after 29.45 s, `0.96` at 8.50-9.12 s, `0.98` at 25.60-26.30 s, and
`0.70` otherwise. The programme arc is `1.00` to 4.18 s, `0.82` to 9.12 s,
`0.72` to 17.00 s, `0.84` to 25.60 s, then `1.00`. `asetpts`, `apad`, and
`atrim` preserve the authoritative 31.20-second duration; the added tail is
silence.

Measured premix values supplied to pass 2:

| Candidate | measured_I | measured_TP | measured_LRA | measured_thresh | offset |
| --- | ---: | ---: | ---: | ---: | ---: |
| Celebrity | -15.95 | -2.84 | 3.80 | -25.95 | 0.43 |
| Motivation A | -16.58 | -2.16 | 4.00 | -26.58 | 0.58 |
| Motivation B | -17.20 | -2.03 | 2.30 | -27.20 | 0.45 |

`alimiter` is the last dynamics processor. `asetpts` plus `atrim`/`apad` is used
after it only where needed to preserve the authoritative media duration.

## Integrity

| Candidate | SHA-256 |
| --- | --- |
| Celebrity | `AE03109AEF7E7CF041D5C555FACCAAB6D15837CD0784DC9CC46C703474E90E4F` |
| Motivation A | `43000D26486CEA29995B9041A0327B9F41F4D3FA106432F9233B6086260AA747` |
| Motivation B | `AF3C96EB80C8FBD96AF07A2E9BA277D69B2ECA7AE86A1FB69BED12E16DDABC5B` |

## Honest limitation

The celebrity voice timbre cannot be replaced by mixing. There is no alternate
approved voice take in this project, and absolute tone of an unknown voice is
not diagnosable from spectrum alone. This preview improves voice/music
separation and dynamics only. A different voice requires a separately approved
recording or TTS generation. Human listening approval is still required for all
three candidates.
