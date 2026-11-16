<#
.SYNOPSIS
    W2: select a stub with Explorer's preview pane open.
.DESCRIPTION
    Expected: no preview, and no recall. Records the recalls started.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    # A stub with a type Explorer previews (a PDF, an image or a text file).
    [Parameter(Mandatory = $true)] [string] $File
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
$folder = Split-Path -Parent $File
[void](Open-ExplorerFolder -Path $folder -View details)
Wait-Tester -Context $Context -Instruction 'In that Explorer window, turn on the preview pane (View, Preview pane, or Alt+P). Select nothing yet.'
$m = Measure-Recalls -Context $Context -Action { Select-InExplorer -Path $File }
$preview = Read-Observation -Context $Context -Question "With $(Split-Path -Leaf $File) selected, did the preview pane show its content? (y/n)"
$noPreview = if ($preview -match '^(y|yes)$') { $false } elseif ($preview -match '^(n|no)$') { $true } else { $null }
Write-MatrixResult -Context $Context -Row 'W2' -StartedUtc $started `
    -Action 'Select a stub with the preview pane open' -Expected 'No preview, no recall.' `
    -Verdict (Resolve-Verdict @((Test-NoRecall $m.Recalls), $noPreview)) `
    -Measures @{ selected = [bool]$m.Result; recalls_started = (Get-RecallNumber $m.Recalls) } `
    -Observations @{ preview_shown = $preview }
