# The customer guide's AD tool (docs/guide/tools/Set-OmniFilePosixIds.ps1, section 1.4), against a fake AD:
# the ActiveDirectory cmdlets are stand-ins here, mocked per test.

BeforeAll {
    $script:Tool = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))) 'docs/guide/tools/Set-OmniFilePosixIds.ps1'
    # Stand-ins so the commands exist to be mocked (the module isn't on this machine).
    function global:Get-ADGroup { param($Identity, $Properties, $LDAPFilter) }
    function global:Get-ADGroupMember { param($Identity, [switch] $Recursive) }
    function global:Get-ADUser { param($Identity, $Properties, $LDAPFilter) }
    function global:Set-ADGroup { param($Identity, $Replace) }
    function global:Set-ADUser { param($Identity, $Replace) }

    function New-Group($Name, $Gid) { [pscustomobject]@{ Name = $Name; DistinguishedName = "CN=$Name,DC=corp"; gidNumber = $Gid } }
    function New-User($Sam, $Uid, $Shell) {
        [pscustomobject]@{ SamAccountName = $Sam; DistinguishedName = "CN=$Sam,DC=corp"; uidNumber = $Uid; loginShell = $Shell; objectClass = 'user' }
    }
}

AfterAll {
    foreach ($f in 'Get-ADGroup', 'Get-ADGroupMember', 'Get-ADUser', 'Set-ADGroup', 'Set-ADUser') {
        Remove-Item -LiteralPath "Function:\$f" -ErrorAction SilentlyContinue
    }
}

Describe 'Set-OmniFilePosixIds.ps1' {
    BeforeEach {
        $global:OmniAdGroups = @{
            'Domain Users'    = New-Group 'Domain Users' $null
            'OmniFile-Admins' = New-Group 'OmniFile-Admins' 30004
            'Proj-Writers'    = New-Group 'Proj-Writers' $null
        }
        $global:OmniAdUsers = @{
            'alice' = New-User 'alice' 20001 $null       # an admin without a shell
            'bob'   = New-User 'bob' $null $null         # a writer without a uid
            'carol' = New-User 'carol' 20002 '/bin/bash'
        }
        $global:OmniAdMembers = @{ 'OmniFile-Admins' = @('alice'); 'Proj-Writers' = @('bob', 'carol', 'alice') }
        Mock Get-ADGroup {
            if ($LDAPFilter) { return @($global:OmniAdGroups.Values | Where-Object { $_.gidNumber }) + @([pscustomobject]@{ gidNumber = 30010 }) }
            return $global:OmniAdGroups[$Identity]
        }
        Mock Get-ADGroupMember { @($global:OmniAdMembers[$Identity] | ForEach-Object { $global:OmniAdUsers[$_] }) }
        Mock Get-ADUser {
            if ($LDAPFilter) { return @($global:OmniAdUsers.Values | Where-Object { $_.uidNumber }) + @([pscustomobject]@{ uidNumber = 2000000 }) }
            return $global:OmniAdUsers[$Identity]
        }
        Mock Set-ADGroup {}
        Mock Set-ADUser {}
    }

    It 'reports what is missing and changes nothing' {
        $out = & $script:Tool -Report -Groups 'Domain Users', 'OmniFile-Admins', 'Proj-Writers' -LoginShellGroup 'OmniFile-Admins'
        $out | Should -Contain 'missing: group Domain Users has no gidNumber'
        $out | Should -Contain 'missing: group Proj-Writers has no gidNumber'
        $out | Should -Contain 'missing: user bob has no uidNumber'
        $out | Should -Contain 'missing: user alice has no loginShell'
        $out[-1] | Should -Be '4 missing'
        Should -Invoke Set-ADGroup -Times 0
        Should -Invoke Set-ADUser -Times 0
    }

    It 'without -Apply, says what it would set, above every number in use, and sets nothing' {
        $out = & $script:Tool -Groups 'Domain Users', 'OmniFile-Admins', 'Proj-Writers' -LoginShellGroup 'OmniFile-Admins' -FirstUid 20000 -FirstGid 30000
        # gids above the highest in use (30010); uids above 20002 (2000000 is outside the range and ignored).
        $out | Should -Contain 'would set gidNumber 30011 on group Domain Users'
        $out | Should -Contain 'would set gidNumber 30012 on group Proj-Writers'
        $out | Should -Contain 'would set uidNumber 20003 on bob'
        $out | Should -Contain 'would set loginShell /bin/bash on alice'
        $out[-1] | Should -Be 'dry run: 4 changes; run again with -Apply to make them'
        Should -Invoke Set-ADUser -Times 0
    }

    It 'with -Apply, sets exactly those, and never touches a number already set' {
        $out = & $script:Tool -Groups 'Domain Users', 'OmniFile-Admins', 'Proj-Writers' -LoginShellGroup 'OmniFile-Admins' -FirstUid 25000 -FirstGid 30000 -Apply
        $out[-1] | Should -Be 'done: 4 changes'
        Should -Invoke Set-ADGroup -Times 2
        Should -Invoke Set-ADGroup -Times 0 -ParameterFilter { $Identity -eq 'CN=OmniFile-Admins,DC=corp' }
        Should -Invoke Set-ADUser -Times 1 -ParameterFilter { $Identity -eq 'CN=bob,DC=corp' -and $Replace.uidNumber -eq 25000 }
        Should -Invoke Set-ADUser -Times 1 -ParameterFilter { $Identity -eq 'CN=alice,DC=corp' -and $Replace.loginShell -eq '/bin/bash' }
        Should -Invoke Set-ADUser -Times 0 -ParameterFilter { $Identity -eq 'CN=carol,DC=corp' }
    }

    It 'refuses a first number outside the filer''s id range' {
        { & $script:Tool -Groups 'Proj-Writers' -FirstUid 500 } | Should -Throw '*10000-999999*'
    }
}
