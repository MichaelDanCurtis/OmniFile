<#
.SYNOPSIS
    W9: mark a folder "Always available offline".
.DESCRIPTION
    Expected: refused, or no effect (every share sets csc policy = disable). Records what Windows says,
    what Offline Files holds afterwards, and the recalls started.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    [Parameter(Mandatory = $true)] [string] $Folder,
    # How long to give Offline Files to act after the click.
    [int] $Seconds = 60
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
$m = Measure-Recalls -Context $Context -Action {
    Wait-Tester -Context $Context -Instruction "In Explorer, right-click $Folder and choose 'Always available offline' (if Windows offers it)."
    Start-Pause -Seconds $Seconds
    Get-OfflineFilesState -Path $Folder
}
$said = Read-Observation -Context $Context -Question "What did Windows say, or was 'Always available offline' missing?"
Write-MatrixResult -Context $Context -Row 'W9' -StartedUtc $started `
    -Action 'Mark a folder Always available offline' -Expected 'Refused or no effect (csc policy = disable).' `
    -Verdict (Resolve-Verdict @((Test-NoRecall $m.Recalls), ($m.Result.ItemsCached -eq 0))) `
    -Measures @{ offline_files_enabled = $m.Result.Enabled; items_cached = $m.Result.ItemsCached
        recalls_started = (Get-RecallNumber $m.Recalls) } `
    -Observations @{ windows_said = $said }
