<#
.SYNOPSIS
    W13: save over an existing file from Office (Word or Excel), which writes a temp file and renames it.
.DESCRIPTION
    Expected: a new version of the same file, not a new file. The script first saves the way Word does
    (write ~WRDnnnn.tmp, rename the original to ~WRLnnnn.tmp, rename the temp to the original's name,
    delete the old one), then asks the tester to do a real save in Word, and to read the file's versions
    in the portal.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    # A folder on the share where the test user may write.
    [Parameter(Mandatory = $true)] [string] $Folder
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
$file = Join-Path $Folder 'w13-office-save.txt'
Set-Content -LiteralPath $file -Value 'version 1'
Wait-Tester -Context $Context -Instruction "Wait until the portal lists $(Split-Path -Leaf $file) (version 1)."

# The save, as Word does it.
$temp = Join-Path $Folder '~WRD0001.tmp'
$old = Join-Path $Folder '~WRL0001.tmp'
Set-Content -LiteralPath $temp -Value 'version 2, saved the way Word saves'
Move-Item -LiteralPath $file -Destination $old
Move-Item -LiteralPath $temp -Destination $file
Remove-Item -LiteralPath $old
$content = (Get-Content -LiteralPath $file -Raw).Trim()
$leftovers = @(Get-ChildItem -LiteralPath $Folder -Force -Filter '~WR*.tmp').Count

Wait-Tester -Context $Context -Instruction "Now open a Word document on the share, change it, and save it (Ctrl+S)."
$emulated = Read-Observation -Context $Context -Question "How many versions does the portal list for $(Split-Path -Leaf $file)? (expected 2)"
$word = Read-Observation -Context $Context -Question 'For the Word document: one more version of the same file, or a new file? (version/new)'
$sameFile = if ($word -match '^version') { $true } elseif ($word -match '^new') { $false } else { $null }
$twoVersions = if ($emulated -match '^\d+$') { [int]$emulated -eq 2 } else { $null }
Write-MatrixResult -Context $Context -Row 'W13' -StartedUtc $started `
    -Action 'Save over a file the way Office does (temp file, then renames)' `
    -Expected 'A new version of the same file, not a new file.' `
    -Verdict (Resolve-Verdict @(($content -eq 'version 2, saved the way Word saves'), ($leftovers -eq 0), $twoVersions, $sameFile)) `
    -Measures @{ file = $file; temp_files_left = $leftovers } `
    -Observations @{ portal_versions = $emulated; word_save = $word }
