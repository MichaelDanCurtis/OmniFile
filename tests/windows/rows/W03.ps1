<#
.SYNOPSIS
    W3: right-click a stub, Properties.
.DESCRIPTION
    Expected: size and dates shown, attributes include Offline (Advanced), and no recall.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    [Parameter(Mandatory = $true)] [string] $File
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
$state = Get-ItemState -Path $File
$m = Measure-Recalls -Context $Context -Action { Show-Properties -Path $File }
$shown = Read-Observation -Context $Context -Question 'In the Properties dialog: are the size and dates shown, and under Advanced, is the file marked Offline? (y/n)'
$ok = if ($shown -match '^(y|yes)$') { $true } elseif ($shown -match '^(n|no)$') { $false } else { $null }
Write-MatrixResult -Context $Context -Row 'W3' -StartedUtc $started `
    -Action 'Right-click a stub, Properties' `
    -Expected 'Size and dates shown; attributes include Offline (Advanced). No recall.' `
    -Verdict (Resolve-Verdict @((Test-NoRecall $m.Recalls), $state.Offline, $ok)) `
    -Measures @{ size = $state.Length; attributes = $state.Attributes; offline_attribute = $state.Offline
        recalls_started = (Get-RecallNumber $m.Recalls) } `
    -Observations @{ properties_shown = $shown }
