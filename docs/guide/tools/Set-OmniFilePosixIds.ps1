<#
.SYNOPSIS
    Give AD users and groups the POSIX attributes OmniFile needs (docs/guide/README.md, section 1.4).

.DESCRIPTION
    The edge filer maps AD users and groups to numeric ids with winbind's idmap_ad, from the RFC 2307
    attributes: uidNumber on users, gidNumber on groups, and loginShell on the filer's administrators.

    For each group named, and every user in it (nested groups included):
      - a group without gidNumber gets the next free gidNumber;
      - a user without uidNumber gets the next free uidNumber;
      - a member of a -LoginShellGroup without loginShell gets -LoginShell.
    The members of Domain Users are not listed (AD doesn't record primary-group members), so name the
    share and admin groups too: only their members get a uidNumber.

    A number already set is never changed, and a number in use anywhere in the domain is never handed
    out again: new numbers start above the highest in use, or at -FirstUid / -FirstGid if higher.
    Every number is inside 10000-999999, the filer's idmap range.

    -Report lists what is missing. Without -Apply it prints what it would change and changes nothing.
    With -Apply it makes the changes. Runs with the ActiveDirectory module (RSAT), as a domain admin.

.EXAMPLE
    .\Set-OmniFilePosixIds.ps1 -Report -Groups 'Domain Users','OmniFile-Admins','Proj-Owners' -LoginShellGroup 'OmniFile-Admins'

.EXAMPLE
    .\Set-OmniFilePosixIds.ps1 -Groups 'Domain Users','OmniFile-Admins','Proj-Owners' -LoginShellGroup 'OmniFile-Admins' -FirstUid 20000 -FirstGid 30000 -Apply
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string[]] $Groups,
    [string[]] $LoginShellGroup = @(),
    [string] $LoginShell = '/bin/bash',
    [int] $FirstUid = 20000,
    [int] $FirstGid = 30000,
    [switch] $Report,
    [switch] $Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Low = 10000
$High = 999999

if (-not (Get-Command Get-ADUser -ErrorAction SilentlyContinue)) {
    Import-Module ActiveDirectory -ErrorAction Stop
}
foreach ($n in @($FirstUid, $FirstGid)) {
    if ($n -lt $Low -or $n -gt $High) {
        throw "-FirstUid and -FirstGid must be inside $Low-$High (the filer's idmap range)"
    }
}

function Get-Attr($Object, [string] $Name) {
    # An AD attribute's value, or $null when the object doesn't carry it (unset attributes may be absent).
    $p = $Object.PSObject.Properties[$Name]
    if ($p) { return $p.Value }
    return $null
}

function Get-HighestInUse([string] $Attribute, [string] $Kind) {
    $filter = "($Attribute=*)"
    $found = if ($Kind -eq 'user') {
        @(Get-ADUser -LDAPFilter $filter -Properties $Attribute)
    } else {
        @(Get-ADGroup -LDAPFilter $filter -Properties $Attribute)
    }
    $top = 0
    foreach ($o in $found) {
        $v = [int](Get-Attr $o $Attribute)
        if ($v -le $High -and $v -gt $top) { $top = $v }
    }
    return $top
}

# What is missing.
$groupsMissing = New-Object System.Collections.Generic.List[object]
$usersMissingUid = New-Object System.Collections.Generic.List[object]
$usersMissingShell = New-Object System.Collections.Generic.List[object]
$seen = @{}
$shellGroups = @{}
foreach ($g in $LoginShellGroup) { $shellGroups[$g.ToLowerInvariant()] = $true }

foreach ($name in $Groups) {
    $group = Get-ADGroup -Identity $name -Properties gidNumber
    if (-not (Get-Attr $group 'gidNumber')) { $groupsMissing.Add($group) }
    if ($group.Name -eq 'Domain Users') { continue }
    $members = @(Get-ADGroupMember -Identity $name -Recursive | Where-Object { $_.objectClass -eq 'user' })
    foreach ($m in $members) {
        $user = Get-ADUser -Identity $m.SamAccountName -Properties uidNumber, loginShell
        $key = $user.SamAccountName.ToLowerInvariant()
        if (-not $seen.ContainsKey($key)) {
            $seen[$key] = $true
            if (-not (Get-Attr $user 'uidNumber')) { $usersMissingUid.Add($user) }
        }
        if ($shellGroups.ContainsKey($name.ToLowerInvariant()) -and -not (Get-Attr $user 'loginShell') -and
            -not ($usersMissingShell | Where-Object { $_.SamAccountName -eq $user.SamAccountName })) {
            $usersMissingShell.Add($user)
        }
    }
}

if ($Report) {
    foreach ($g in $groupsMissing) { Write-Output "missing: group $($g.Name) has no gidNumber" }
    foreach ($u in $usersMissingUid) { Write-Output "missing: user $($u.SamAccountName) has no uidNumber" }
    foreach ($u in $usersMissingShell) { Write-Output "missing: user $($u.SamAccountName) has no loginShell" }
    $count = $groupsMissing.Count + $usersMissingUid.Count + $usersMissingShell.Count
    if ($count -eq 0) { Write-Output 'nothing missing' } else { Write-Output "$count missing" }
    return
}

$nextUid = [Math]::Max($FirstUid, (Get-HighestInUse 'uidNumber' 'user') + 1)
$nextGid = [Math]::Max($FirstGid, (Get-HighestInUse 'gidNumber' 'group') + 1)
$changes = 0
$verb = if ($Apply) { 'set' } else { 'would set' }

foreach ($g in $groupsMissing) {
    if ($nextGid -gt $High) { throw "no gidNumber left inside $Low-$High" }
    if ($Apply) { Set-ADGroup -Identity $g.DistinguishedName -Replace @{ gidNumber = $nextGid } }
    Write-Output "$verb gidNumber $nextGid on group $($g.Name)"
    $nextGid++
    $changes++
}
foreach ($u in $usersMissingUid) {
    if ($nextUid -gt $High) { throw "no uidNumber left inside $Low-$High" }
    if ($Apply) { Set-ADUser -Identity $u.DistinguishedName -Replace @{ uidNumber = $nextUid } }
    Write-Output "$verb uidNumber $nextUid on $($u.SamAccountName)"
    $nextUid++
    $changes++
}
foreach ($u in $usersMissingShell) {
    if ($Apply) { Set-ADUser -Identity $u.DistinguishedName -Replace @{ loginShell = $LoginShell } }
    Write-Output "$verb loginShell $LoginShell on $($u.SamAccountName)"
    $changes++
}

if ($Apply) {
    Write-Output "done: $changes changes"
} else {
    Write-Output "dry run: $changes changes; run again with -Apply to make them"
}
