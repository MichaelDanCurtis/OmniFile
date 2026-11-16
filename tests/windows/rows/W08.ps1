<#
.SYNOPSIS
    W8: turn on Windows Search indexing for the mapped share, where policy allows it.
.DESCRIPTION
    Expected: offline files are not indexed. Records the recalls started and how many items, and how
    many offline files, the index holds after -Minutes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    # The share as the index sees it: the mapped drive (Z:\) or the UNC path added to a library.
    [Parameter(Mandatory = $true)] [string] $Path,
    # How long to let the indexer work.
    [int] $Minutes = 15
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
$offline = @((Get-FolderStates -Path $Path -Recurse).Files | Where-Object { $_.Offline } | ForEach-Object { $_.Name })
Wait-Tester -Context $Context -Instruction "Add $Path to the search index (Indexing Options, Modify; or include it in a library), where your policy allows it."
$m = Measure-Recalls -Context $Context -Action {
    Start-Pause -Seconds ($Minutes * 60)
    @(Get-IndexedItems -Scope $Path)
}
$indexed = @($m.Result)
$indexedOffline = @($indexed | Where-Object { $offline -contains (Split-Path -Leaf $_) }).Count
Write-MatrixResult -Context $Context -Row 'W8' -StartedUtc $started `
    -Action "Index the mapped share $Path for $Minutes minutes" -Expected 'Offline files are not indexed.' `
    -Verdict (Resolve-Verdict @((Test-NoRecall $m.Recalls), ($indexedOffline -eq 0))) `
    -Measures @{ offline_files = $offline.Count; indexed = $indexed.Count; indexed_offline = $indexedOffline
        recalls_started = (Get-RecallNumber $m.Recalls) }
