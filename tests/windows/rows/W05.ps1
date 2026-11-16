<#
.SYNOPSIS
    W5: open a stub on a "failfast" share.
.DESCRIPTION
    Expected: an error at once, <file>.RECALLING.txt beside it, and a Teams card to the user. Records
    the error Windows reports (the NT status Samba maps EIO to, as an HRESULT) and the time to error.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    [Parameter(Mandatory = $true)] [string] $File,
    # What "at once" means, in seconds.
    [int] $WithinSeconds = 10
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')
if ($Context.ShareKind -ne 'failfast') { throw "W5 runs on a failfast share; this one is $($Context.ShareKind)" }

$started = Get-UtcNow
$m = Measure-Recalls -Context $Context -Action { Read-FileTimed -Path $File }
$read = $m.Result
$note = Test-ItemExists -Path "$File.RECALLING.txt"
Wait-Tester -Context $Context -Instruction "Now double-click $(Split-Path -Leaf $File) in Explorer, and read the message Windows shows."
$shown = Read-Observation -Context $Context -Question 'What did Windows say (the message text)?'
$card = Read-Observation -Context $Context -Question 'Did a Teams card about the recall reach the user? (y/n)'
$cardOk = if ($card -match '^(y|yes)$') { $true } elseif ($card -match '^(n|no)$') { $false } else { $null }
$recalls = Get-RecallNumber $m.Recalls
$started1 = if ($null -eq $recalls) { $null } else { $recalls -ge 1 }
Write-MatrixResult -Context $Context -Row 'W5' -StartedUtc $started `
    -Action 'Open a stub on a failfast share' `
    -Expected 'An error at once, <file>.RECALLING.txt beside it, and a Teams card to the user.' `
    -Verdict (Resolve-Verdict @((-not $read.Ok), ($read.Seconds -le $WithinSeconds), $note, $started1, $cardOk)) `
    -Measures @{ error = $read.Error; hresult = $read.HResult; seconds_to_error = $read.Seconds
        recalling_note = $note; recalls_started = $recalls } `
    -Observations @{ windows_said = $shown; teams_card = $card }
