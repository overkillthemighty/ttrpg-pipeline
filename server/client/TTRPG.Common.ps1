# Shared helpers for record.ps1, Sync-TTRPGLavs.ps1 and Submit-Session.ps1.
# Dot-source it:  . (Join-Path $PSScriptRoot "TTRPG.Common.ps1")
# Works in Windows PowerShell 5.1 and PowerShell 7.

Set-StrictMode -Version Latest

$script:TtrpgRoot = $PSScriptRoot

function Write-Info  { param([string]$Message) Write-Host "[INFO] $Message" }
function Write-Ok    { param([string]$Message) Write-Host "[ OK ] $Message" -ForegroundColor Green }
function Write-Warn2 { param([string]$Message) Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Write-Err2  { param([string]$Message) Write-Host "[FAIL] $Message" -ForegroundColor Red }

# ---------------------------------------------------------------- config

$script:DefaultConfig = [ordered]@{
    recordingsRoot      = ""        # blank = the folder these scripts are in
    mode                = "lavs"    # lavs | single (room mic only; record.ps1 -Single / -Lavs override it)
    playerCount         = 4
    channelsPerReceiver = 2
    roomMic             = "optional" # optional | required | none
    sampleRate          = 48000
    segmentSeconds      = 60
    micCheckSeconds     = 5
    stallSeconds        = 20
    sync                = [ordered]@{ windowSeconds = 10; searchStartSeconds = 0 }
    server              = [ordered]@{ url = ""; apiKey = ""; chunkMB = 16; timeoutSeconds = 300 }
    processing          = [ordered]@{ whisperModel = ""; language = "en"; publish = $true; enrollment = @()
                                      diarize = $false; peopleAtTable = 4 }   # peopleAtTable: players + DM (usually 3 + 1), 0 = ask at upload
}

function Merge-Defaults {
    param($Defaults, $Actual)
    $out = [ordered]@{}
    foreach ($k in $Defaults.Keys) {
        $has = $null -ne $Actual -and ($Actual.PSObject.Properties.Name -contains $k)
        $v = if ($has) { $Actual.$k } else { $null }
        if ($Defaults[$k] -is [System.Collections.IDictionary]) {
            $out[$k] = Merge-Defaults $Defaults[$k] $v
        }
        elseif ($has -and $null -ne $v) { $out[$k] = $v }
        else { $out[$k] = $Defaults[$k] }
    }
    return [pscustomobject]$out
}

function Get-TtrpgConfig {
    $path = Join-Path $script:TtrpgRoot "config.json"
    if (-not (Test-Path $path)) {
        $script:DefaultConfig | ConvertTo-Json -Depth 6 | Set-Content -Path $path -Encoding UTF8
        Write-Warn2 "Created default config.json beside the scripts. Edit it to set the server URL and API key."
    }
    try {
        $raw = Get-Content -Path $path -Raw | ConvertFrom-Json
    }
    catch {
        throw "config.json is not valid JSON: $($_.Exception.Message)"
    }
    $cfg = Merge-Defaults $script:DefaultConfig $raw
    # Nested objects need converting too
    $cfg.sync = [pscustomobject]$cfg.sync
    $cfg.server = [pscustomobject]$cfg.server
    $cfg.processing = [pscustomobject]$cfg.processing

    $cfg.playerCount = [int]$cfg.playerCount
    $cfg.channelsPerReceiver = [int]$cfg.channelsPerReceiver
    if ($cfg.playerCount -lt 1 -or $cfg.playerCount -gt 16) { throw "config.json: playerCount must be 1-16" }
    if ($cfg.channelsPerReceiver -lt 1 -or $cfg.channelsPerReceiver -gt 2) { throw "config.json: channelsPerReceiver must be 1 or 2" }
    if (@("optional", "required", "none") -notcontains [string]$cfg.roomMic) { throw "config.json: roomMic must be optional, required or none" }
    if (@("lavs", "single") -notcontains [string]$cfg.mode) { throw "config.json: mode must be lavs or single" }
    if ([string]::IsNullOrWhiteSpace([string]$cfg.recordingsRoot)) { $cfg.recordingsRoot = $script:TtrpgRoot }
    return $cfg
}

function Get-ReceiverCount {
    param($Config)
    return [int][Math]::Ceiling($Config.playerCount / $Config.channelsPerReceiver)
}

# Players laid out across receivers: Player1 = Rx1 ch0, Player2 = Rx1 ch1, Player3 = Rx2 ch0, ...
function Get-PlayerLayout {
    param($Config)
    $layout = @()
    for ($i = 0; $i -lt $Config.playerCount; $i++) {
        $layout += [pscustomobject]@{
            Name     = "Player$($i + 1)"
            Receiver = [int][Math]::Floor($i / $Config.channelsPerReceiver) + 1
            Channel  = $i % $Config.channelsPerReceiver
        }
    }
    return $layout
}

# ---------------------------------------------------------------- tools

function Get-FFmpegPath {
    param([string]$Name = "ffmpeg")
    $parent = Split-Path $script:TtrpgRoot -Parent
    $candidates = @((Join-Path $script:TtrpgRoot "$Name.exe"), (Join-Path $script:TtrpgRoot $Name))
    if ($parent) { $candidates += (Join-Path $parent "$Name.exe") }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate -PathType Leaf) { return $candidate }
    }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "$Name not found. Put $Name.exe beside the scripts or add it to PATH."
}

