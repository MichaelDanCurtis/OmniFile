BeforeAll {
    . (Join-Path $PSScriptRoot 'TestHelpers.ps1')
    $script:Runner = Join-Path $script:Kit 'Invoke-Matrix.ps1'
}

Describe 'Invoke-Matrix.ps1' {
    BeforeEach { Set-KitMocks }

    It 'runs the rows a settings file names, records a row that stopped as failed, and goes on' {
        $share = Join-Path $TestDrive 'share'
        New-Item -ItemType Directory -Path $share | Out-Null
        $out = Join-Path $TestDrive 'results'
        $settings = Join-Path $TestDrive 'matrix.psd1'
        Set-Content -LiteralPath $settings -Value @"
@{
    Edge = 'admin@filer'; Share = '\\filer\projects'; ShareKind = 'wait'; OutDir = '$out'; SettleSeconds = 0
    Rows = @{
        W05 = @{ File = '\\filer\projects\a.segy' }
        W12 = @{ Folder = '$share' }
    }
}
"@
        $results = & $script:Runner -Settings $settings 6>$null
        @($results | ForEach-Object { "$($_.row) $($_.verdict)" }) | Should -Be @('W5 fail', 'W12 recorded')
        (Get-Content -Raw -LiteralPath (Join-Path $out 'W5.json') | ConvertFrom-Json).notes | Should -BeLike '*failfast share*'
        @(Get-Content -LiteralPath (Join-Path $out 'results.md') | Where-Object { $_ -like '| W*' }).Count | Should -Be 2
    }

    It 'runs only the rows asked for' {
        $share = Join-Path $TestDrive 'share2'
        New-Item -ItemType Directory -Path $share | Out-Null
        $settings = Join-Path $TestDrive 'matrix2.psd1'
        Set-Content -LiteralPath $settings -Value @"
@{
    Edge = 'admin@filer'; Share = '\\filer\projects'; ShareKind = 'wait'; OutDir = '$(Join-Path $TestDrive 'results2')'
    SettleSeconds = 0
    Rows = @{ W05 = @{ File = 'a' }; W12 = @{ Folder = '$share' } }
}
"@
        $results = & $script:Runner -Settings $settings -Rows W12 6>$null
        @($results).Count | Should -Be 1
        $results[0].row | Should -Be 'W12'
    }

    It 'the example settings file loads and names every row' {
        $s = Import-PowerShellDataFile -Path (Join-Path $script:Kit 'matrix.example.psd1')
        @($s.Rows.Keys | Sort-Object) | Should -Be @(1..14 | Where-Object { $_ -ne 5 } | ForEach-Object { 'W{0:D2}' -f $_ })
    }
}
