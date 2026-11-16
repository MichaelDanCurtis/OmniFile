# Shared by the kit's Pester tests: a context in TestDrive, and the mocks that stand in for Windows,
# ssh and the tester. Dot-source it in BeforeAll, then call Set-KitMocks in BeforeEach.

$script:Kit = Split-Path -Parent $PSScriptRoot
$script:Rows = Join-Path $script:Kit 'rows'
Import-Module (Join-Path $script:Kit 'OmniFileMatrix.psm1') -Force

function New-TestContext {
    param([string] $ShareKind = 'wait', [string] $Role = 'Writer', [string] $Name = 'out')
    $out = Join-Path $TestDrive $Name
    return New-MatrixContext -Settings @{
        Edge = 'admin@filer'; Share = '\\filer\projects'; ShareKind = $ShareKind; Role = $Role
        OutDir = $out; SettleSeconds = 0; NonInteractive = $false
    }
}

function Get-RowResult {
    param($Context, [string] $Row)
    return (Get-Content -Raw -LiteralPath (Join-Path $Context.OutDir "$Row.json") | ConvertFrom-Json)
}

function Set-RecallCounts {
    <# The recall counts the fake filer answers, one per count asked for, in order (the last repeats).
       $null: the count fails (ssh refused). #>
    param([object[]] $Counts)
    $global:OmniTestCounts = [System.Collections.Generic.List[object]]::new()
    foreach ($c in $Counts) { $global:OmniTestCounts.Add($c) }
    $global:OmniTestEdgeCommands = [System.Collections.Generic.List[string]]::new()
}

function Set-KitMocks {
    Set-RecallCounts @(0)
    Mock -ModuleName OmniFileMatrix Get-ClientInfo {
        [pscustomobject]@{ Caption = 'Microsoft Windows 11 Enterprise'; Version = '10.0.26200'; Build = '26200'; Machine = 'TEST01' }
    }
    Mock -ModuleName OmniFileMatrix Start-Pause {}
    Mock Start-Pause {}
    $edge = {
        $global:OmniTestEdgeCommands.Add($Command)
        if ($Command -notlike '*omnifile-recall-count*') { return '' }
        $n = $global:OmniTestCounts[0]
        if ($global:OmniTestCounts.Count -gt 1) { $global:OmniTestCounts.RemoveAt(0) }
        if ($null -eq $n) { throw 'ssh: Permission denied (publickey)' }
        $ids = @(1..([int]$n) | Where-Object { $n -gt 0 } | ForEach-Object { 100 + $_ })
        return ('{{"since":1,"until":2,"requests":{0},"recalls":{0},"recall_ids":[{1}],"file_ids":[{1}]}}' -f $n, ($ids -join ','))
    }
    # The module asks for counts; W14 runs its restart command itself.
    Mock -ModuleName OmniFileMatrix Invoke-EdgeCommand $edge
    Mock Invoke-EdgeCommand $edge
    Mock Read-Observation { 'y' }
    Mock Wait-Tester {}
    Mock Open-ExplorerFolder { $true }
    Mock Select-InExplorer { $true }
    Mock Show-Properties {}
    Mock Start-App { [pscustomobject]@{ Id = 4242 } }
}

function New-FileState {
    param([string] $Name, [bool] $Offline = $true, [long] $Length = 1048576)
    return [pscustomobject]@{ Name = $Name; Length = $Length; LastWriteTimeUtc = [DateTime]::UtcNow
        Attributes = $(if ($Offline) { 'Archive, Offline' } else { 'Archive' }); Offline = $Offline }
}
