#Requires -Version 5.1
<#
.SYNOPSIS
    Publish the local service on a temporary public HTTPS URL via ngrok.

.DESCRIPTION
    Run this in a SECOND PowerShell window, while start.ps1 is already running
    in the first.

    This service handles national IDs, dates of birth, phone numbers and full
    transaction histories. A public URL with no password puts all of that in
    front of anyone who has the link, and search engines do find shared tunnel
    URLs. The script therefore refuses to publish an unprotected service unless
    you explicitly pass -NoPassword.

.PARAMETER Port
    Local port the service is listening on. Default 8000.

.PARAMETER Domain
    Your ngrok static domain, e.g. my-app.ngrok-free.app. The free plan includes
    one. Without it the URL changes on every restart.

.PARAMETER NoPassword
    Publish without checking for a password. Use only with throwaway test data.

.EXAMPLE
    .\share.ps1
    .\share.ps1 -Domain my-app.ngrok-free.app
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$Domain = "",
    [switch]$NoPassword
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

# --- is ngrok installed? ---------------------------------------------------
if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    Write-Host 'ngrok is not installed.' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  winget install ngrok.ngrok'
    Write-Host ''
    Write-Host '  Then create a free account at https://dashboard.ngrok.com/signup'
    Write-Host '  copy your authtoken, and run:'
    Write-Host ''
    Write-Host '  ngrok config add-authtoken YOUR_TOKEN_HERE'
    exit 1
}

# --- is the service actually running? --------------------------------------
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 5
} catch {
    Fail "Nothing is answering on port $Port. Start the service first, in another window:`n         .\start.ps1 -Password `"choose-a-password`""
}

# --- is it protected? ------------------------------------------------------
if (-not $health.password_protected -and -not $NoPassword) {
    Write-Host ''
    Write-Host '  The service is running WITHOUT a password.' -ForegroundColor Red
    Write-Host ''
    Write-Host '  Publishing it would expose national IDs, dates of birth, phone'
    Write-Host '  numbers and transaction histories to anyone with the link.'
    Write-Host ''
    Write-Host '  Stop the service (Ctrl+C in the other window) and restart it as:'
    Write-Host ''
    Write-Host "      .\start.ps1 -Password `"choose-a-password`"" -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  Then run this script again. To publish anyway with throwaway'
    Write-Host '  test data only, re-run with -NoPassword.'
    exit 1
}

if ($NoPassword -and -not $health.password_protected) {
    Write-Host ''
    Write-Host '  WARNING: publishing an unprotected service. Use test data only.' -ForegroundColor Yellow
}

# --- go --------------------------------------------------------------------
Write-Host ''
Write-Host '  Opening a public tunnel to the underwriting service' -ForegroundColor Cyan
Write-Host ''
Write-Host "  Local port     $Port"
Write-Host "  Password       $(if ($health.password_protected) { 'yes - share the username and password separately from the link' } else { 'NO' })"
Write-Host ''
Write-Host '  ngrok prints the public URL as "Forwarding". Share that.'
Write-Host '  On the free plan, visitors see an ngrok warning page first and'
Write-Host '  click through - that is normal, not an error.'
Write-Host ''
Write-Host '  Ctrl+C here closes the tunnel. The service keeps running.' -ForegroundColor DarkGray
Write-Host ''

$ngrokArgs = @('http', "$Port")
if ($Domain) { $ngrokArgs += @('--url', "https://$Domain") }

& ngrok @ngrokArgs
