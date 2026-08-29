[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Alias('Input')]
    [string]$InputFile,

    [Parameter(Mandatory = $true)]
    [string]$Master,

    [Parameter(Mandatory = $true)]
    [string]$Telegram,

    [Parameter(Mandatory = $true)]
    [double]$Duration,

    [string]$Report
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$ffmpeg = 'C:\Users\ns277\bin\ffmpeg.exe'
$ffprobe = 'C:\Users\ns277\bin\ffprobe.exe'
$inputPath = (Resolve-Path -LiteralPath $InputFile).Path
$masterPath = [System.IO.Path]::GetFullPath($Master)
$telegramPath = [System.IO.Path]::GetFullPath($Telegram)

foreach ($path in @($masterPath, $telegramPath)) {
    $parent = Split-Path -Parent $path
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
}

# Windows PowerShell treats FFmpeg's normal stderr progress as a native error.
# From this point on, rely on LASTEXITCODE and explicit validation instead.
$ErrorActionPreference = 'Continue'

function Measure-Loudness {
    param([Parameter(Mandatory = $true)][string]$InputPath)

    $log = (& $ffmpeg -hide_banner -nostats -i $InputPath `
        -map 0:a:0 -af 'loudnorm=I=-14.5:TP=-2.0:LRA=7:print_format=json' `
        -f null NUL 2>&1 | Out-String)
    $start = $log.LastIndexOf('{')
    $end = $log.LastIndexOf('}')
    if ($start -lt 0 -or $end -le $start) {
        throw "Could not parse loudnorm analysis for $InputPath"
    }
    return ($log.Substring($start, $end - $start + 1) | ConvertFrom-Json)
}

function Encode-Variant {
    param(
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [Parameter(Mandatory = $true)][int]$Crf,
        [Parameter(Mandatory = $true)][string]$AudioBitrate,
        [string]$VideoFilter = '',
        [double]$LoudnessTolerance = 0.5,
        [string]$VideoBitrate = ''
    )

    $postGainDb = 0.0
    $encodedMeasure = $null
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        $audioFilter = "loudnorm=I=-14.0:TP=-2.2:LRA=7," +
            "volume=$($postGainDb.ToString([Globalization.CultureInfo]::InvariantCulture))dB," +
            'alimiter=limit=0.80:level=false:attack=5:release=50'

        $arguments = @('-hide_banner', '-y', '-i', $inputPath, '-map', '0:v:0', '-map', '0:a:0')
        if ($VideoFilter) { $arguments += @('-vf', $VideoFilter) }
        $arguments += @(
            '-af', $audioFilter,
            '-t', $Duration.ToString([Globalization.CultureInfo]::InvariantCulture), '-r', '30',
            '-c:v', 'libx264', '-preset', 'slow',
            '-profile:v', 'high', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', $AudioBitrate, '-ar', '48000', '-ac', '2',
            '-movflags', '+faststart', $OutputPath
        )
        if ($VideoBitrate) {
            $outputIndex = $arguments.Count - 1
            $arguments = @($arguments[0..($outputIndex - 1)]) + @('-b:v', $VideoBitrate, '-maxrate', '750k', '-bufsize', '1300k') + @($arguments[$outputIndex])
        }
        else {
            $outputIndex = $arguments.Count - 1
            $arguments = @($arguments[0..($outputIndex - 1)]) + @('-crf', "$Crf") + @($arguments[$outputIndex])
        }

        & $ffmpeg @arguments
        if ($LASTEXITCODE -ne 0) { throw "Delivery encode failed: $OutputPath" }

        $encodedMeasure = Measure-Loudness -InputPath $OutputPath
        $encodedTp = [double]$encodedMeasure.input_tp
        $encodedI = [double]$encodedMeasure.input_i
        $loudnessError = -14.5 - $encodedI
        if ($encodedTp -le -1.2 -and [math]::Abs($loudnessError) -le $LoudnessTolerance) { break }

        if ([math]::Abs($loudnessError) -gt $LoudnessTolerance) {
            $postGainDb += $loudnessError
        }
        if ($attempt -eq 4) {
            throw "Delivery audio remains outside target: $encodedI LUFS-I / $encodedTp dBTP"
        }
    }

    $integrated = [double]$encodedMeasure.input_i
    if ([math]::Abs($integrated + 14.5) -gt $LoudnessTolerance) {
        throw "Encoded loudness outside tolerance: $integrated LUFS-I ($OutputPath)"
    }

    return [pscustomobject]@{
        path = $OutputPath
        lufs_i = $integrated
        true_peak_dbtp = [double]$encodedMeasure.input_tp
        lra_lu = [double]$encodedMeasure.input_lra
        post_gain_db = $postGainDb
    }
}

$masterMeasure = Encode-Variant -OutputPath $masterPath -Crf 16 -AudioBitrate '192k'
$telegramMeasure = Encode-Variant -OutputPath $telegramPath -Crf 20 -AudioBitrate '128k' -VideoFilter 'scale=720:1280:flags=lanczos' -LoudnessTolerance 1.0 -VideoBitrate '650k'

$result = [pscustomobject]@{
    generated_at = (Get-Date).ToString('o')
    input = $inputPath
    master = [pscustomobject]@{
        loudness = $masterMeasure
        probe = (& $ffprobe -v error -show_entries format=duration,size,bit_rate -show_entries stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $masterPath | ConvertFrom-Json)
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $masterPath).Hash
    }
    telegram = [pscustomobject]@{
        loudness = $telegramMeasure
        probe = (& $ffprobe -v error -show_entries format=duration,size,bit_rate -show_entries stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels -of json $telegramPath | ConvertFrom-Json)
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $telegramPath).Hash
    }
}

if ($Report) {
    $reportPath = [System.IO.Path]::GetFullPath($Report)
    $reportParent = Split-Path -Parent $reportPath
    if (-not (Test-Path -LiteralPath $reportParent)) {
        New-Item -ItemType Directory -Path $reportParent -Force | Out-Null
    }
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $reportPath -Encoding utf8
}

$result | ConvertTo-Json -Depth 10
