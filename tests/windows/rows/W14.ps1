<#
.SYNOPSIS
    W14: leave Explorer open on a stub folder for an hour while the edge restarts and the control API
    is briefly down.
.DESCRIPTION
    Expected: no error dialogs for listing; an open in progress waits or fails by policy. The script
    keeps Explorer open on -Folder, lists the folder every -PollSeconds for -Minutes, and records each
    listing that failed or took long. At -RestartAtMinute it asks for the filer to be restarted (or runs
    -RestartCommand on the filer over ssh), and at -OutageAtMinute for the control API to be cut off
    (for example, the edge's route to Azure dropped for five minutes).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    [Parameter(Mandatory = $true)] [string] $Folder,
    [int] $Minutes = 60,
    [int] $PollSeconds = 30,
    [int] $RestartAtMinute = 15,
    [int] $OutageAtMinute = 35,
    # Run on the filer at -RestartAtMinute, e.g. 'sudo -n systemctl restart omnifile-edge'. Empty: ask.
    [string] $RestartCommand,
    # A listing slower than this counts as a stall.
    [double] $SlowSeconds = 10
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
[void](Open-ExplorerFolder -Path $Folder -View details)
$polls = [math]::Max(1, [int]($Minutes * 60 / $PollSeconds))
$restartAt = [int]($RestartAtMinute * 60 / $PollSeconds)
$outageAt = [int]($OutageAtMinute * 60 / $PollSeconds)
$errors = New-Object System.Collections.Generic.List[string]
$slow = 0
$longest = 0.0
for ($i = 0; $i -lt $polls; $i++) {
    if ($i -eq $restartAt) {
        if ($RestartCommand) {
            try { [void](Invoke-EdgeCommand -Context $Context -Command $RestartCommand) } catch { $errors.Add("restart: $_") }
        } else {
            Write-Host "W14: restart the filer's daemon now: sudo systemctl restart omnifile-edge (the script keeps listing)"
        }
    }
    if ($i -eq $outageAt) {
        Write-Host 'W14: cut the filer off from the control API now, for about five minutes (the script keeps listing)'
    }
    try {
        $l = Get-FolderStates -Path $Folder
        if ($l.Seconds -gt $longest) { $longest = $l.Seconds }
        if ($l.Seconds -gt $SlowSeconds) { $slow++ }
    } catch {
        $errors.Add("poll $($i): $($_.Exception.Message)")
    }
    Start-Pause -Seconds $PollSeconds
}
$shown = Read-Observation -Context $Context -Question 'Did Windows show anything during the hour (an error dialog, a red X, a frozen window)?'
Write-MatrixResult -Context $Context -Row 'W14' -StartedUtc $started `
    -Action "Explorer open on stubs for $Minutes minutes, through a filer restart and a control API outage" `
    -Expected 'No error dialogs for listing; an open in progress waits or fails by policy.' `
    -Verdict (Resolve-Verdict @(($errors.Count -eq 0))) `
    -Measures @{ polls = $polls; listing_errors = $errors.Count; slow_listings = $slow; longest_listing_seconds = $longest } `
    -Observations @{ windows_showed = $shown } -Notes ($errors -join '; ')
