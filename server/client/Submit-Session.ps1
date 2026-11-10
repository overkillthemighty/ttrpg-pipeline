<#
.SYNOPSIS
    Uploads a synced session to the transcription server and (optionally) follows its progress.

.DESCRIPTION
    Uploads 00_synced\synced_*.flac in chunks. If the connection drops, run the same command
    again: it picks up where it stopped (upload state is kept in <session>\upload_state.json).
    The local recordings are never deleted, so a session can be submitted again any time
    with -Resubmit.

.PARAMETER SessionDir
    Session folder. Default: the most recently synced session (latest_sync.json).

.PARAMETER Resubmit
    Start a new server job even if this session was submitted before.

.PARAMETER NoWait
    Don't follow the job's progress after uploading.

.PARAMETER Diarize
    Room-mic-only sessions: have the server guess speakers (Speaker 1, 2, ...). Overrides
    processing.diarize in config.json. -NoDiarize turns it off.

.PARAMETER People
    Room-mic-only sessions with diarization: how many people talk at the table, players
    PLUS the DM. Overrides processing.peopleAtTable. If neither is set you're asked.

.EXAMPLE
    .\Submit-Session.ps1
.EXAMPLE
    .\Submit-Session.ps1 -SessionDir "C:\Intel\Recorder\session_2026-10-15_12-06-27" -Resubmit -WhisperModel medium
#>
[CmdletBinding()]
param(
    [string]$SessionDir = "",
    [switch]$Resubmit,
    [switch]$NoWait,
    [string]$Title = "",
    [string]$WhisperModel = "",
    [string]$Language = "",
    [string[]]$Enrollment = @(),
    [switch]$NoPublish,
    [switch]$Diarize,
    [switch]$NoDiarize,
    [int]$People = 0
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "TTRPG.Common.ps1")
$Config = Get-TtrpgConfig

$ServerUrl = ([string]$Config.server.url).TrimEnd("/")
$ApiKey = [string]$Config.server.apiKey
if (-not $ServerUrl -or -not $ApiKey) {
    throw "Set server.url and server.apiKey in config.json (create a key in the dashboard under API keys)."
}
$ChunkBytes = [int]([double]$Config.server.chunkMB * 1MB)
$TimeoutMs = [int]$Config.server.timeoutSeconds * 1000

try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

# ---------------------------------------------------------------- HTTP

function Invoke-Api {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        $Json = $null,
        [byte[]]$Bytes = $null,
        [int]$Count = -1,
        [hashtable]$Headers = @{}
    )
    # Returns @{ Status = int; Body = object-or-string }. Network failures throw.
    $req = [System.Net.HttpWebRequest]::Create("$ServerUrl$Path")
    $req.Method = $Method
    $req.Timeout = $TimeoutMs
    $req.ReadWriteTimeout = $TimeoutMs
    $req.Headers["Authorization"] = "Bearer $ApiKey"
    $req.Accept = "application/json"
    $req.UserAgent = "TTRPG-Submit/3"
    foreach ($k in $Headers.Keys) { $req.Headers[$k] = [string]$Headers[$k] }
    $payload = $null
    if ($null -ne $Json) {
        $payload = [Text.Encoding]::UTF8.GetBytes(($Json | ConvertTo-Json -Depth 8 -Compress))
        $req.ContentType = "application/json"
        $Count = $payload.Length
    }
    elseif ($null -ne $Bytes) {
        $payload = $Bytes
        $req.ContentType = "application/octet-stream"
        if ($Count -lt 0) { $Count = $Bytes.Length }
    }
    if ($null -ne $payload) {
        $req.AllowWriteStreamBuffering = $false
        $req.ContentLength = $Count
        $s = $req.GetRequestStream()
        try { $s.Write($payload, 0, $Count) } finally { $s.Close() }
    }
    elseif ($Method -ne "GET") { $req.ContentLength = 0 }

    $resp = $null
    try { $resp = $req.GetResponse() }
    catch [System.Net.WebException] {
        if ($_.Exception.Response) { $resp = $_.Exception.Response } else { throw }
    }
    try {
        $reader = New-Object IO.StreamReader($resp.GetResponseStream())
        $text = $reader.ReadToEnd()
        $reader.Close()
        $body = $text
        try { if ($text) { $body = $text | ConvertFrom-Json } } catch { }
        return @{ Status = [int]$resp.StatusCode; Body = $body }
    }
    finally { $resp.Close() }
}

