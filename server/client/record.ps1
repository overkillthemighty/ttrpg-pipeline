<#
.SYNOPSIS
    TTRPG multi-mic recorder (v3): crash-safe, segmented FLAC recording with resume.

.DESCRIPTION
    - Records every player's lav (2 per wireless receiver) and an optional room mic.
    - One ffmpeg process per device, split into channels, so both channels of a
      receiver stay sample-locked.
    - Writes FLAC in short segments (config: segmentSeconds). A crash, power cut
      or closed window loses at most a few seconds; everything already written
      stays playable.
    - ffmpeg runs in its own hidden console, so Ctrl+C or closing this window
      does not stop the recording. Run record.ps1 again to re-attach and stop it.
    - If a device stream dies or stalls, the recorder restarts all streams as a
      new "part" and asks for another clap so the sync step can realign it.
    - If the saved devices (last_devices.json) don't match the config or any of
      them is missing, the file is deleted and every device is chosen again.

.PARAMETER Reselect
    Ignore last_devices.json and choose every device again.

.PARAMETER SkipMicCheck
    Skip the level check before recording.

.PARAMETER Single
    Single-mic mode for this session: record only the room mic (no lav receivers, no clap).
    Speakers can't be told apart by lav level; the server can optionally guess them
    (processing.diarize in config.json, or Submit-Session.ps1 -Diarize).
    config.json "mode": "single" makes this the default.

.PARAMETER Lavs
    Force the normal lav mode for this session even if config.json says "single".

.EXAMPLE
    .\record.ps1
.EXAMPLE
    .\record.ps1 -Single
#>
[CmdletBinding()]
param(
    [switch]$Reselect,
    [switch]$SkipMicCheck,
    [switch]$Single,
    [switch]$Lavs
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "TTRPG.Common.ps1")

$Config = Get-TtrpgConfig
$FFMPEG = Get-FFmpegPath "ffmpeg"
$StateFile = Join-Path $PSScriptRoot "last_devices.json"
$ActiveFile = Join-Path $PSScriptRoot "active_recording.json"
$LatestSessionFile = Join-Path $PSScriptRoot "latest_session.json"
$StopFlag = Join-Path $PSScriptRoot "stop_recording.flag"   # create this file to stop from another window/script
$SingleStateFile = Join-Path $PSScriptRoot "last_single_device.json"
if ($Single -and $Lavs) { throw "Use either -Single or -Lavs, not both." }
$Mode = if ($Single) { "single" } elseif ($Lavs) { "lavs" } else { [string]$Config.mode }
if ($Mode -eq "single") {
    $Layout = @()
    $ReceiverCount = 0
}
else {
    $Layout = Get-PlayerLayout $Config
    $ReceiverCount = Get-ReceiverCount $Config
}

# The clap is only needed to line up several tracks.
function Get-ClapHint {
    param($Manifest, [string]$Text)
    if ($Manifest -and ($Manifest.PSObject.Properties.Name -contains "mode") -and $Manifest.mode -eq "single") { return "" }
    return " $Text"
}

# ================================================================ devices

function Get-AudioDevices {
    $raw = Invoke-Tool $FFMPEG @("-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy") -AllowFailure
    $devices = @()
    $currentName = $null
    foreach ($line in ($raw -split "`r?`n")) {
        if ($line -match '"(.+?)" \(audio\)') { $currentName = $Matches[1] }
        elseif ($line -match 'Alternative name "(.+?)"' -and $currentName) {
            $devices += [pscustomobject]@{ Name = $currentName; ID = $Matches[1] }
            $currentName = $null
        }
    }
    return $devices
}

function Get-ExpectedRoles {
    $roles = @()
    for ($r = 1; $r -le $ReceiverCount; $r++) { $roles += "rx$r" }
    if ($Config.roomMic -ne "none") { $roles += "room" }
    return $roles
}

function Get-SavedId($entry) {
    if ($null -eq $entry) { return $null }
    if ($entry -is [string]) { return $entry }           # v2 format stored just the ID
    if ($entry.PSObject.Properties.Name -contains "id") { return [string]$entry.id }
    return $null
}

