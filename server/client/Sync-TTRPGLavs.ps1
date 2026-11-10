<#
.SYNOPSIS
    Aligns the player lav recordings of a session on the clap and writes synced FLAC files.

.DESCRIPTION
    Works with sessions from the new recorder (segmented FLAC, possibly several parts after a
    restart) and with older sessions (one WAV per player in players\).

    For every part it finds the clap (loudest 20 ms burst in the search window) on each
    player's track, trims the late tracks so all claps line up, then joins the parts using
    their recorded start times so the timeline stays continuous.

    After finding the clap it shows the times and asks whether to use them, ignore the clap
    (keep the tracks as recorded), or type the clap times in yourself. -Yes skips the question.

    Single-mic sessions (record.ps1 -Single) have one track, the room mic: its parts are joined
    the same way (no clap needed) into synced_Room.flac.

    Output: <session>\00_synced\synced_<Player>.flac (16 kHz mono) and sync_manifest.json.
    No Python needed. The original recordings are never modified.

.PARAMETER SessionDir
    Session folder. Default: the most recent recording (latest_session.json).

.PARAMETER SyncWindowSeconds
    How many seconds from the start of each part to search for the clap. Default: config.json.

.PARAMETER Submit
    Upload to the processing server afterwards (runs Submit-Session.ps1).

.PARAMETER Force
    Re-sync even if synced files already exist.

.PARAMETER NoClap
    Don't look for a clap: keep the tracks as recorded. They start together, so they're
    usually within ~0.1 s of each other.

.PARAMETER ClapTimes
    Clap times you read off yourself (e.g. in Audacity), in seconds from the start of the part's
    first segment, one per player:  -ClapTimes Player1=4.04,Player2=4.05,Player3=4.03,Player4=4.06
    For a later part, put the part number first:  -ClapTimes 2:Player1=3.10,2:Player2=3.12,...
    Parts without times are handled as usual.

.PARAMETER Yes
    Don't ask what to do with the clap; take the default (use it when it looks right,
    otherwise keep the tracks as recorded).

.EXAMPLE
    .\Sync-TTRPGLavs.ps1 -Submit
.EXAMPLE
    .\Sync-TTRPGLavs.ps1 -SessionDir "C:\Intel\Recorder\session_2026-10-15_12-06-27" -SyncWindowSeconds 5 -Submit
.EXAMPLE
    .\Sync-TTRPGLavs.ps1 -SessionDir "C:\Intel\Recorder\session_2026-10-15_12-06-27" -Force -NoClap -Submit
#>
[CmdletBinding()]
param(
    [string]$SessionDir = "",
    [double]$SyncWindowSeconds = -1,
    [double]$SearchStartSeconds = -1,
    [double]$MaxClapSpreadSeconds = 1.0,
    [switch]$Submit,
    [switch]$Force,
    [switch]$KeepWork,
    [switch]$NoClap,
    [string[]]$ClapTimes = @(),
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "TTRPG.Common.ps1")

$Config = Get-TtrpgConfig
$FFMPEG = Get-FFmpegPath "ffmpeg"
if ($SyncWindowSeconds -le 0) { $SyncWindowSeconds = [double]$Config.sync.windowSeconds }
if ($SearchStartSeconds -lt 0) { $SearchStartSeconds = [double]$Config.sync.searchStartSeconds }

if (-not ("TtrpgClap" -as [type])) {
    Add-Type -TypeDefinition @"
public static class TtrpgClap {
    // pcm: 16-bit little-endian mono. Returns { peakSeconds, peakValue }.
    public static double[] FindPeak(byte[] pcm, int sampleRate) {
        int n = pcm.Length / 2;
        if (n == 0) return new double[] { 0, 0 };
        int win = System.Math.Max(1, sampleRate / 50);   // 20 ms
        double[] cum = new double[n + 1];
        for (int i = 0; i < n; i++) {
            short s = (short)(pcm[2 * i] | (pcm[2 * i + 1] << 8));
            cum[i + 1] = cum[i] + System.Math.Abs(s / 32768.0);
        }
        int best = 0; double bestVal = -1;
        int half = win / 2;
        for (int i = 0; i < n; i++) {
            int a = System.Math.Max(0, i - half), b = System.Math.Min(n, i - half + win);
            if (b <= a) continue;
            double v = (cum[b] - cum[a]) / win;
            if (v > bestVal) { bestVal = v; best = i; }
        }
        return new double[] { (double)best / sampleRate, bestVal };
    }
}
"@
}

