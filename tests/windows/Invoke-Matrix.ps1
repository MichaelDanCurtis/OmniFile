<#
.SYNOPSIS
    Run rows of the Windows client matrix (docs/pilots/windows-client-matrix.md) on this client.
.DESCRIPTION
    Reads the settings file (copy matrix.example.psd1 and fill it in), then runs each row it lists, or
    only those named with -Rows, in order. Each row writes <OutDir>\Wnn.json and a line of
    <OutDir>\results.md. A row that stops with an error is recorded as failed, with the error, and the
    next row runs. Prints a summary at the end and returns the results.
.EXAMPLE
    .\Invoke-Matrix.ps1 -Settings .\matrix.psd1
.EXAMPLE
    .\Invoke-Matrix.ps1 -Settings .\matrix.psd1 -Rows W1, W5
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Settings,
    [string[]] $Rows
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'OmniFileMatrix.psm1')

$s = Import-PowerShellDataFile -Path $Settings
if (-not $s.ContainsKey('Rows')) { throw "$Settings has no Rows" }
$ctx = New-MatrixContext -Settings $s
$want = if ($Rows) { @($Rows | ForEach-Object { 'W{0:D2}' -f [int]($_ -replace '^W', '') }) } else { @($s.Rows.Keys | Sort-Object) }
$results = @()
foreach ($row in $want) {
    if (-not $s.Rows.ContainsKey($row)) { Write-Warning "$row is not in $Settings; skipped"; continue }
    $script = Join-Path (Join-Path $PSScriptRoot 'rows') "$row.ps1"
    $args1 = $s.Rows[$row]
    Write-Host "== $row"
    try {
        $r = & $script -Context $ctx @args1
        $results += @($r | Where-Object { $_ -and $_.PSObject.Properties['row'] })
    } catch {
        $short = 'W' + [int]($row -replace '^W', '')
        $results += Write-MatrixResult -Context $ctx -Row $short -Action "$row (stopped)" -Expected 'see the matrix' `
            -Verdict 'fail' -Notes "the runner stopped: $($_.Exception.Message)"
    }
}
$summary = $results | Group-Object -Property verdict | ForEach-Object { "$($_.Name) $($_.Count)" }
Write-Host "matrix: $($results.Count) rows; $($summary -join ', '). Results in $($ctx.OutDir)"
return $results
