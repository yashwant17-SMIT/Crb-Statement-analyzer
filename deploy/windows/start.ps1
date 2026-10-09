#Requires -Version 5.1
<#
.SYNOPSIS
    Run the MoMo underwriting service.

.DESCRIPTION
    Serves the API and, when frontend\dist exists, the console on the same port.

.PARAMETER Port
    Listening port. Default 8001.

.PARAMETER Lan
    Bind 0.0.0.0 so other machines on the network can reach it. Without this the
    service binds 127.0.0.1 and is reachable only from this machine.

.PARAMETER Password
    Require a username and password on every request. Set this before exposing
    the service beyond this machine - see share.ps1.

.PARAMETER Username
    Username to pair with -Password. Default "underwriter".

.PARAMETER Reload
    Restart on source changes. Development only.

.EXAMPLE
    .\start.ps1
    .\start.ps1 -Lan -Port 8081
#>
[CmdletBinding()]
param(
    [int]$Port = 8001,
    [switch]$Lan,
    [switch]$Reload,
    [string]$Password = "",
    [string]$Username = "underwriter"
)

$ErrorActionPreference = 'Stop'

$Root    = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Backend = Join-Path $Root 'backend'
$VenvPy  = Join-Path $Backend '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPy)) {
    Write-Host "Virtual environment not found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

$bindHost = if ($Lan) { '0.0.0.0' } else { '127.0.0.1' }
$display  = if ($Lan) { (Get-NetIPAddress -AddressFamily IPv4 |
                Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
                Select-Object -First 1 -ExpandProperty IPAddress) } else { 'localhost' }

# Force UTF-8. Without it the default Windows console codepage (cp1252) cannot
# encode characters the engine emits, such as the sigma in the profile-deviation
# metric, and logging a request can raise UnicodeEncodeError.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# Same-origin when the console is served from dist, so no CORS is needed.
if (-not $env:ALLOWED_ORIGINS) { $env:ALLOWED_ORIGINS = 'http://localhost:' + $Port }

# Password gate. Off unless -Password is supplied.
if ($Password) {
    $env:ACCESS_PASSWORD = $Password
    $env:ACCESS_USERNAME = $Username
} else {
    Remove-Item Env:\ACCESS_PASSWORD -ErrorAction SilentlyContinue
}

$hasConsole = Test-Path (Join-Path $Root 'frontend\dist\index.html')

Write-Host ''
Write-Host '  MoMo statement underwriting' -ForegroundColor Cyan
Write-Host ''
if ($hasConsole) {
    Write-Host "  Console   http://${display}:$Port"
} else {
    Write-Host '  Console   not built - run setup.ps1 without -SkipFrontend' -ForegroundColor Yellow
}
Write-Host "  API docs  http://${display}:$Port/docs"
Write-Host "  Health    http://${display}:$Port/api/v1/health"
if ($Password) {
    Write-Host ''
    Write-Host "  Sign in   $Username / $Password" -ForegroundColor Cyan
    Write-Host '  Password protection is ON. Share credentials separately from the link.'
} else {
    Write-Host ''
    Write-Host '  No password set. Fine on this machine; add -Password before sharing.' -ForegroundColor DarkGray
}
Write-Host ''
Write-Host '  Ctrl+C to stop.' -ForegroundColor DarkGray
Write-Host ''

if ($Lan) {
    $ruleName = "MoMo Underwriting $Port"
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if (-not $existing) {
        try {
            New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
            Write-Host "  Firewall rule added for port $Port (private networks)." -ForegroundColor DarkGray
        } catch {
            Write-Host "  Could not add a firewall rule. Run as Administrator, or add it manually." -ForegroundColor Yellow
        }
    }
}

$uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', $bindHost, '--port', $Port)
if ($Reload) { $uvicornArgs += '--reload' }

Push-Location $Backend
try {
    & $VenvPy @uvicornArgs
} finally {
    Pop-Location
}
