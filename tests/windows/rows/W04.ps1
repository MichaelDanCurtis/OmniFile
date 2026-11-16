<#
.SYNOPSIS
    W4: on a "wait" share, open a stub of a few MB in Notepad, then a 2 GB stub in the client's
    seismic tool.
.DESCRIPTION
    Expected: each open waits (the filer answers STATUS_PENDING) until the recall completes, then reads
    the right bytes. Records how long each application waited, whether it showed "Not responding" or an
    error, and the bytes' hash. Without -AppPath the 2 GB stub is read by this script, as a batch job
    would read it.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    [Parameter(Mandatory = $true)] [string] $SmallFile,
    [Parameter(Mandatory = $true)] [string] $LargeFile,
    # The files' SHA-256 when they were written (from the portal, or the writer's own record).
    [string] $SmallSha256,
    [string] $LargeSha256,
    # The seismic application's executable; empty: the script reads the large file itself.
    [string] $AppPath
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')
if ($Context.ShareKind -ne 'wait') { throw "W4 runs on a wait share; this one is $($Context.ShareKind)" }

$started = Get-UtcNow
function Test-Hash($read, [string] $want) {
    if (-not $read.Ok) { return $false }
    if (-not $want) { return $null }
    return ($read.Sha256 -eq $want.ToLowerInvariant())
}

# Notepad on the small stub: the tester times it.
$notepad = Measure-Recalls -Context $Context -Action {
    $t0 = Get-UtcNow
    [void](Start-App -Path 'notepad.exe' -Argument $SmallFile)
    Wait-Tester -Context $Context -Instruction "Wait until Notepad shows $(Split-Path -Leaf $SmallFile), or an error."
    [math]::Round(((Get-UtcNow) - $t0).TotalSeconds, 1)
}
$notepadSaw = Read-Observation -Context $Context -Question 'What did Notepad do while it waited (nothing, "Not responding", an error)?'
$small = Read-FileTimed -Path $SmallFile

# The large stub: in the client's application, or read here.
if ($AppPath) {
    $app = Measure-Recalls -Context $Context -Action {
        $t0 = Get-UtcNow
        [void](Start-App -Path $AppPath -Argument $LargeFile)
        Wait-Tester -Context $Context -Instruction "Wait until the application has opened $(Split-Path -Leaf $LargeFile), or gives up."
        [math]::Round(((Get-UtcNow) - $t0).TotalSeconds, 1)
    }
    $appSaw = Read-Observation -Context $Context -Question 'What did the application do while it waited (nothing, "Not responding", an error, a timeout after how long)?'
    $large = Read-FileTimed -Path $LargeFile
    $largeWait = $app.Result
    $largeRecalls = Get-RecallNumber $app.Recalls
} else {
    $read = Measure-Recalls -Context $Context -Action { Read-FileTimed -Path $LargeFile }
    $large = $read.Result
    $appSaw = 'not run: read by the script'
    $largeWait = $large.Seconds
    $largeRecalls = Get-RecallNumber $read.Recalls
}

Write-MatrixResult -Context $Context -Row 'W4' -StartedUtc $started `
    -Action 'Open stubs on a wait share: a few MB in Notepad, 2 GB in the seismic tool' `
    -Expected 'The open waits (STATUS_PENDING) until the recall completes, then reads the right bytes.' `
    -Verdict (Resolve-Verdict @($small.Ok, $large.Ok, (Test-Hash $small $SmallSha256), (Test-Hash $large $LargeSha256))) `
    -Measures @{ notepad_wait_seconds = $notepad.Result; small_sha256 = $small.Sha256; small_error = $small.Error
        large_wait_seconds = $largeWait; large_sha256 = $large.Sha256; large_error = $large.Error
        recalls_started = @((Get-RecallNumber $notepad.Recalls), $largeRecalls) } `
    -Observations @{ notepad = $notepadSaw; application = $appSaw }
