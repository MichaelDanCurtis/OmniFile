BeforeAll {
    . (Join-Path $PSScriptRoot 'TestHelpers.ps1')
}

Describe 'New-MatrixContext' {
    BeforeEach { Set-KitMocks }

    It 'takes the settings, with defaults, and makes the results folder' {
        $ctx = New-TestContext
        $ctx.Share | Should -Be '\\filer\projects'
        $ctx.Auth | Should -Be 'kerberos'
        $ctx.CountCommand | Should -Be '~/omnifile-recall-count.sh'
        $ctx.Client.Caption | Should -Be 'Microsoft Windows 11 Enterprise'
        Test-Path -LiteralPath $ctx.OutDir | Should -BeTrue
    }

    It 'refuses settings without the share, or an unknown share kind' {
        { New-MatrixContext -Settings @{ Edge = 'e'; ShareKind = 'wait'; OutDir = (Join-Path $TestDrive 'x') } } |
            Should -Throw '*need Share*'
        { New-MatrixContext -Settings @{ Edge = 'e'; Share = '\\f\s'; ShareKind = 'slow'; OutDir = (Join-Path $TestDrive 'x') } } |
            Should -Throw '*wait, failfast or quick*'
    }
}

Describe 'Get-EdgeRecallCount' {
    BeforeEach { Set-KitMocks }

    It 'asks the filer for the window in seconds since 1970, and reads its answer' {
        Set-RecallCounts @(2)
        $ctx = New-TestContext
        $since = [DateTime]::new(2026, 11, 3, 10, 0, 0, [DateTimeKind]::Utc)
        $c = Get-EdgeRecallCount -Context $ctx -Since $since -Until $since.AddMinutes(1)
        $global:OmniTestEdgeCommands[0] | Should -Be '~/omnifile-recall-count.sh --since 1793700000 --until 1793700060'
        $c.recalls | Should -Be 2
        $c.recall_ids | Should -Be @(101, 102)
    }

    It 'gives $null, with a warning, when the filer cannot be asked' {
        Set-RecallCounts @($null)
        $ctx = New-TestContext
        $c = Get-EdgeRecallCount -Context $ctx -Since ([DateTime]::UtcNow) -WarningVariable w -WarningAction SilentlyContinue
        $c | Should -BeNullOrEmpty
        "$w" | Should -BeLike '*could not count*'
    }
}

Describe 'Resolve-Verdict' {
    It 'fails on any false, records on any unmeasured, passes otherwise' {
        Resolve-Verdict @($true, $true) | Should -Be 'pass'
        Resolve-Verdict @($true, $false, $null) | Should -Be 'fail'
        Resolve-Verdict @($true, $null) | Should -Be 'recorded'
        Resolve-Verdict @() | Should -Be 'pass'
    }
}

Describe 'ConvertFrom-RobocopySummary' {
    It 'reads the Files line of robocopy''s summary' {
        $out = @(
            '               Total    Copied   Skipped  Mismatch    FAILED    Extras',
            '    Dirs :         3         3         0         0         0         0',
            '   Files :        50         0        50         0         0         0',
            '   Bytes :   1.200 g         0   1.200 g         0         0         0')
        $s = ConvertFrom-RobocopySummary -Output $out
        $s.Total | Should -Be 50
        $s.Skipped | Should -Be 50
        $s.Copied | Should -Be 0
        ConvertFrom-RobocopySummary -Output @('no summary') | Should -BeNullOrEmpty
    }
}

Describe 'Read-FileTimed' {
    It 'hashes a file it can read' {
        $f = Join-Path $TestDrive 'a.txt'
        [IO.File]::WriteAllText($f, 'abc')
        $r = Read-FileTimed -Path $f
        $r.Ok | Should -BeTrue
        $r.Sha256 | Should -Be 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    }

    It 'reports the error of one it cannot' {
        $r = Read-FileTimed -Path (Join-Path $TestDrive 'missing.bin')
        $r.Ok | Should -BeFalse
        $r.Error | Should -Not -BeNullOrEmpty
        $r.HResult | Should -Match '^0x[0-9A-F]{8}$'
    }
}

Describe 'Read-Observation' {
    It 'records "not observed" in a run nobody watches' {
        $ctx = [pscustomobject]@{ NonInteractive = $true }
        Read-Observation -Context $ctx -Question 'anything?' | Should -Be 'not observed'
    }
}

Describe 'Write-MatrixResult' {
    BeforeEach { Set-KitMocks }

    It 'writes the row as JSON and as a line of results.md in the matrix''s columns' {
        $ctx = New-TestContext
        $r = Write-MatrixResult -Context $ctx -Row 'W1' -Action 'do it' -Expected 'it works' -Verdict 'pass' `
            -Measures @{ recalls_started = 0; list_seconds = 1.5 } -Observations @{ overlay_shown = 'y' } -Notes 'a|b'
        $r.verdict | Should -Be 'pass'
        $j = Get-RowResult $ctx 'W1'
        $j.row | Should -Be 'W1'
        $j.client.Build | Should -Be '26200'
        $j.share_kind | Should -Be 'wait'
        $j.measures.recalls_started | Should -Be 0
        $md = Get-Content -LiteralPath (Join-Path $ctx.OutDir 'results.md')
        $md[0] | Should -Be '# Windows client matrix: Microsoft Windows 11 Enterprise 10.0.26200 on TEST01'
        $md[2] | Should -Be '| # | Share | Verdict | Record | Notes |'
        $md[-1] | Should -Be '| W1 | wait | pass | list_seconds=1.5; recalls_started=0; overlay_shown=y | a/b |'
    }

    It 'shows a count it could not take as n/a' {
        $ctx = New-TestContext
        [void](Write-MatrixResult -Context $ctx -Row 'W2' -Action 'a' -Expected 'e' -Verdict 'recorded' -Measures @{ recalls_started = $null })
        (Get-Content -LiteralPath (Join-Path $ctx.OutDir 'results.md'))[-1] | Should -BeLike '*recalls_started=n/a*'
    }
}
