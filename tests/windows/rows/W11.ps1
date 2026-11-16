<#
.SYNOPSIS
    W11: the Security tab on a share root and a subfolder, as Owner, Writer and Reader.
.DESCRIPTION
    Expected: the AD groups' entries are shown, and only Owners may change them (access-control.md).
    Run it once signed in as a member of each group, with the matrix's Role set to match. The script
    reads both ACLs and tries a harmless edit (add, then remove, Everyone: read) on the subfolder.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    [Parameter(Mandatory = $true)] [string] $Root,
    [Parameter(Mandatory = $true)] [string] $Subfolder
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')
if (@('Owner', 'Writer', 'Reader') -notcontains $Context.Role) { throw "W11 needs Role = Owner, Writer or Reader, not $($Context.Role)" }

$started = Get-UtcNow
$rootAcl = @(Get-PathAcl -Path $Root)
$subAcl = @(Get-PathAcl -Path $Subfolder)
$allowed = Test-AclEdit -Path $Subfolder
$shouldBe = ($Context.Role -eq 'Owner')
$tab = Read-Observation -Context $Context -Question 'On the Security tab, are the share''s AD groups listed (Owners, Writers, Readers)? (y/n)'
$tabOk = if ($tab -match '^(y|yes)$') { $true } elseif ($tab -match '^(n|no)$') { $false } else { $null }
Write-MatrixResult -Context $Context -Row 'W11' -StartedUtc $started `
    -Action "Security tab on the root and a subfolder, as $($Context.Role)" `
    -Expected "The AD groups' entries shown; only Owners may change them." `
    -Verdict (Resolve-Verdict @(($rootAcl.Count -gt 0), ($allowed -eq $shouldBe), $tabOk)) `
    -Measures @{ root_entries = @($rootAcl | ForEach-Object { "$($_.Identity):$($_.Type)" })
        subfolder_entries = $subAcl.Count; edit_allowed = $allowed; edit_should_be_allowed = $shouldBe } `
    -Observations @{ groups_shown = $tab }