# Returns a hashtable role -> device (or $null for a skipped optional room mic), or $null if
# the saved file doesn't match the current config / hardware (the file is then deleted).
function Get-SavedSelection {
    param([array]$Devices)
    if ($Reselect) {
        if (Test-Path $StateFile) { Remove-Item $StateFile -Force }
        return $null
    }
    $saved = Read-JsonFile $StateFile
    if ($null -eq $saved) { return $null }

    $expected = Get-ExpectedRoles
    $savedRoles = @($saved.PSObject.Properties.Name)
    $problem = $null
    $diff = Compare-Object -ReferenceObject $expected -DifferenceObject $savedRoles
    if ($diff) {
        $problem = "saved roles ($($savedRoles -join ', ')) don't match the config ($($expected -join ', '))"
    }
    $sel = @{}
    if (-not $problem) {
        foreach ($role in $expected) {
            $id = Get-SavedId $saved.$role
            if (-not $id) {
                if ($role -eq "room" -and $Config.roomMic -eq "optional") { $sel[$role] = $null; continue }
                $problem = "no device saved for $role"
                break
            }
            $match = $Devices | Where-Object { $_.ID -eq $id } | Select-Object -First 1
            if (-not $match) { $problem = "the saved device for $role is not connected"; break }
            $sel[$role] = $match
        }
    }
    if ($problem) {
        Write-Warn2 "Saved devices don't match ($problem). Removing last_devices.json and choosing all devices again."
        Remove-Item $StateFile -Force
        return $null
    }
    return $sel
}

function Read-DeviceChoice {
    param([string]$Label, [array]$Devices, [bool]$Optional, [string[]]$TakenIds)
    while ($true) {
        Write-Host ""
        Write-Host "Select ${Label}:" -ForegroundColor Cyan
        for ($i = 0; $i -lt $Devices.Count; $i++) {
            $mark = if ($TakenIds -contains $Devices[$i].ID) { "  (already used)" } else { "" }
            Write-Host ("  [{0}] {1}{2}" -f ($i + 1), $Devices[$i].Name, $mark)
        }
        $prompt = if ($Optional) { "Number (blank to skip)" } else { "Number" }
        $ans = Read-Host $prompt
        if ([string]::IsNullOrWhiteSpace($ans)) {
            if ($Optional) { return $null }
            continue
        }
        $n = 0
        if ([int]::TryParse($ans, [ref]$n) -and $n -ge 1 -and $n -le $Devices.Count) {
            $d = $Devices[$n - 1]
            if ($TakenIds -contains $d.ID) { Write-Warn2 "That device is already used for another receiver."; continue }
            return $d
        }
        Write-Warn2 "Enter a number from 1 to $($Devices.Count)."
    }
}

function Select-AllDevices {
    param([array]$Devices)
    $sel = @{}
    $taken = @()
    for ($r = 1; $r -le $ReceiverCount; $r++) {
        $players = ($Layout | Where-Object { $_.Receiver -eq $r } | ForEach-Object { $_.Name }) -join " + "
        $d = Read-DeviceChoice "Receiver $r ($players)" $Devices $false $taken
        $sel["rx$r"] = $d
        $taken += $d.ID
    }
    if ($Config.roomMic -ne "none") {
        $sel["room"] = Read-DeviceChoice "Room mic" $Devices ($Config.roomMic -eq "optional") @()
    }
    $state = [ordered]@{}
    foreach ($role in (Get-ExpectedRoles)) {
        $d = $sel[$role]
        $state[$role] = if ($d) { [ordered]@{ id = $d.ID; name = $d.Name } } else { $null }
    }
    Write-JsonFile $StateFile $state
    return $sel
}

