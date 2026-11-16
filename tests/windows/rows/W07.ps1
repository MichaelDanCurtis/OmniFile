<#
.SYNOPSIS
    W7: run Microsoft Defender's quick scan, then a custom scan of the mapped share.
.DESCRIPTION
    Expected: no recalls from the quick scan. A custom scan that opens files would recall them: it is
    recorded, with whether Defender skipped the offline files.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    # The share as Defender sees it: a mapped drive (Z:\) or the UNC path.
    [Parameter(Mandatory = $true)] [string] $Path
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
$stubs = @((Get-FolderStates -Path $Path -Recurse).Files | Where-Object { $_.Offline }).Count
$quick = Measure-Recalls -Context $Context -Action { Invoke-DefenderScan -ScanType QuickScan }
$custom = Measure-Recalls -Context $Context -Action { Invoke-DefenderScan -ScanType CustomScan -Path $Path }
$customRecalls = Get-RecallNumber $custom.Recalls
$skipped = if ($null -eq $customRecalls) { $null } else { $customRecalls -eq 0 }
Write-MatrixResult -Context $Context -Row 'W7' -StartedUtc $started `
    -Action "Defender quick scan, then a custom scan of $Path" `
    -Expected 'No recalls from the quick scan; the custom scan is recorded.' `
    -Verdict (Resolve-Verdict @((Test-NoRecall $quick.Recalls))) `
    -Measures @{ stubs = $stubs; quick_recalls = (Get-RecallNumber $quick.Recalls); custom_recalls = $customRecalls
        custom_skipped_offline = $skipped }
