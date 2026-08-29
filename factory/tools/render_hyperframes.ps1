[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Project,

    [Parameter(Mandatory = $true)]
    [string]$Output,

    [ValidateSet('draft', 'standard', 'high')]
    [string]$Quality = 'high',

    [ValidateRange(1, 240)]
    [int]$Fps = 30,

    [ValidateRange(0, 51)]
    [int]$Crf = 16,

    [ValidateRange(1, 2)]
    [int]$Workers = 1,

    [string]$HyperframesVersion = '0.8.17',

    [switch]$UseGpuEncoding
)

$ErrorActionPreference = 'Stop'
$bundledFfmpegDir = 'C:\Users\ns277\bin'
if (Test-Path -LiteralPath (Join-Path $bundledFfmpegDir 'ffmpeg.exe')) {
    $env:Path = "$bundledFfmpegDir;$env:Path"
}
$projectPath = (Resolve-Path -LiteralPath $Project).Path
$outputPath = if ([System.IO.Path]::IsPathRooted($Output)) {
    [System.IO.Path]::GetFullPath($Output)
} else {
    [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Output))
}
$outputParent = Split-Path -Parent $outputPath
if (-not (Test-Path -LiteralPath $outputParent)) {
    New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
}

$runtimeRoot = $env:VIDEO_FACTORY_RUNTIME_ROOT
if ([string]::IsNullOrWhiteSpace($runtimeRoot)) {
    $runtimeRoot = Join-Path $env:LOCALAPPDATA 'VideoFactoryRuntime'
}
$frameCache = Join-Path $runtimeRoot 'hyperframes-frames'
New-Item -ItemType Directory -Path $frameCache -Force | Out-Null

$arguments = @(
    '--yes',
    "hyperframes@$HyperframesVersion",
    'render',
    $projectPath,
    '--output', $outputPath,
    '--quality', $Quality,
    '--fps', $Fps,
    '--crf', $Crf,
    '--workers', $Workers,
    '--max-concurrent-renders', '1',
    '--frames-cache-dir', $frameCache,
    '--strict'
)
if ($UseGpuEncoding) {
    $arguments += '--gpu'
}

Write-Host "HyperFrames $HyperframesVersion | workers=$Workers | frame-cache=$frameCache"
& npx @arguments
if ($LASTEXITCODE -ne 0) {
    throw "HyperFrames render failed with exit code $LASTEXITCODE"
}

Write-Output $outputPath
