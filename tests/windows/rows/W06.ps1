<#
.SYNOPSIS
    W6: copy a folder with stubs in it, with Explorer, then robocopy /E, then robocopy /E /XA:O.
.DESCRIPTION
    Expected: Explorer and plain robocopy read (and recall) every stub; /XA:O skips the offline files.
    Records the recalls each tool started and robocopy's skipped count. Each copy needs its own folder of
    stubs, since a copy that recalls fills them: -ExplorerSource and -RobocopySource. /XA:O recalls
    nothing, so -XaoSource may be either of them before its copy, or a third.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    [Parameter(Mandatory = $true)] [string] $ExplorerSource,
    [Parameter(Mandatory = $true)] [string] $RobocopySource,
    [Parameter(Mandatory = $true)] [string] $XaoSource,
    # A local folder the copies land in (it is created; each tool gets its own subfolder).
    [Parameter(Mandatory = $true)] [string] $Destination
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
$xaoOffline = @((Get-FolderStates -Path $XaoSource -Recurse).Files | Where-Object { $_.Offline }).Count
$explorerOffline = @((Get-FolderStates -Path $ExplorerSource -Recurse).Files | Where-Object { $_.Offline }).Count
$robocopyOffline = @((Get-FolderStates -Path $RobocopySource -Recurse).Files | Where-Object { $_.Offline }).Count

# /XA:O first, while its source still holds stubs.
$xao = Measure-Recalls -Context $Context -Action {
    Invoke-Robocopy -Arguments @($XaoSource, (Join-Path $Destination 'xao'), '/E', '/XA:O', '/R:0', '/W:0', '/NP')
}
$explorer = Measure-Recalls -Context $Context -Action { Copy-WithExplorer -Source $ExplorerSource -Destination (Join-Path $Destination 'explorer') }
$plain = Measure-Recalls -Context $Context -Action {
    Invoke-Robocopy -Arguments @($RobocopySource, (Join-Path $Destination 'robocopy'), '/E', '/R:0', '/W:0', '/NP')
}
$xaoSummary = ConvertFrom-RobocopySummary -Output $xao.Result.Output
$plainSummary = ConvertFrom-RobocopySummary -Output $plain.Result.Output
$xaoSkipped = if ($xaoSummary) { $xaoSummary.Skipped } else { $null }
$xaoSkipsAll = if ($null -eq $xaoSkipped) { $null } else { $xaoSkipped -ge $xaoOffline }
Write-MatrixResult -Context $Context -Row 'W6' -StartedUtc $started `
    -Action 'Copy a folder with stubs: Explorer, robocopy /E, robocopy /E /XA:O' `
    -Expected 'Explorer and plain robocopy read (and recall) every stub; /XA:O skips the offline files.' `
    -Verdict (Resolve-Verdict @((Test-NoRecall $xao.Recalls), $xaoSkipsAll, ($xao.Result.ExitCode -lt 8), ($plain.Result.ExitCode -lt 8))) `
    -Measures @{ explorer_stubs = $explorerOffline; explorer_recalls = (Get-RecallNumber $explorer.Recalls)
        robocopy_stubs = $robocopyOffline; robocopy_recalls = (Get-RecallNumber $plain.Recalls)
        robocopy_copied = $(if ($plainSummary) { $plainSummary.Copied } else { $null })
        xao_stubs = $xaoOffline; xao_recalls = (Get-RecallNumber $xao.Recalls); xao_skipped = $xaoSkipped }
