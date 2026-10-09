#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Remove the underwriting Windows service or scheduled task.
.EXAMPLE
    .\uninstall-service.ps1
    .\uninstall-service.ps1 -UseTaskScheduler
#>
[CmdletBinding()]
param(
    [switch]$UseTaskScheduler,
    [int]$Port = 8000,
    [string]$ServiceName = 'MomoUnderwriting'
)

$ErrorActionPreference = 'Continue'

if ($UseTaskScheduler) {
    if (Get-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $ServiceName -Confirm:$false
        Write-Host "Scheduled task '$ServiceName' removed." -ForegroundColor Green
    } else {
        Write-Host "No scheduled task named '$ServiceName'." -ForegroundColor Yellow
    }
} else {
    $nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        if ($nssm) {
            & $nssm stop $ServiceName 2>$null | Out-Null
            & $nssm remove $ServiceName confirm | Out-Null
        } else {
            Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
            sc.exe delete $ServiceName | Out-Null
        }
        Write-Host "Service '$ServiceName' removed." -ForegroundColor Green
    } else {
        Write-Host "No service named '$ServiceName'." -ForegroundColor Yellow
    }
}

$ruleName = "MoMo Underwriting $Port"
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Remove-NetFirewallRule -DisplayName $ruleName
    Write-Host "Firewall rule removed." -ForegroundColor Green
}
