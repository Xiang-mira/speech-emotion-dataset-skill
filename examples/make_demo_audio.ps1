param([Parameter(Mandatory=$true)][string]$Project)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$root = (Resolve-Path -LiteralPath $Project).Path
$rows = Import-Csv -LiteralPath (Join-Path $root 'collection.csv') -Encoding UTF8
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voices = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'zh-CN' } | ForEach-Object { $_.VoiceInfo.Name } | Sort-Object)
    if ($voices.Count -eq 0) { throw 'The sample generator needs an installed zh-CN System.Speech voice.' }
    $audioFormat = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    foreach ($row in $rows) {
        $path = [IO.Path]::GetFullPath((Join-Path $root $row.audio))
        if (-not $path.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid output path.' }
        if (Test-Path -LiteralPath $path) { throw "A recording already exists: $path" }
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path)) | Out-Null
        $index = if ($row.speaker_id -eq 'SPK-B' -and $voices.Count -gt 1) { 1 } else { 0 }
        $synth.SelectVoice($voices[$index])
        $rate = if ($row.sample_id.EndsWith('A')) { '+8%' } elseif ($row.sample_id.EndsWith('B')) { '-7%' } else { '0%' }
        $pitch = if ($row.sample_id.EndsWith('B')) { '+18%' } elseif ($row.sample_id.EndsWith('A')) { '+6%' } else { '0%' }
        $spoken = [Security.SecurityElement]::Escape($row.text)
        $phrase = if ($row.pair_id) { "<prosody rate='$rate' pitch='$pitch'>$spoken</prosody>" } else { $spoken }
        $ssml = "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>$phrase</speak>"
        $synth.SetOutputToWaveFile($path, $audioFormat)
        try { $synth.SpeakSsml($ssml) } finally { $synth.SetOutputToNull() }
    }
} finally { $synth.Dispose() }