# Single-mic mode: the room mic. Saved in its own file so switching modes never wipes the
# lav receivers in last_devices.json. Falls back to the room mic saved there.
function Get-SingleSelection {
    param([array]$Devices)
    if ($Reselect -and (Test-Path $SingleStateFile)) { Remove-Item $SingleStateFile -Force }
    if (-not $Reselect) {
        $id = Get-SavedId (Read-JsonFile $SingleStateFile)
        if ($id) {
            $match = $Devices | Where-Object { $_.ID -eq $id } | Select-Object -First 1
            if ($match) { return @{ room = $match } }
            Write-Warn2 "The saved single mic is not connected. Removing last_single_device.json and choosing again."
            Remove-Item $SingleStateFile -Force
        }
        else {
            $saved = Read-JsonFile $StateFile
            if ($saved -and ($saved.PSObject.Properties.Name -contains "room")) {
                $rid = Get-SavedId $saved.room
                $match = if ($rid) { $Devices | Where-Object { $_.ID -eq $rid } | Select-Object -First 1 } else { $null }
                if ($match) { return @{ room = $match } }
            }
        }
    }
    if ($Devices.Count -eq 1) {
        $d = $Devices[0]
        Write-Info "Using the only microphone found: $($d.Name)"
    }
    else { $d = Read-DeviceChoice "the single mic (room mic)" $Devices $false @() }
    Write-JsonFile $SingleStateFile ([ordered]@{ id = $d.ID; name = $d.Name })
    return @{ room = $d }
}

# ================================================================ mic check

function Test-Mics {
    param($Selection)
    $secs = [int]$Config.micCheckSeconds
    Write-Host ""
    Write-Host "Mic check ($secs s per device): have each player talk now." -ForegroundColor Cyan
    for ($r = 1; $r -le $ReceiverCount; $r++) {
        $dev = $Selection["rx$r"]
        $players = @($Layout | Where-Object { $_.Receiver -eq $r })
        $graph = Get-SplitGraph $players -WithVolumeDetect
        $ffArgs = @("-hide_banner", "-nostdin", "-nostats", "-t", "$secs", "-f", "dshow", "-i", "audio=$($dev.ID)",
                  "-filter_complex", $graph)
        foreach ($p in $players) { $ffArgs += @("-map", "[o$($p.Channel)]", "-f", "null", "-") }
        $out = Invoke-Tool $FFMPEG $ffArgs -AllowFailure
        $peaks = @([regex]::Matches($out, 'Parsed_volumedetect_(\d+).*?max_volume:\s+(-?[\d.]+) dB') |
            Sort-Object { [int]$_.Groups[1].Value } | ForEach-Object { [double]::Parse($_.Groups[2].Value, [Globalization.CultureInfo]::InvariantCulture) })
        for ($i = 0; $i -lt $players.Count; $i++) {
            $name = $players[$i].Name
            if ($i -ge $peaks.Count) { Write-Warn2 "${name}: no level reading (is the receiver sending audio?)"; continue }
            $pk = $peaks[$i]
            if ($pk -lt -50) { Write-Warn2 ("{0}: very low signal (peak {1} dB). Check the transmitter." -f $name, $pk) }
            else { Write-Ok ("{0}: peak {1} dB" -f $name, $pk) }
        }
    }
    if ($Selection.ContainsKey("room") -and $Selection["room"]) {
        $out = Invoke-Tool $FFMPEG @("-hide_banner", "-nostdin", "-nostats", "-t", "$secs", "-f", "dshow", "-i", "audio=$($Selection['room'].ID)",
                                     "-af", "volumedetect", "-f", "null", "-") -AllowFailure
        if ($out -match 'max_volume:\s+(-?[\d.]+) dB') { Write-Ok "Room mic: peak $($Matches[1]) dB" }
        else { Write-Warn2 "Room mic: no level reading" }
    }
}

# ================================================================ recording processes

