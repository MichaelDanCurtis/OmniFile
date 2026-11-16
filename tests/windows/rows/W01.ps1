<#
.SYNOPSIS
    W1: open a folder of 50 stubs in Explorer, in Details, then Large icons view.
.DESCRIPTION
    Expected: the files show as offline, and no recall starts. Records whether Explorer drew the offline
    overlay, how many recalls started (should be 0), and how long the folder took to list.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    # A folder on the share holding about 50 stubs (files whose data is in Azure, not on the filer).
    [Parameter(Mandatory = $true)] [string] $Folder
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
$listing = Get-FolderStates -Path $Folder
$offline = @($listing.Files | Where-Object { $_.Offline })
$m = Measure-Recalls -Context $Context -Action {
    [void](Open-ExplorerFolder -Path $Folder -View details)
    Start-Pause -Seconds $Context.SettleSeconds
    [void](Open-ExplorerFolder -Path $Folder -View largeicons)
}
$overlay = Read-Observation -Context $Context -Question 'Did Explorer show the files as offline (the offline overlay or grey icons), in both views? (y/n)'
$allOffline = ($listing.Files.Count -gt 0 -and $offline.Count -eq $listing.Files.Count)
Write-MatrixResult -Context $Context -Row 'W1' -StartedUtc $started `
    -Action 'Open a folder of stubs in Explorer, Details then Large icons' `
    -Expected 'The files show as offline. No recall starts.' `
    -Verdict (Resolve-Verdict @((Test-NoRecall $m.Recalls), $allOffline)) `
    -Measures @{ files = $listing.Files.Count; offline_attribute = $offline.Count; list_seconds = $listing.Seconds
        recalls_started = (Get-RecallNumber $m.Recalls) } `
    -Observations @{ overlay_shown = $overlay }
