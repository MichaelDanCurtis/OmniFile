# The customer guide's Exchange Online step (docs/guide/README.md, on=exchange-admin), run as written
# against stand-ins for the Exchange cmdlets: it must be safe to repeat, as the guide promises.

BeforeAll {
    $repo = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
    $guide = Get-Content -Raw -LiteralPath (Join-Path $repo 'docs/guide/README.md')
    $m = [regex]::Match($guide, '```powershell guide-step=\d+ on=exchange-admin[^\n]*\n(.*?)\n```', 'Singleline')
    if (-not $m.Success) { throw 'the guide has no exchange-admin step' }
    $values = @{ OMNI_WORKER_CLIENT_ID = '11111111-1111-1111-1111-111111111111'
        OMNI_WORKER_PRINCIPAL_ID = '22222222-2222-2222-2222-222222222222'; OMNI_PREFIX = 'omni'
        OMNI_NOTIFY_SENDER = 'omnifile@contoso.example' }
    $text = [regex]::Replace($m.Groups[1].Value, '<value:([A-Z0-9_]+)>', { param($x) $values[$x.Groups[1].Value] })
    $script:Step = [scriptblock]::Create($text)
    foreach ($f in 'Connect-ExchangeOnline', 'Get-ServicePrincipal', 'New-ServicePrincipal', 'Get-ManagementScope',
        'New-ManagementScope', 'Get-ManagementRoleAssignment', 'New-ManagementRoleAssignment',
        'Test-ServicePrincipalAuthorization') {
        Set-Item -Path "Function:\global:$f" -Value { param([Parameter(ValueFromRemainingArguments = $true)] $Rest) }
    }
}

AfterAll {
    foreach ($f in 'Connect-ExchangeOnline', 'Get-ServicePrincipal', 'New-ServicePrincipal', 'Get-ManagementScope',
        'New-ManagementScope', 'Get-ManagementRoleAssignment', 'New-ManagementRoleAssignment',
        'Test-ServicePrincipalAuthorization') {
        Remove-Item -LiteralPath "Function:\$f" -ErrorAction SilentlyContinue
    }
}

Describe 'the Exchange Online step for the fallback mailbox' {
    BeforeEach {
        Mock Connect-ExchangeOnline {}
        Mock New-ServicePrincipal {}
        Mock New-ManagementScope {}
        Mock New-ManagementRoleAssignment {}
        Mock Test-ServicePrincipalAuthorization { [pscustomobject]@{ RoleName = 'Application Mail.Send'; InScope = $true } }
    }

    It 'makes the service principal, the scope and the assignment the first time' {
        Mock Get-ServicePrincipal { $null }
        Mock Get-ManagementScope { $null }
        Mock Get-ManagementRoleAssignment { $null }
        [void](& $script:Step)
        Should -Invoke New-ServicePrincipal -Times 1 -Exactly
        Should -Invoke New-ManagementScope -Times 1 -Exactly -ParameterFilter { "$Rest" -like '*OmniFile omni sender*' }
        Should -Invoke New-ManagementRoleAssignment -Times 1 -Exactly -ParameterFilter { "$Rest" -like '*Application Mail.Send*' }
    }

    It 'makes nothing again on a rerun' {
        Mock Get-ServicePrincipal { [pscustomobject]@{ AppId = '11111111-1111-1111-1111-111111111111' } }
        Mock Get-ManagementScope { [pscustomobject]@{ Name = 'OmniFile omni sender' } }
        Mock Get-ManagementRoleAssignment { [pscustomobject]@{ Role = 'Application Mail.Send' } }
        [void](& $script:Step)
        Should -Invoke New-ServicePrincipal -Times 0 -Exactly
        Should -Invoke New-ManagementScope -Times 0 -Exactly
        Should -Invoke New-ManagementRoleAssignment -Times 0 -Exactly
        Should -Invoke Test-ServicePrincipalAuthorization -Times 1 -Exactly
    }
}