function Get-SplitGraph {
    param([array]$Players, [switch]$WithVolumeDetect)
    $tail = if ($WithVolumeDetect) { ",volumedetect" } else { "" }
    if ($Players.Count -eq 1) {
        $c = $Players[0].Channel
        return "[0:a]pan=mono|c0=c$c$tail[o$c]"
    }
    $splits = ($Players | ForEach-Object { "[s$($_.Channel)]" }) -join ""
    $g = "[0:a]asplit=$($Players.Count)$splits"
    foreach ($p in $Players) { $g += ";[s$($p.Channel)]pan=mono|c0=c$($p.Channel)$tail[o$($p.Channel)]" }
    return $g
}

function Start-Recorder {
    param([string]$Role, [string]$DeviceId, [array]$Outputs, [string]$PartDir)
    # $Outputs: objects with Channel and Prefix
    # Console output is kept to errors; the full log goes to ffmpeg_<role>.log via FFREPORT.
    $ffArgs = @("-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-f", "dshow", "-i", "audio=$DeviceId")
    $players = @($Outputs | ForEach-Object { [pscustomobject]@{ Channel = $_.Channel } })
    $ffArgs += @("-filter_complex", (Get-SplitGraph $players))
    foreach ($o in $Outputs) {
        $ffArgs += @("-map", "[o$($o.Channel)]", "-ar", "$($Config.sampleRate)", "-sample_fmt", "s16", "-c:a", "flac",
                   "-f", "segment", "-segment_time", "$($Config.segmentSeconds)", "-segment_format", "flac",
                   "-segment_format_options", "flush_packets=1",
                   "-reset_timestamps", "1", "$($o.Prefix)_%05d.flac")
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FFMPEG
    $psi.Arguments = ($ffArgs | ForEach-Object { ConvertTo-ArgString $_ }) -join " "
    $psi.WorkingDirectory = $PartDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true          # own hidden console: immune to Ctrl+C / closing this window
    $psi.EnvironmentVariables["FFREPORT"] = "file=ffmpeg_$Role.log:level=32"
    $p = [System.Diagnostics.Process]::Start($psi)
    return [pscustomobject]@{
        role      = $Role
        pid       = $p.Id
        startTicks = [int64]$p.StartTime.ToUniversalTime().Ticks
        prefixes  = @($Outputs | ForEach-Object { $_.Prefix })
    }
}

function Get-LiveProcess {
    param($Entry)
    $p = Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    if ($p.ProcessName -notlike "ffmpeg*") { return $null }
    try {
        # Guard against Windows reusing the PID for a different ffmpeg
        $diff = [Math]::Abs($p.StartTime.ToUniversalTime().Ticks - [int64]$Entry.startTicks)
        if ($diff -gt 50000000) { return $null }   # > 5 s
    }
    catch { }
    return $p
}

function Stop-Recorders {
    param([array]$Procs)
    foreach ($e in $Procs) {
        $p = Get-LiveProcess $e
        if ($p) {
            try { $p.Kill() } catch { }
        }
    }
    foreach ($e in $Procs) {
        $p = Get-LiveProcess $e
        if ($p) { try { [void]$p.WaitForExit(5000) } catch { } }
    }
}

# ================================================================ session / manifest

function New-SessionManifest {
    param($Selection)
    $timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
    $sessionDir = Join-Path $Config.recordingsRoot "session_$timestamp"
    New-Item -ItemType Directory -Path $sessionDir -Force | Out-Null
    $receivers = @()
    for ($r = 1; $r -le $ReceiverCount; $r++) {
        $d = $Selection["rx$r"]
        $receivers += [ordered]@{
            role = "rx$r"; label = "Receiver $r"; deviceName = $d.Name; deviceId = $d.ID
            players = @($Layout | Where-Object { $_.Receiver -eq $r } | ForEach-Object { [ordered]@{ name = $_.Name; channel = $_.Channel } })
        }
    }
    $room = $null
    if ($Selection.ContainsKey("room") -and $Selection["room"]) {
        $room = [ordered]@{ deviceName = $Selection["room"].Name; deviceId = $Selection["room"].ID }
    }
    $players = @($Layout | ForEach-Object {
        $d = $Selection["rx$($_.Receiver)"]
        [ordered]@{ name = $_.Name; receiver = "Receiver $($_.Receiver)"; channel = $_.Channel; deviceName = $d.Name; deviceId = $d.ID }
    })
    $m = [ordered]@{
        version          = 3
        sessionTimestamp = $timestamp
        sessionDate      = $timestamp.Substring(0, 10)
        sessionDir       = $sessionDir
        status           = "recording"
        format           = "flac"
        mode             = $Mode
        sampleRate       = [int]$Config.sampleRate
        segmentSeconds   = [int]$Config.segmentSeconds
        playerCount      = $(if ($Mode -eq "single") { 0 } else { [int]$Config.playerCount })
        receivers        = $receivers
        roomMic          = $room
        players          = $players
        parts            = @()
    }
    return [pscustomobject]$m
}

function Save-Manifest { param($Manifest) Write-JsonFile (Join-Path $Manifest.sessionDir "session_manifest.json") $Manifest 10 }

function Save-Latest {
    param($Manifest)
    Write-JsonFile $LatestSessionFile ([ordered]@{
        sessionTimestamp = $Manifest.sessionTimestamp
        sessionDir       = $Manifest.sessionDir
        manifestPath     = (Join-Path $Manifest.sessionDir "session_manifest.json")
    })
}

function Start-Part {
    param($Manifest, [string]$Reason)
    $index = @($Manifest.parts).Count + 1
    $partName = "part{0:D2}" -f $index
    $partDir = Join-Path (Join-Path $Manifest.sessionDir "raw") $partName
    New-Item -ItemType Directory -Path $partDir -Force | Out-Null

    $procs = @()
    try {
        foreach ($rx in $Manifest.receivers) {
            $outs = @($rx.players | ForEach-Object { [pscustomobject]@{ Channel = [int]$_.channel; Prefix = $_.name } })
            $procs += Start-Recorder $rx.role $rx.deviceId $outs $partDir
        }
        if ($Manifest.roomMic) {
            $procs += Start-Recorder "room" $Manifest.roomMic.deviceId @([pscustomobject]@{ Channel = 0; Prefix = "room" }) $partDir
        }
    }
    catch {
        Stop-Recorders $procs
        throw
    }
    $now = Get-Date
    $part = [ordered]@{
        index          = $index
        dir            = "raw\$partName"
        reason         = $Reason
        startedAt      = $now.ToString("o")
        startedAtUnixMs = [int64](($now.ToUniversalTime() - [DateTime]'1970-01-01').TotalMilliseconds)
        endedAt        = $null
        endReason      = $null
    }
    $Manifest.parts = @($Manifest.parts) + @([pscustomobject]$part)
    Save-Manifest $Manifest
    Write-JsonFile $ActiveFile ([ordered]@{
        sessionDir = $Manifest.sessionDir
        monitorPid = $PID
        part       = $index
        partDir    = $partDir
        startedAt  = $part.startedAt
        processes  = $procs
    })
    return [pscustomobject]@{ Index = $index; Dir = $partDir; Procs = $procs }
}

function Stop-Part {
    param($Manifest, $Part, [string]$EndReason)
    Stop-Recorders $Part.Procs
    $last = @($Manifest.parts)[-1]
    if ($last -and -not $last.endedAt) {
        $last.endedAt = (Get-Date).ToString("o")
        $last.endReason = $EndReason
    }
    Save-Manifest $Manifest
}

function Invoke-Beep { try { [Console]::Beep(880, 250); [Console]::Beep(660, 250) } catch { } }

# ================================================================ monitor loop

# File size of a segment ffmpeg is still writing. Opening the file returns the real length; the
# directory listing on NTFS can lag behind for files that are open for writing.
function Get-LiveLength {
    param([string]$Path)
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        try { return $fs.Length } finally { $fs.Close() }
    }
    catch { return (Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue).Length }
}