# ---------------------------------------------------------------- clap options

$Inv = [Globalization.CultureInfo]::InvariantCulture

function ConvertTo-Seconds([string]$Text) {
    $v = 0.0
    if ([double]::TryParse($Text.Trim(), [Globalization.NumberStyles]::Float, $Inv, [ref]$v) -and $v -ge 0) { return $v }
    return $null
}

# -ClapTimes Player1=4.04,2:Player1=3.1 -> @{ 1 = @{ Player1 = 4.04 }; 2 = @{ Player1 = 3.1 } }
$ManualClaps = @{}
foreach ($entry in @($ClapTimes | ForEach-Object { "$_" -split ',' } | Where-Object { $_.Trim() })) {
    if ($entry -notmatch '^\s*(?:(\d+)\s*:)?\s*([^=:]+?)\s*=\s*(\S+)\s*$') {
        throw "Can't read -ClapTimes entry '$entry'. Use Player1=4.04 with a dot for decimals (2:Player1=4.04 for part 2)."
    }
    $pi = if ($Matches[1]) { [int]$Matches[1] } else { 1 }
    $name = $Matches[2]
    $sec = ConvertTo-Seconds $Matches[3]
    if ($null -eq $sec) { throw "-ClapTimes entry '$entry': '$($Matches[3])' isn't a number of seconds (use a dot: 4.04)." }
    if (-not $ManualClaps.ContainsKey($pi)) { $ManualClaps[$pi] = @{} }
    $ManualClaps[$pi][$name] = $sec
}

$CanAsk = $false
try { $CanAsk = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected } catch { }

# Asks a question, or returns $Default with -Yes / when nobody can answer.
function Read-Answer([string]$Prompt, [string]$Default) {
    if ($Yes -or -not $CanAsk) { return $Default }
    try { $a = Read-Host $Prompt } catch { return $Default }
    if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
    return $a.Trim()
}

# ---------------------------------------------------------------- locate session

