<#
.SYNOPSIS
    Serves mic-test.html at http://localhost:8765/ so the browser treats it as a real site.

.DESCRIPTION
    Chrome won't give microphone IDs to a page opened straight from a file, so the test can only
    see one mic that way. Over http://localhost it can open every mic. Nothing leaves this computer.
    Press Ctrl+C (or close the window) to stop.
#>
param([int]$Port = 8765)

$ErrorActionPreference = "Stop"
$page = Join-Path $PSScriptRoot "mic-test.html"
if (-not (Test-Path $page)) { throw "mic-test.html not found next to this script." }

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://localhost:$Port/")
$listener.Start()
$url = "http://localhost:$Port/"
Write-Host "Mic test running at $url  (Ctrl+C to stop)"
try { Start-Process $url } catch { Write-Host "Open $url in Chrome or Edge." }

try {
    while ($listener.IsListening) {
        $ctx = $listener.GetContext()
        $path = $ctx.Request.Url.AbsolutePath
        if ($path -eq "/" -or $path -eq "/mic-test.html") {
            $bytes = [IO.File]::ReadAllBytes($page)
            $ctx.Response.ContentType = "text/html; charset=utf-8"
            $ctx.Response.StatusCode = 200
        }
        else {
            $bytes = [Text.Encoding]::UTF8.GetBytes("Not found")
            $ctx.Response.StatusCode = 404
        }
        $ctx.Response.ContentLength64 = $bytes.Length
        $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
        $ctx.Response.Close()
    }
}
finally {
    $listener.Stop()
    $listener.Close()
}