function Get-StreamHealth {
    param($Part, [hashtable]$Seen)
    # Returns @{ ok = bool; detail = string; segments = int }
    $problems = @()
    $segments = 0
    $now = Get-Date
    foreach ($e in $Part.Procs) {
        if (-not (Get-LiveProcess $e)) { $problems += "$($e.role) stopped"; continue }
        foreach ($prefix in $e.prefixes) {
            $files = @(Get-ChildItem -Path $Part.Dir -Filter "${prefix}_*.flac" -ErrorAction SilentlyContinue | Sort-Object Name)
            $segments = [Math]::Max($segments, $files.Count)
            $key = "$prefix"
            $size = if ($files.Count) { $files[-1].Name + ":" + (Get-LiveLength $files[-1].FullName) } else { "none" }
            if (-not $Seen.ContainsKey($key) -or $Seen[$key].size -ne $size) {
                $Seen[$key] = @{ size = $size; changed = $now }
            }
            elseif (($now - $Seen[$key].changed).TotalSeconds -gt [double]$Config.stallSeconds) {
                $problems += "$prefix stalled"
            }
        }
    }
    return @{ ok = ($problems.Count -eq 0); detail = ($problems -join ", "); segments = $segments }
}

function Read-StopKey {
    # Returns $true when the user confirmed stopping (or stop_recording.flag exists).
    if (Test-Path $StopFlag) {
        Remove-Item $StopFlag -Force -ErrorAction SilentlyContinue
        Write-Host ""
        Write-Info "stop_recording.flag found: stopping."
        return $true
    }
    try {
        if (-not [Console]::KeyAvailable) { return $false }
        $k = [Console]::ReadKey($true)
    }
    catch { return $false }
    $isCtrlC = ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control))
    if ($k.Key -ne [ConsoleKey]::Q -and -not $isCtrlC) { return $false }
    Write-Host ""
    Write-Host "Stop recording? [y/N] " -ForegroundColor Yellow -NoNewline
    $ans = [Console]::ReadKey($true)
    Write-Host $ans.KeyChar
    return ($ans.Key -eq [ConsoleKey]::Y)
}