function Get-Detail($r) {
    if ($r.Body -and $r.Body.PSObject -and $r.Body.PSObject.Properties.Name -contains "detail") { return [string]$r.Body.detail }
    return [string]$r.Body
}

function Assert-Ok($r, [string]$What) {
    if ($r.Status -ge 200 -and $r.Status -lt 300) { return }
    if ($r.Status -eq 401) { throw "$What failed: the server rejected the API key (401). Check server.apiKey in config.json." }
    throw "$What failed: HTTP $($r.Status) $(Get-Detail $r)"
}

# Retries network errors / 5xx with backoff. 4xx responses are returned to the caller.
function Invoke-ApiRetry {
    param([scriptblock]$Call, [string]$What, [int]$Tries = 12)
    for ($i = 1; ; $i++) {
        try {
            $r = & $Call
            if ($r.Status -lt 500 -and $r.Status -ne 429) { return $r }
            $msg = "HTTP $($r.Status)"
        }
        catch { $msg = $_.Exception.Message }
        if ($i -ge $Tries) { throw "$What failed after $Tries attempts ($msg). Run the same command again to resume." }
        $wait = [Math]::Min(60, [Math]::Pow(2, $i))
        Write-Warn2 "$What`: $msg. Retrying in $wait s ($i/$Tries)..."
        Start-Sleep -Seconds $wait
    }
}

# ---------------------------------------------------------------- script version

# Warn (never fail) if the server has newer laptop scripts than these.
$localVersionFile = Join-Path $PSScriptRoot "client_version.txt"
if (Test-Path $localVersionFile) {
    try {
        $localVersion = (Get-Content -Path $localVersionFile -Raw).Trim()
        $h = Invoke-Api GET "/api/v1/health"
        if ($h.Status -eq 200 -and $h.Body -and ($h.Body.PSObject.Properties.Name -contains "client_version") -and
            $h.Body.client_version -and [string]$h.Body.client_version -ne $localVersion) {
            Write-Warn2 "The server has different laptop scripts (server $($h.Body.client_version), here $localVersion). Download them from $ServerUrl/client"
        }
    }
    catch { }
}

# ---------------------------------------------------------------- session

