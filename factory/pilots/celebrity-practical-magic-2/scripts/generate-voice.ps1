$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $PSScriptRoot
$audioDir = Join-Path $projectDir 'assets\audio'
New-Item -ItemType Directory -Force -Path $audioDir | Out-Null

$scriptPath = Join-Path $projectDir 'SCRIPT.md'
$lines = @(
  Get-Content -LiteralPath $scriptPath -Encoding UTF8 |
    Where-Object { $_ -match '^\d+\.\s+' } |
    ForEach-Object { $_ -replace '^\d+\.\s+', '' }
)

if ($lines.Count -ne 9) {
  throw "Expected 9 narration lines in SCRIPT.md, found $($lines.Count)."
}

Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speaker.SelectVoice('Microsoft Irina Desktop')
$speaker.Rate = 2
$speaker.Volume = 100

try {
  for ($i = 0; $i -lt $lines.Count; $i++) {
    $number = ($i + 1).ToString('00')
    $target = Join-Path $audioDir ("vo-$number.wav")
    if (Test-Path -LiteralPath $target) {
      Remove-Item -LiteralPath $target -Force
    }
    $speaker.SetOutputToWaveFile($target)
    $speaker.Speak($lines[$i])
    $speaker.SetOutputToNull()
  }
}
finally {
  $speaker.Dispose()
}

$lines | ConvertTo-Json -Depth 2 | Set-Content -LiteralPath (Join-Path $audioDir 'voice-lines.json') -Encoding UTF8
