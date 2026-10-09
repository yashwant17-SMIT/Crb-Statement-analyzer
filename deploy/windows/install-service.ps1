#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install the underwriting service so it starts with Windows.

.DESCRIPTION
    There is no gunicorn on Windows, so the process needs a supervisor.

    Preferred: NSSM, which runs uvicorn as a genuine Windows service with
    automatic restart and log rotation. Install it with:  winget install NSSM.NSSM

    Fallback: -UseTaskScheduler registers a scheduled task that runs at boot.
    It starts without anyone logging in, but it will not restart on crash.

.PARAMETER Port
    Listening port. Default 8000.

.PARAMETER Lan
    Bind 0.0.0.0 and open the firewall, so other machines can reach it.

.PARAMETER UseTaskScheduler
    Use Task Scheduler instead of NSSM.

.EXAMPLE
    .\install-service.ps1 -Lan
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$Lan,
    [switch]$UseTaskScheduler,
    [string]$ServiceName = 'MomoUnderwriting'
)

$ErrorActionPreference = 'Stop'

$Root    = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Backend = Join-Path $Root 'backend'
$VenvPy  = Join-Path $Backend '.venv\Scripts\python.exe'
$LogDir  = Join-Path $Root 'logs'

if (-not (Test-Path $VenvPy)) {
    Write-Host 'Virtual environment not found. Run .\setup.ps1 first.' -ForegroundColor Red
    exit 1
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$bindHost = if ($Lan) { '0.0.0.0' } else { '127.0.0.1' }
$appArgs  = "-m uvicorn app.main:app --host $bindHost --port $Port"

if ($Lan) {
    $ruleName = "MoMo Underwriting $Port"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
        Write-Host "Firewall rule added for port $Port (private networks)." -ForegroundColor Green
    }
}

if ($UseTaskScheduler) {
    Write-Host 'Registering a scheduled task...' -ForegroundColor Cyan

    $action = New-ScheduledTaskAction -Execute $VenvPy -Argument $appArgs -WorkingDirectory $Backend
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    Unregister-ScheduledTask -TaskName $ServiceName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $ServiceName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description 'MoMo statement underwriting API and console' | Out-Null
    Start-ScheduledTask -TaskName $ServiceName

    Write-Host "`nScheduled task '$ServiceName' registered and started." -ForegroundColor Green
    Write-Host "  Status:  Get-ScheduledTask -TaskName $ServiceName"
    Write-Host "  Stop:    Stop-ScheduledTask -TaskName $ServiceName"
    Write-Host "  Remove:  .\uninstall-service.ps1 -UseTaskScheduler"
    exit 0
}

$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssm) {
    Write-Host 'NSSM not found.' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  Install it:   winget install NSSM.NSSM'
    Write-Host '  Then re-run:  .\install-service.ps1'
    Write-Host ''
    Write-Host '  Or use the built-in scheduler instead (no restart-on-crash):'
    Write-Host '                .\install-service.ps1 -UseTaskScheduler'
    exit 1
}

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "Service '$ServiceName' already exists. Removing it first..." -ForegroundColor Yellow
    & $nssm stop $ServiceName 2>$null | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 2
}

Write-Host "Installing service '$ServiceName'..." -ForegroundColor Cyan
& $nssm install $ServiceName $VenvPy $appArgs
& $nssm set $ServiceName AppDirectory $Backend
& $nssm set $ServiceName DisplayName 'MoMo Statement Underwriting'
& $nssm set $ServiceName Description 'Parses MoMo statement PDFs and returns an underwriting report.'
& $nssm set $ServiceName Start SERVICE_AUTO_START

# UTF-8: the default console codepage cannot encode every character the engine
# emits, and a service has no console to fix afterwards.
& $nssm set $ServiceName AppEnvironmentExtra "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8" "ALLOWED_ORIGINS=http://localhost:$Port"

& $nssm set $ServiceName AppStdout (Join-Path $LogDir 'service.log')
& $nssm set $ServiceName AppStderr (Join-Path $LogDir 'service.err.log')
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateOnline 1
& $nssm set $ServiceName AppRotateBytes 10485760

# Restart on crash, but back off so a genuinely broken build does not spin.
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 5000

& $nssm start $ServiceName
Start-Sleep -Seconds 6

$svc = Get-Service -Name $ServiceName
Write-Host "`nService '$ServiceName' is $($svc.Status)." -ForegroundColor Green

try {
    $probe = if ($Lan) { 'localhost' } else { '127.0.0.1' }
    $health = Invoke-RestMethod -Uri "http://${probe}:$Port/api/v1/health" -TimeoutSec 10
    Write-Host "Health check: $($health.status), version $($health.version)" -ForegroundColor Green
    Write-Host "`n  Console:  http://localhost:$Port"
} catch {
    Write-Host "Health check failed. Inspect $LogDir\service.err.log" -ForegroundColor Yellow
}

Write-Host ''
Write-Host "  Stop:     nssm stop $ServiceName"
Write-Host "  Restart:  nssm restart $ServiceName"
Write-Host "  Logs:     $LogDir"
Write-Host "  Remove:   .\uninstall-service.ps1"