if ([string]::IsNullOrWhiteSpace($SessionDir)) {
    $latest = Read-JsonFile (Join-Path $PSScriptRoot "latest_session.json")
    if (-not $latest -or -not $latest.sessionDir) { throw "No -SessionDir given and latest_session.json not found." }
    $SessionDir = [string]$latest.sessionDir
}
$SessionDir = (Resolve-Path $SessionDir).Path.TrimEnd('\', '/')
$manifest = Read-JsonFile (Join-Path $SessionDir "session_manifest.json")
$folderName = Split-Path $SessionDir -Leaf
$sessionTimestamp = if ($manifest -and $manifest.PSObject.Properties.Name -contains "sessionTimestamp") { [string]$manifest.sessionTimestamp } else { $folderName -replace '^session_', '' }
$sessionDate = if ($sessionTimestamp -match '^\d{4}-\d{2}-\d{2}') { $Matches[0] } else { (Get-Item $SessionDir).CreationTime.ToString("yyyy-MM-dd") }
$recordedDir = if ($manifest -and $manifest.PSObject.Properties.Name -contains "sessionDir") { [string]$manifest.sessionDir } else { $SessionDir }

if ($manifest -and $manifest.PSObject.Properties.Name -contains "status" -and $manifest.status -eq "recording") {
    $active = Read-JsonFile (Join-Path $PSScriptRoot "active_recording.json")
    if ($active -and $active.sessionDir -eq $manifest.sessionDir) {
        throw "This session is still recording (or was interrupted). Run record.ps1 first to stop or finish it."
    }
}

$OutDir = Join-Path $SessionDir "00_synced"
$WorkDir = Join-Path $OutDir "_work"
$SyncManifestPath = Join-Path $OutDir "sync_manifest.json"

# ---------------------------------------------------------------- build part list

# Each part: @{ Index; StartMs; Tracks = ordered name -> @(input files) }
$parts = @()
$isV3 = $manifest -and ($manifest.PSObject.Properties.Name -contains "version") -and [int]$manifest.version -ge 3
$Mode = if ($isV3 -and ($manifest.PSObject.Properties.Name -contains "mode") -and $manifest.mode -eq "single") { "single" } else { "lavs" }
if ($isV3) {
    # track name -> file prefix written by the recorder
    $trackPrefixes = [ordered]@{}
    if ($Mode -eq "single") { $trackPrefixes["Room"] = "room" }
    else { foreach ($pl in @($manifest.players)) { $trackPrefixes[[string]$pl.name] = [string]$pl.name } }
    foreach ($p in @($manifest.parts)) {
        $partDir = Resolve-SessionPath ([string]$p.dir) $SessionDir $recordedDir
        if (-not (Test-Path $partDir)) { Write-Warn2 "Part $($p.index): folder missing ($partDir); skipping"; continue }
        $tracks = [ordered]@{}
        foreach ($name in $trackPrefixes.Keys) {
            $segs = @(Get-ChildItem -Path $partDir -Filter "$($trackPrefixes[$name])_*.flac" | Where-Object { $_.Length -gt 0 } | Sort-Object Name | ForEach-Object { $_.FullName })
            if ($segs.Count) { $tracks[$name] = $segs }
        }
        if ($tracks.Count -eq 0) { Write-Warn2 "Part $($p.index) has no audio; skipping"; continue }
        if ($tracks.Count -lt $trackPrefixes.Count) { Write-Warn2 "Part $($p.index) is missing some players' audio" }
        $parts += [pscustomobject]@{ Index = [int]$p.index; StartMs = [int64]$p.startedAtUnixMs; Tracks = $tracks }
    }
}
else {
    $tracks = [ordered]@{}
    if ($manifest -and $manifest.PSObject.Properties.Name -contains "players") {
        foreach ($pl in @($manifest.players)) {
            $f = Resolve-SessionPath ([string]$pl.file) $SessionDir $recordedDir
            if (-not (Test-Path $f)) {
                $alt = Join-Path (Join-Path $SessionDir "players") (Split-Path $f -Leaf)
                if (Test-Path $alt) { $f = $alt } else { throw "Recording for $($pl.name) not found: $f" }
            }
            $tracks[[string]$pl.name] = @($f)
        }
    }
    else {
        $i = 1
        foreach ($f in @(Get-ChildItem -Path (Join-Path $SessionDir "players") -Filter "*.wav" | Sort-Object Name)) {
            $tracks["Player$i"] = @($f.FullName); $i++
        }
    }
    if ($tracks.Count -eq 0) { throw "No player recordings found in $SessionDir" }
    $parts += [pscustomobject]@{ Index = 1; StartMs = 0; Tracks = $tracks }
}
if ($parts.Count -eq 0) { throw "No recorded audio found in $SessionDir" }
$names = @()
foreach ($p in $parts) { foreach ($n in $p.Tracks.Keys) { if ($names -notcontains $n) { $names += $n } } }

# ---------------------------------------------------------------- already done?

if (($NoClap -or $ManualClaps.Count) -and -not $Force -and (Test-Path $SyncManifestPath)) {
    Write-Info "Re-syncing because -NoClap / -ClapTimes was given."
    $Force = [switch]$true
}
$expectedOutputs = $names | ForEach-Object { Join-Path $OutDir "synced_$_.flac" }
if (-not $Force -and (Test-Path $SyncManifestPath) -and -not ($expectedOutputs | Where-Object { -not (Test-Path $_) })) {
    Write-Ok "Session is already synced ($OutDir). Use -Force to redo it."
}
else {
    New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null
    Write-Host ""
    Write-Info "Session:   $SessionDir"
    Write-Info "Tracks:    $($names -join ', ')$(if ($Mode -eq 'single') { '  (single mic)' })"
    Write-Info "Parts:     $($parts.Count)"
    if ($Mode -ne "single") { Write-Info "Clap search: $SearchStartSeconds s + $SyncWindowSeconds s" }

    $report = @{}
    foreach ($n in $names) { $report[$n] = @() }
    $alignedByPart = @()

    foreach ($part in $parts) {
        $tag = "p{0:D2}" -f $part.Index
        Write-Host ""
        Write-Info "Part $($part.Index)"

        # a) one continuous file per player for this part
        $joined = [ordered]@{}
        foreach ($n in $part.Tracks.Keys) {
            $files = @($part.Tracks[$n])
            $dst = Join-Path $WorkDir "${tag}_$n.flac"
            if ($files.Count -eq 1) {
                Invoke-Tool $FFMPEG @("-nostdin", "-y", "-loglevel", "error", "-i", $files[0], "-ac", "1", "-ar", "16000", "-c:a", "flac", $dst) | Out-Null
            }
            else {
                $list = Join-Path $WorkDir "${tag}_$n.txt"
                $lines = @("ffconcat version 1.0") + ($files | ForEach-Object { "file '" + ($_ -replace "'", "'\''") + "'" })
                [IO.File]::WriteAllLines($list, $lines)
                Invoke-Tool $FFMPEG @("-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", $list,
                                      "-ac", "1", "-ar", "16000", "-c:a", "flac", $dst) | Out-Null
            }
            $joined[$n] = $dst
        }

        # b) find the clap on each track (nothing to line up with a single track)
        #    clapSource: detected | manual | none (tracks kept as recorded)
        $peaks = [ordered]@{}
        $clapSource = "none"
        $manual = if ($ManualClaps.ContainsKey($part.Index)) { $ManualClaps[$part.Index] } else { $null }
        if ($joined.Count -eq 1) {
            foreach ($n in $joined.Keys) { $peaks[$n] = 0.0 }
        }
        elseif ($manual) {
            $missing = @($joined.Keys | Where-Object { -not $manual.ContainsKey($_) })
            if ($missing.Count) { throw "-ClapTimes gives part $($part.Index) times for some tracks but not: $($missing -join ', ')" }
            foreach ($n in $joined.Keys) {
                $peaks[$n] = [double]$manual[$n]
                Write-Host ("   {0,-10} clap at {1,8:N3} s   (from -ClapTimes)" -f $n, $peaks[$n])
            }
            $clapSource = "manual"
        }
        elseif ($NoClap) {
            foreach ($n in $joined.Keys) { $peaks[$n] = 0.0 }
            Write-Info "Not using a clap (-NoClap): keeping the tracks as recorded."
        }
        else {
            foreach ($n in $joined.Keys) {
                $probe = Join-Path $WorkDir "${tag}_$n.probe.raw"
                Invoke-Tool $FFMPEG @("-nostdin", "-y", "-loglevel", "error", "-ss", (Format-Inv $SearchStartSeconds "0.000"),
                                      "-t", (Format-Inv $SyncWindowSeconds "0.000"), "-i", $joined[$n],
                                      "-ac", "1", "-ar", "16000", "-af", "highpass=f=80", "-f", "s16le", $probe) | Out-Null
                $r = [TtrpgClap]::FindPeak([IO.File]::ReadAllBytes($probe), 16000)
                $peaks[$n] = $SearchStartSeconds + $r[0]
                Write-Host ("   {0,-10} clap at {1,8:N3} s   (strength {2:N4})" -f $n, $peaks[$n], $r[1])
            }
            $spread = ($peaks.Values | Measure-Object -Maximum).Maximum - ($peaks.Values | Measure-Object -Minimum).Minimum
            $looksOk = $spread -le $MaxClapSpreadSeconds
            if ($looksOk) {
                Write-Ok ("The clap lines up across tracks within {0:N0} ms." -f ($spread * 1000))
            }
            else {
                Write-Warn2 ("Clap times differ by {0:N2} s (more than {1} s), so the clap probably wasn't found on every track." -f $spread, $MaxClapSpreadSeconds)
            }

            $default = if ($looksOk) { "C" } else { "N" }
            if (-not $Yes -and $CanAsk) {
                Write-Host "   C = use these clap times"
                Write-Host "   N = ignore the clap and keep the tracks as recorded (they start together, usually within ~0.1 s)"
                Write-Host "   M = type in the clap time for each track yourself (e.g. read off in Audacity)"
            }
            $prompt = if ($looksOk) { "Part $($part.Index): use the clap? [C/n/m]" } else { "Part $($part.Index): what now? [c/N/m]" }
            $clapSource = $null
            while (-not $clapSource) {
                $ans = (Read-Answer $prompt $default).Substring(0, 1).ToUpperInvariant()
                switch ($ans) {
                    "C" { $clapSource = "detected" }
                    "N" { $clapSource = "none" }
                    "M" {
                        Write-Host "   Times are in seconds from the start of each track's first file in this part"
                        Write-Host "   (open that file in Audacity and click on the clap). Enter keeps the detected time."
                        foreach ($n in @($joined.Keys)) {
                            $first = Split-Path ([string]@($part.Tracks[$n])[0]) -Leaf
                            while ($true) {
                                $t = Read-Answer ("   {0} ({1}) clap at [{2}]" -f $n, $first, (Format-Inv $peaks[$n] "0.000")) (Format-Inv $peaks[$n] "0.000")
                                $v = ConvertTo-Seconds $t
                                if ($null -ne $v) { $peaks[$n] = $v; break }
                                Write-Warn2 "'$t' isn't a number of seconds (use a dot: 4.04)."
                            }
                        }
                        $spread = ($peaks.Values | Measure-Object -Maximum).Maximum - ($peaks.Values | Measure-Object -Minimum).Minimum
                        if ($spread -gt $MaxClapSpreadSeconds) {
                            Write-Warn2 ("Those times are {0:N2} s apart. Using them anyway; re-run with -Force if that's wrong." -f $spread)
                        }
                        $clapSource = "manual"
                    }
                    default { Write-Warn2 "Type C, N or M." }
                }
            }
        }
        $ref = ($peaks.Values | Measure-Object -Minimum).Minimum
        $useClap = $joined.Count -gt 1 -and $clapSource -ne "none"
        if ($joined.Count -gt 1 -and -not $useClap) {
            Write-Info "Part $($part.Index): no offsets, tracks kept as recorded."
        }
        if ($useClap) {
            foreach ($n in $joined.Keys) {
                if (($peaks[$n] - $ref) -ge (Get-MediaDuration $joined[$n])) {
                    throw "The clap time for $n in part $($part.Index) ($($peaks[$n]) s) is past the end of its recording."
                }
            }
        }

        # c) trim late tracks
        $aligned = [ordered]@{}
        foreach ($n in $joined.Keys) {
            $delta = if ($useClap) { $peaks[$n] - $ref } else { 0.0 }
            $dst = Join-Path $WorkDir "${tag}_$n.aligned.flac"
            if ($delta -ge 0.0005) {
                Invoke-Tool $FFMPEG @("-nostdin", "-y", "-loglevel", "error", "-i", $joined[$n],
                                      "-af", ("atrim=start={0},asetpts=PTS-STARTPTS" -f (Format-Inv $delta)),
                                      "-c:a", "flac", $dst) | Out-Null
                Write-Host ("   {0,-10} trimmed {1:N1} ms" -f $n, ($delta * 1000))
            }
            else {
                Move-Item -Path $joined[$n] -Destination $dst -Force
            }
            $aligned[$n] = $dst
            $report[$n] += [ordered]@{ part = $part.Index; clapSeconds = $peaks[$n]; referenceSeconds = $ref; deltaSeconds = $delta; clapUsed = $useClap; clapSource = $(if ($joined.Count -gt 1) { $clapSource } else { "single" }) }
        }
        $durations = @{}
        foreach ($n in $aligned.Keys) { $durations[$n] = Get-MediaDuration $aligned[$n] }
        $alignedByPart += [pscustomobject]@{ Part = $part; Files = $aligned; Durations = $durations
                                             MaxLen = ($durations.Values | Measure-Object -Maximum).Maximum }
    }

    # d) place parts on one timeline using their wall-clock start times
    $offsets = @()
    for ($k = 0; $k -lt $alignedByPart.Count; $k++) {
        if ($k -eq 0) { $offsets += 0.0; continue }
        $wall = ($alignedByPart[$k].Part.StartMs - $alignedByPart[0].Part.StartMs) / 1000.0
        $minStart = $offsets[$k - 1] + $alignedByPart[$k - 1].MaxLen
        if ($wall -lt $minStart) { $wall = $minStart }
        $offsets += $wall
        Write-Info ("Part {0} placed at {1:N1} s (gap of {2:N1} s after part {3})" -f $alignedByPart[$k].Part.Index, $wall, ($wall - $minStart), $alignedByPart[$k - 1].Part.Index)
    }

    $syncedRows = @()
    foreach ($n in $names) {
        $out = Join-Path $OutDir "synced_$n.flac"
        $pieces = @()
        for ($k = 0; $k -lt $alignedByPart.Count; $k++) {
            $ap = $alignedByPart[$k]
            $isLast = ($k -eq $alignedByPart.Count - 1)
            if (-not $ap.Files.Contains($n)) {
                # player missing in this part: silence for the whole part
                $len = if ($isLast) { $ap.MaxLen } else { $offsets[$k + 1] - $offsets[$k] }
                $sil = Join-Path $WorkDir ("silence_{0}_{1}.flac" -f $k, $n)
                Invoke-Tool $FFMPEG @("-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                                      "-t", (Format-Inv $len "0.000"), "-sample_fmt", "s16", "-c:a", "flac", $sil) | Out-Null
                $pieces += $sil
                continue
            }
            if ($isLast) { $pieces += $ap.Files[$n]; continue }
            $target = $offsets[$k + 1] - $offsets[$k]
            $padded = Join-Path $WorkDir ("padded_{0}_{1}.flac" -f $k, $n)
            Invoke-Tool $FFMPEG @("-nostdin", "-y", "-loglevel", "error", "-i", $ap.Files[$n],
                                  "-af", ("apad=whole_dur={0},atrim=end={0}" -f (Format-Inv $target "0.000")),
                                  "-c:a", "flac", $padded) | Out-Null
            $pieces += $padded
        }
        if ($pieces.Count -eq 1) {
            Copy-Item -Path $pieces[0] -Destination $out -Force
        }
        else {
            $list = Join-Path $WorkDir "final_$n.txt"
            [IO.File]::WriteAllLines($list, @("ffconcat version 1.0") + ($pieces | ForEach-Object { "file '" + ($_ -replace "'", "'\''") + "'" }))
            Invoke-Tool $FFMPEG @("-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", $list, "-c:a", "flac", $out) | Out-Null
        }
        $syncedRows += [ordered]@{ speaker = $n; syncedFile = $out; parts = $report[$n] }
        Write-Ok "synced_$n.flac  ($((Get-Item $out).Length / 1MB | ForEach-Object { '{0:N1}' -f $_ }) MB)"
    }

    Write-JsonFile $SyncManifestPath ([ordered]@{
        version          = 3
        mode             = $Mode
        createdAt        = (Get-Date).ToString("o")
        sessionDir       = $SessionDir
        sessionTimestamp = $sessionTimestamp
        sessionDate      = $sessionDate
        outputDir        = $OutDir
        searchStartSeconds = $SearchStartSeconds
        syncWindowSeconds  = $SyncWindowSeconds
        partOffsetsSeconds = $offsets
        syncedLav        = $syncedRows
    }) 8

    if (-not $KeepWork) { Remove-Item -Path $WorkDir -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Ok "Synced files are in $OutDir"
}

Write-JsonFile (Join-Path $PSScriptRoot "latest_sync.json") ([ordered]@{
    createdAt        = (Get-Date).ToString("o")
    sessionDir       = $SessionDir
    syncManifestPath = $SyncManifestPath
    syncedDir        = $OutDir
})

if ($Submit) {
    & (Join-Path $PSScriptRoot "Submit-Session.ps1") -SessionDir $SessionDir
}