if ([string]::IsNullOrWhiteSpace($SessionDir)) {
    $latest = Read-JsonFile (Join-Path $PSScriptRoot "latest_sync.json")
    if (-not $latest -or -not $latest.sessionDir) { throw "No -SessionDir given and latest_sync.json not found. Run Sync-TTRPGLavs.ps1 first." }
    $SessionDir = [string]$latest.sessionDir
}
$SessionDir = (Resolve-Path $SessionDir).Path.TrimEnd('\', '/')
$syncDir = Join-Path $SessionDir "00_synced"
$syncManifest = Read-JsonFile (Join-Path $syncDir "sync_manifest.json")
if (-not $syncManifest) { throw "This session isn't synced yet. Run: .\Sync-TTRPGLavs.ps1 -SessionDir `"$SessionDir`" -Submit" }

$files = @()
foreach ($row in @($syncManifest.syncedLav)) {
    $f = Join-Path $syncDir (Split-Path ([string]$row.syncedFile) -Leaf)
    if (-not (Test-Path $f)) { throw "Synced file missing: $f (re-run Sync-TTRPGLavs.ps1 -Force)" }
    $files += [pscustomobject]@{ Name = [string]$row.speaker; Path = $f; Size = (Get-Item $f).Length }
}
if ($files.Count -eq 0) { throw "No synced files listed in sync_manifest.json" }

$IsSingle = ($syncManifest.PSObject.Properties.Name -contains "mode") -and $syncManifest.mode -eq "single"
$folder = Split-Path $SessionDir -Leaf
$sessionDate = if ($syncManifest.PSObject.Properties.Name -contains "sessionDate" -and $syncManifest.sessionDate) { [string]$syncManifest.sessionDate }
               elseif ($folder -match '(\d{4}-\d{2}-\d{2})') { $Matches[1] }
               else { (Get-Item $SessionDir).CreationTime.ToString("yyyy-MM-dd") }
$StatePath = Join-Path $SessionDir "upload_state.json"
# Fingerprint of the synced files: if they changed (e.g. re-synced with -Force) a new job is needed.
$Fingerprint = ($files | ForEach-Object { "{0}:{1}:{2}" -f $_.Name, $_.Size, (Get-Item $_.Path).LastWriteTimeUtc.Ticks }) -join "|"

# ---------------------------------------------------------------- existing job?

$jobId = $null
$state = Read-JsonFile $StatePath
if ($state -and -not $Resubmit -and $state.server -eq $ServerUrl -and
    ($state.PSObject.Properties.Name -contains "fingerprint") -and $state.fingerprint -ne $Fingerprint) {
    Write-Info "The synced files changed since job $($state.jobId) was created; starting a new job."
    $state = $null
}
if ($state -and -not $Resubmit -and $state.server -eq $ServerUrl) {
    $r = Invoke-ApiRetry { Invoke-Api GET "/api/v1/jobs/$($state.jobId)" } "Checking previous upload"
    if ($r.Status -eq 200) {
        $st = [string]$r.Body.status
        switch ($st) {
            "uploading" { $jobId = $state.jobId; Write-Info "Resuming upload for job $jobId" }
            { $_ -in @("queued", "running", "done") } {
                $jobId = $state.jobId
                Write-Ok "This session was already submitted as job $jobId (status: $st)."
                Write-Host "   Use -Resubmit to process it again."
            }
            default {
                Write-Warn2 "Job $($state.jobId) is '$st'. Retry it from the dashboard ($ServerUrl/jobs/$($state.jobId)), or run again with -Resubmit."
                return
            }
        }
    }
    else { Write-Info "Previous job $($state.jobId) not found on the server; creating a new one." }
}

if (-not $jobId) {
    Write-Info "Hashing $($files.Count) files..."
    $decl = @()
    foreach ($f in $files) {
        $decl += [ordered]@{ name = $f.Name; filename = (Split-Path $f.Path -Leaf); size = $f.Size
                             sha256 = (Get-FileHash -Algorithm SHA256 -Path $f.Path).Hash.ToLowerInvariant() }
    }
    $opts = [ordered]@{}
    $model = if ($WhisperModel) { $WhisperModel } else { [string]$Config.processing.whisperModel }
    $lang = if ($Language) { $Language } else { [string]$Config.processing.language }
    $enrSource = if ($Enrollment.Count) { $Enrollment } else { $Config.processing.enrollment }
    $enr = @($enrSource | Where-Object { $_ })
    if ($model) { $opts.whisper_model = $model }
    if ($lang) { $opts.language = $lang }
    if ($enr.Count) { $opts.enrollment = $enr }
    $opts.publish = -not $NoPublish -and [bool]$Config.processing.publish
    if ($IsSingle) {
        $opts.mode = "single"
        $doDiarize = if ($Diarize) { $true } elseif ($NoDiarize) { $false } else { [bool]$Config.processing.diarize }
        if ($doDiarize) {
            $opts.diarize = $true
            $n = if ($People -gt 0) { $People } else { [int]$Config.processing.peopleAtTable }
            if ($n -le 0) {
                try {
                    $ans = Read-Host "How many people talk at the table, counting every player AND the DM? (blank = let the server guess)"
                    if ($ans -match '^\s*(\d+)\s*$') { $n = [int]$Matches[1] }
                }
                catch { }
            }
            if ($n -gt 0) { $opts.min_speakers = $n; $opts.max_speakers = $n; Write-Info "Speaker guessing on: $n people (players + DM)" }
            else { Write-Info "Speaker guessing on: number of people left to the server" }
        }
    }
    $body = [ordered]@{
        session_date = $sessionDate
        title        = $(if ($Title) { $Title } else { "Session $sessionDate" })
        client_ref   = "$env:COMPUTERNAME/$folder"
        synced       = $true
        files        = $decl
        options      = $opts
    }
    $r = Invoke-ApiRetry { Invoke-Api POST "/api/v1/jobs" -Json $body } "Creating job"
    Assert-Ok $r "Creating job"
    $jobId = [string]$r.Body.id
    Write-JsonFile $StatePath ([ordered]@{ server = $ServerUrl; jobId = $jobId; createdAt = (Get-Date).ToString("o"); fingerprint = $Fingerprint })
    Write-Ok "Created job $jobId"
}

# ---------------------------------------------------------------- upload

$job = (Invoke-ApiRetry { Invoke-Api GET "/api/v1/jobs/$jobId" } "Reading job").Body
if ($job.status -eq "uploading") {
    $total = ($files | Measure-Object -Property Size -Sum).Sum
    $doneBefore = 0
    foreach ($f in $files) {
        $path = "/api/v1/jobs/$jobId/files/$([uri]::EscapeDataString($f.Name))"
        $r = Invoke-ApiRetry { Invoke-Api GET $path } "Checking $($f.Name)"
        Assert-Ok $r "Checking $($f.Name)"
        $offset = [int64]$r.Body.received
        if ($offset -gt 0 -and $offset -lt $f.Size) { Write-Info ("{0}: resuming at {1:N1} MB" -f $f.Name, ($offset / 1MB)) }
        $fs = [IO.File]::OpenRead($f.Path)
        $checksumFailures = 0
        try {
            $buf = New-Object byte[] $ChunkBytes
            while ($offset -lt $f.Size) {
                $fs.Position = $offset
                $n = $fs.Read($buf, 0, $ChunkBytes)
                if ($n -le 0) { throw "$($f.Name): local file is shorter than when the job was created. Run again with -Resubmit." }
                $o = $offset
                $r = Invoke-ApiRetry { Invoke-Api PATCH $path -Bytes $buf -Count $n -Headers @{ "Upload-Offset" = "$o" } } "Uploading $($f.Name)"
                if ($r.Status -eq 409 -and $r.Body.PSObject.Properties.Name -contains "received") {
                    $offset = [int64]$r.Body.received   # server has a different offset: follow it
                    Start-Sleep -Seconds 2
                    continue
                }
                if ($r.Status -eq 422) {
                    $checksumFailures++
                    if ($checksumFailures -ge 2) { throw "$($f.Name): checksum keeps failing. The file changed since the job was created; run again with -Resubmit." }
                    Write-Warn2 "$($f.Name): $(Get-Detail $r). Uploading it again."
                    $offset = 0
                    continue
                }
                Assert-Ok $r "Uploading $($f.Name)"
                $offset = [int64]$r.Body.received
                $pct = [Math]::Min(100, (($doneBefore + $offset) / $total) * 100)
                Write-Progress -Activity "Uploading session $folder" -Status ("{0}: {1:N0} / {2:N0} MB" -f $f.Name, ($offset / 1MB), ($f.Size / 1MB)) -PercentComplete $pct
            }
        }
        finally { $fs.Close() }
        $doneBefore += $f.Size
        Write-Ok "Uploaded $($f.Name)"
    }
    Write-Progress -Activity "Uploading session $folder" -Completed
    $r = Invoke-ApiRetry { Invoke-Api POST "/api/v1/jobs/$jobId/submit" } "Submitting"
    Assert-Ok $r "Submitting"
    Write-Ok "Queued for processing: $ServerUrl/jobs/$jobId"
}

if ($NoWait) { return }

# ---------------------------------------------------------------- follow progress

Write-Info "Following progress (Ctrl+C stops watching; the job keeps running on the server)."
$lastMsg = ""
while ($true) {
    try { $r = Invoke-Api GET "/api/v1/jobs/$jobId" } catch { Start-Sleep -Seconds 10; continue }
    if ($r.Status -ne 200) { Start-Sleep -Seconds 10; continue }
    $j = $r.Body
    $pct = [Math]::Max(0, [Math]::Min(100, [double]$j.percent))
    Write-Progress -Activity "Processing $folder (job $jobId)" -Status "$($j.status): $($j.message)" -PercentComplete $pct
    if ($j.message -ne $lastMsg -and $j.stage -ne "transcribe") { Write-Info "$($j.stage): $($j.message)"; $lastMsg = $j.message }
    if ($j.status -in @("done", "failed", "cancelled")) {
        Write-Progress -Activity "Processing $folder (job $jobId)" -Completed
        if ($j.status -eq "done") {
            Write-Ok "Done."
            if ($j.results.PSObject.Properties.Name -contains "nextcloud" -and $j.results.nextcloud) {
                Write-Ok "Results in Nextcloud: $($j.results.nextcloud.folder)"
            }
            else { Write-Info "Download the results from $ServerUrl/jobs/$jobId" }
        }
        elseif ($j.status -eq "failed") { Write-Err2 "Job failed: $($j.error)`n   Details: $ServerUrl/jobs/$jobId" }
        else { Write-Warn2 "Job was cancelled." }
        break
    }
    Start-Sleep -Seconds 5
}