function Watch-Recording {
    param($Manifest, $Part)
    $sessionStart = ([DateTime]'1970-01-01').AddMilliseconds([double]@($Manifest.parts)[0].startedAtUnixMs).ToLocalTime()
    $seen = @{}
    $prevCtrlC = $null
    try { $prevCtrlC = [Console]::TreatControlCAsInput; [Console]::TreatControlCAsInput = $true } catch { }
    Write-Host ""
    Write-Host "Recording. Press Q (or Ctrl+C) to stop. Closing this window does NOT stop the recording." -ForegroundColor Yellow
    try {
        while ($true) {
            Start-Sleep -Milliseconds 500
            if (Read-StopKey) { break }
            $h = Get-StreamHealth $Part $seen
            $elapsed = (Get-Date) - $sessionStart
            $line = "  {0:hh\:mm\:ss}  part {1}  segment {2}  {3}" -f $elapsed, $Part.Index, $h.segments, $(if ($h.ok) { "all streams OK" } else { "PROBLEM: $($h.detail)" })
            try { [Console]::Write("`r" + $line.PadRight([Math]::Max(20, [Console]::WindowWidth - 1))) } catch { Write-Host $line }
            if (-not $h.ok) {
                Write-Host ""
                Invoke-Beep
                Write-Err2 "Recording problem: $($h.detail). Restarting all streams as a new part..."
                Stop-Part $Manifest $Part "stream problem: $($h.detail)"
                $Part = $null
                while (-not $Part) {
                    try {
                        $Part = Start-Part $Manifest "restarted after: $($h.detail)"
                        Start-Sleep -Seconds 3
                        $chk = Get-StreamHealth $Part @{}
                        if (-not $chk.ok) { throw "streams did not start: $($chk.detail)" }
                        $seen = @{}
                        Invoke-Beep
                        Write-Ok ("Recording again (part $($Part.Index))." + (Get-ClapHint $Manifest "CLAP ONCE NOW so this part can be re-synced."))
                    }
                    catch {
                        if ($Part) { Stop-Part $Manifest $Part "failed to start: $($_.Exception.Message)"; $Part = $null }
                        Write-Err2 "Could not restart: $($_.Exception.Message). Retrying in 10 s (press Q to give up)."
                        for ($i = 0; $i -lt 20; $i++) {
                            Start-Sleep -Milliseconds 500
                            if (Read-StopKey) { return $null }
                        }
                    }
                }
            }
        }
    }
    finally {
        if ($null -ne $prevCtrlC) { try { [Console]::TreatControlCAsInput = $prevCtrlC } catch { } }
        Write-Host ""
    }
    return $Part
}

