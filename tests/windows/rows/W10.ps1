<#
.SYNOPSIS
    W10: the Previous Versions tab on a folder, then on a file; Open, Copy and Restore from a label.
.DESCRIPTION
    Expected: labels listed newest first; Open reads that version's bytes; Restore follows decision 34.
    The script reads the file at one label through its @GMT path, as Open does, and checks the bytes;
    the tester records the tab itself and the restore.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    # The file's path below the share's root, e.g. projects\2026\line_1042.segy.
    [Parameter(Mandatory = $true)] [string] $RelativePath,
    # A label from the Previous Versions tab, as SMB names it: @GMT-YYYY.MM.DD-HH.MM.SS (UTC).
    [Parameter(Mandatory = $true)] [ValidatePattern('^@GMT-\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}$')] [string] $Label,
    # That version's SHA-256, from the portal (the file's versions); empty: not checked.
    [string] $ExpectedSha256
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
# UNC paths are put together as text: the share is \\filer\share, and @GMT tokens are a path element.
$rel = ($RelativePath -replace '/', '\').TrimStart('\')
$file = "$($Context.Share)\$rel"
$folder = $file.Substring(0, $file.LastIndexOf('\'))
Wait-Tester -Context $Context -Instruction "Open Properties, Previous Versions on $folder. Note how long the list takes to appear (labels fill on first open)."
$order = Read-Observation -Context $Context -Question 'Were the labels listed newest first? (y/n)'
$listing = Read-Observation -Context $Context -Question 'How long did the list take, in seconds?'
$versionPath = "$($Context.Share)\$Label\$rel"
$m = Measure-Recalls -Context $Context -Action { Read-FileTimed -Path $versionPath }
$read = $m.Result
$match = if (-not $read.Ok) { $false } elseif ($ExpectedSha256) { $read.Sha256 -eq $ExpectedSha256.ToLowerInvariant() } else { $null }
Wait-Tester -Context $Context -Instruction "Now, on the file's Previous Versions tab, use Restore on the same label."
$restore = Read-Observation -Context $Context -Question 'What did Restore do (the old version over the current one, the current one kept as a version)?'
$newestFirst = if ($order -match '^(y|yes)$') { $true } elseif ($order -match '^(n|no)$') { $false } else { $null }
Write-MatrixResult -Context $Context -Row 'W10' -StartedUtc $started `
    -Action "Previous Versions: list, Open and Restore $Label" `
    -Expected "Labels listed newest first; Open reads that version's bytes; Restore follows decision 34." `
    -Verdict (Resolve-Verdict @($read.Ok, $match, $newestFirst)) `
    -Measures @{ label = $Label; open_seconds = $read.Seconds; sha256 = $read.Sha256; error = $read.Error
        recalls_started = (Get-RecallNumber $m.Recalls) } `
    -Observations @{ newest_first = $order; list_seconds = $listing; restore = $restore }