# Run a program, capture output, throw on failure. Returns combined stdout+stderr text.
function Invoke-Tool {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$AllowFailure
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ($Arguments | ForEach-Object { ConvertTo-ArgString $_ }) -join " "
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $p.StandardError.ReadToEndAsync()
    $out = $p.StandardOutput.ReadToEnd()
    $p.WaitForExit()
    $text = $out + $errTask.Result
    if ($p.ExitCode -ne 0 -and -not $AllowFailure) {
        $tail = ($text -split "`r?`n" | Select-Object -Last 15) -join "`n"
        throw "$([IO.Path]::GetFileName($FilePath)) failed (exit $($p.ExitCode)):`n$tail"
    }
    return $text
}

function ConvertTo-ArgString {
    param([string]$Arg)
    if ($null -eq $Arg -or $Arg -eq "") { return '""' }
    if ($Arg -notmatch '[\s"]') { return $Arg }
    # Windows argv quoting: double any backslashes that precede a quote or the end.
    $escaped = [regex]::Replace($Arg, '(\\*)"', { param($m) ($m.Groups[1].Value * 2) + '\"' })
    $escaped = [regex]::Replace($escaped, '(\\+)$', { param($m) $m.Groups[1].Value * 2 })
    return '"' + $escaped + '"'
}

function Get-MediaDuration {
    param([Parameter(Mandatory)][string]$Path)
    $ffprobe = Get-FFmpegPath "ffprobe"
    $out = Invoke-Tool $ffprobe @("-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", $Path)
    $line = ($out -split "`r?`n" | Where-Object { $_ -match '^[0-9.]+$' } | Select-Object -First 1)
    if (-not $line) { throw "Could not read duration of $Path" }
    return [double]::Parse($line, [Globalization.CultureInfo]::InvariantCulture)
}

function Format-Inv {
    param([double]$Value, [string]$Format = "0.000000")
    return $Value.ToString($Format, [Globalization.CultureInfo]::InvariantCulture)
}

# ---------------------------------------------------------------- JSON files

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    try { return Get-Content -Path $Path -Raw | ConvertFrom-Json }
    catch { Write-Warn2 "Ignoring unreadable JSON file: $Path"; return $null }
}

# Write atomically (temp file + rename) so a crash never leaves a half-written file.
function Write-JsonFile {
    param([string]$Path, $Object, [int]$Depth = 8)
    $tmp = "$Path.tmp"
    $Object | ConvertTo-Json -Depth $Depth | Set-Content -Path $tmp -Encoding UTF8
    Move-Item -Path $tmp -Destination $Path -Force
}

# Resolve a path recorded in a manifest; falls back to the same relative location under
# the session folder if the session was moved/copied elsewhere.
function Resolve-SessionPath {
    param([string]$Recorded, [string]$SessionDir, [string]$RecordedSessionDir)
    if ([string]::IsNullOrWhiteSpace($Recorded)) { return $null }
    if (-not [IO.Path]::IsPathRooted($Recorded) -and $Recorded -notmatch '^[A-Za-z]:') {
        return Join-Path $SessionDir ($Recorded -replace '[\\/]', [IO.Path]::DirectorySeparatorChar)
    }
    if (Test-Path $Recorded) { return $Recorded }
    if ($RecordedSessionDir -and $Recorded.StartsWith($RecordedSessionDir, [StringComparison]::OrdinalIgnoreCase)) {
        $rel = $Recorded.Substring($RecordedSessionDir.Length).TrimStart('\', '/')
        $rel = $rel -replace '[\\/]', [IO.Path]::DirectorySeparatorChar
        return Join-Path $SessionDir $rel
    }
    return $Recorded
}
