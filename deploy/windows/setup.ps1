#Requires -Version 5.1
<#
.SYNOPSIS
    One-time setup for the MoMo underwriting service on Windows.

.DESCRIPTION
    Creates a Python virtual environment, installs dependencies, and builds the
    React console into frontend/dist so the API can serve it on a single port.

.PARAMETER SkipFrontend
    Install the API only. The service then runs headless and the console must be
    served elsewhere.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Repo root is two levels above deploy\windows
$Root     = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Backend  = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Venv     = Join-Path $Backend '.venv'
$VenvPy   = Join-Path $Venv 'Scripts\python.exe'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Fail($msg)       { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

Write-Step 'Checking Python'

# Prefer the py launcher; fall back to python on PATH.
$pythonCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCmd = @('py', '-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCmd = @('python')
} else {
    Fail "Python not found. Install Python 3.11 or newer from https://python.org and tick 'Add python.exe to PATH'."
}

$verText = & $pythonCmd[0] @($pythonCmd[1..($pythonCmd.Length - 1)]) -c "import sys;print('%d.%d' % sys.version_info[:2])"
$ver = [version]$verText
if ($ver -lt [version]'3.11') {
    Fail "Python $verText found, but 3.11 or newer is required (pandas and numpy wheels)."
}
Write-Ok "Python $verText"

Write-Step 'Creating virtual environment'
if (Test-Path $VenvPy) {
    Write-Ok 'Already exists, reusing'
} else {
    & $pythonCmd[0] @($pythonCmd[1..($pythonCmd.Length - 1)]) -m venv $Venv
    if (-not (Test-Path $VenvPy)) { Fail "Virtual environment was not created at $Venv" }
    Write-Ok $Venv
}

Write-Step 'Installing Python dependencies'
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $Backend 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail 'pip install failed. See the output above.' }
Write-Ok 'Dependencies installed'

Write-Step 'Verifying the engine imports'
Push-Location $Backend
try {
    & $VenvPy -c "import app.main; print('ok')"
    if ($LASTEXITCODE -ne 0) { Fail 'The application failed to import.' }
} finally {
    Pop-Location
}
Write-Ok 'Engine imports cleanly'

if ($SkipFrontend) {
    Write-Step 'Skipping the console build (-SkipFrontend)'
} else {
    Write-Step 'Building the console'
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Fail "Node.js not found. Install the LTS build from https://nodejs.org, or re-run with -SkipFrontend to install the API only."
    }
    $nodeVer = (node --version).TrimStart('v')
    if ([version]($nodeVer.Split('-')[0]) -lt [version]'18.0.0') {
        Fail "Node $nodeVer found, but 18 or newer is required by Vite 6."
    }
    Write-Ok "Node $nodeVer"

    Push-Location $Frontend
    try {
        npm install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { Fail 'npm install failed.' }
        npm run build
        if ($LASTEXITCODE -ne 0) { Fail 'npm run build failed.' }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path (Join-Path $Frontend 'dist\index.html'))) {
        Fail 'The build finished but frontend\dist\index.html is missing.'
    }
    Write-Ok 'Console built to frontend\dist'
}

Write-Host "`nSetup complete." -ForegroundColor Green
Write-Host "  Start it now:        .\start.ps1"
Write-Host "  Install as service:  .\install-service.ps1   (run as Administrator)"
