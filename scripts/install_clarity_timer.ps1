# Register the daily Clarity snapshot as a Windows scheduled task.
#
# Clarity answers for three days and no further, so a day nobody captured is a
# day nobody will ever have. The task runs daily, catches up whatever is still
# in range when the machine was off, and never asks twice for a day it already
# has — the call budget is about ten a day.
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_clarity_timer.ps1 -Client sass-srq.com
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_clarity_timer.ps1 -Client sass-srq.com -Remove

param(
    [Parameter(Mandatory = $true)][string]$Client,
    [string]$Time = "09:00",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$taskName = "SEO Clarity snapshot - $Client"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[OK] removed: $taskName"
    exit 0
}

$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) { Write-Error "no python found"; exit 1 }
    $python = $found.Source
}
$repo = Split-Path -Parent $PSScriptRoot

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m seo_core.sources.clarity --client $Client" -WorkingDirectory $repo

# Daily, and again if the machine was asleep at the time: the missed-run catch-up
# only works if the task actually runs.
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Daily Microsoft Clarity snapshot for $Client" `
    -Force | Out-Null

Write-Host "[OK] scheduled: $taskName, daily at $Time"
Write-Host "     catch-up on wake is on; a failure raises a Windows notification"
Write-Host "     run now: Start-ScheduledTask -TaskName '$taskName'"