function Complete-Session {
    param($Manifest, $Part)
    if ($Part) { Stop-Part $Manifest $Part "stopped by user" }
    $Manifest.status = "complete"
    Save-Manifest $Manifest
    Save-Latest $Manifest
    if (Test-Path $ActiveFile) { Remove-Item $ActiveFile -Force }
    Write-Ok "Recording stopped. Session: $($Manifest.sessionDir)"
    Write-Host ""
    $ans = Read-Host "Sync and upload this session now? [Y/n]"
    if ($ans -notmatch '^[nN]') {
        & (Join-Path $PSScriptRoot "Sync-TTRPGLavs.ps1") -SessionDir $Manifest.sessionDir -Submit
    }
    else {
        Write-Host "Later: .\Sync-TTRPGLavs.ps1 -SessionDir `"$($Manifest.sessionDir)`" -Submit"
    }
}

# ================================================================ main

# 1) An earlier recording that is still running, or was interrupted?
$active = Read-JsonFile $ActiveFile
if ($active -and ($active.PSObject.Properties.Name -contains "monitorPid") -and [int]$active.monitorPid -ne $PID) {
    $other = Get-Process -Id ([int]$active.monitorPid) -ErrorAction SilentlyContinue
    if ($other -and $other.ProcessName -match '^(pwsh|powershell)') {
        Write-Warn2 "Another recorder window (PowerShell PID $($other.Id)) is already watching this recording."
        Write-Host "   Use that window to stop it, or close that window and run record.ps1 again."
        return
    }
}
if ($active) {
    $manifest = Read-JsonFile (Join-Path $active.sessionDir "session_manifest.json")
    if (-not $manifest) {
        Write-Warn2 "active_recording.json points to a session without a manifest; ignoring it."
        Remove-Item $ActiveFile -Force
    }
    else {
        $procs = @($active.processes)
        $alive = @($procs | Where-Object { Get-LiveProcess $_ })
        $part = [pscustomobject]@{ Index = [int]$active.part; Dir = $active.partDir; Procs = $procs }
        if ($alive.Count -gt 0) {
            Write-Ok "A recording is still running: $($active.sessionDir) (part $($active.part), started $($active.startedAt))."
            Write-Host "Re-attaching. You can stop it from here."
            $active.monitorPid = $PID
            Write-JsonFile $ActiveFile $active
            $part = Watch-Recording $manifest $part
            Complete-Session $manifest $part
            return
        }
        Write-Warn2 "The recording in $($active.sessionDir) was interrupted (part $($active.part) started $($active.startedAt))."
        $choice = Read-Host "[R]esume it as a new part, [F]inish it as is, or start a [N]ew session? (R/F/N)"
        switch -Regex ($choice) {
            '^[rR]' {
                $last = @($manifest.parts)[-1]
                if ($last -and -not $last.endedAt) { $last.endedAt = (Get-Date).ToString("o"); $last.endReason = "interrupted" }
                $part = Start-Part $manifest "resumed after interruption"
                Invoke-Beep
                Write-Ok ("Recording resumed (part $($part.Index))." + (Get-ClapHint $manifest "CLAP ONCE NOW so this part can be re-synced."))
                $part = Watch-Recording $manifest $part
                Complete-Session $manifest $part
                return
            }
            '^[fF]' {
                $last = @($manifest.parts)[-1]
                if ($last -and -not $last.endedAt) { $last.endedAt = (Get-Date).ToString("o"); $last.endReason = "interrupted" }
                Complete-Session $manifest $null
                return
            }
            default {
                $last = @($manifest.parts)[-1]
                if ($last -and -not $last.endedAt) { $last.endedAt = (Get-Date).ToString("o"); $last.endReason = "interrupted" }
                $manifest.status = "complete"
                Save-Manifest $manifest
                Remove-Item $ActiveFile -Force
                Write-Info "Marked the interrupted session complete. Starting a new one."
            }
        }
    }
}

# 2) Devices
Write-Host ""
if ($Mode -eq "single") { Write-Info "Single-mic mode: recording the room mic only (no lavs, no clap needed)." }
Write-Info "Enumerating audio devices..."
$devices = @(Get-AudioDevices)
if ($devices.Count -eq 0) { throw "No microphones found. Plug in the receivers or a microphone and try again." }
# Only one microphone (or too few for the lav receivers): offer single-mic mode instead of failing.
if ($Mode -ne "single" -and ($devices.Count -eq 1 -or $devices.Count -lt $ReceiverCount)) {
    $short = $devices.Count -lt $ReceiverCount
    Write-Host ""
    if ($short) { Write-Warn2 "Found $($devices.Count) microphone(s), but $ReceiverCount lav receiver(s) are needed for $($Config.playerCount) lavs." }
    else { Write-Warn2 "Only one microphone found: $($devices[0].Name)" }
    Write-Host "   Single-mic mode records just one microphone (room mic only, no lavs, no clap). Players can't be told apart by lav,"
    Write-Host "   but the server can guess speakers if diarization is set up."
    $prompt = if ($short) { "Record with a single mic instead? [Y/n]" } else { "Record with a single mic (room mic only) instead of lav mode? [y/N]" }
    $ans = Read-Host $prompt
    $useSingle = if ($short) { $ans -notmatch '^[nN]' } else { $ans -match '^[yY]' }
    if ($useSingle) {
        $Mode = "single"; $Layout = @(); $ReceiverCount = 0
        Write-Info "Single-mic mode for this session."
    }
    elseif ($short) {
        throw "Found $($devices.Count) audio device(s) but the config needs $ReceiverCount receiver(s). Plug the receivers in and try again."
    }
}
if ($Mode -eq "single") {
    $selection = Get-SingleSelection $devices
    Write-Ok "Single mic: $($selection['room'].Name)   (run with -Single -Reselect to choose another)"
}
else {
    $selection = Get-SavedSelection $devices
    if ($selection) {
        Write-Ok "Using saved devices:"
        foreach ($role in (Get-ExpectedRoles)) {
            $d = $selection[$role]
            Write-Host ("   {0,-5} {1}" -f $role, $(if ($d) { $d.Name } else { "(none)" }))
        }
        Write-Host "   (run with -Reselect to choose different devices)"
    }
    else {
        $selection = Select-AllDevices $devices
    }
}
if ($Mode -ne "single" -and $Config.roomMic -eq "required" -and -not $selection["room"]) { throw "config.json requires a room mic." }

# 3) Mic check
if (-not $SkipMicCheck) { Test-Mics $selection }

# 4) Record
$manifest = New-SessionManifest $selection
Save-Manifest $manifest
Save-Latest $manifest
Write-Host ""
Write-Info "Session folder: $($manifest.sessionDir)"
$part = Start-Part $manifest "start"
Start-Sleep -Seconds 2
$h = Get-StreamHealth $part @{}
if (-not $h.ok) {
    Stop-Part $manifest $part "failed to start"
    Remove-Item $ActiveFile -Force -ErrorAction SilentlyContinue
    throw "Recording did not start: $($h.detail). See the ffmpeg_*.log files in $($part.Dir)."
}
Invoke-Beep
if ($Mode -eq "single") { Write-Ok "Recording started." }
else { Write-Ok "Recording started. CLAP ONCE, LOUDLY, within the next $($Config.sync.windowSeconds) seconds." }
$part = Watch-Recording $manifest $part
Complete-Session $manifest $part
