# Invoke-Tests.ps1 (what run-tests.sh runs) must fail when a test file fails to load, not only when a
# test fails: such a file runs no test, so its failures are never counted.

BeforeAll {
    $script:Runner = Join-Path $PSScriptRoot 'Invoke-Tests.ps1'
    $script:Pwsh = (Get-Process -Id $PID).Path
}

Describe 'Invoke-Tests.ps1' {
    It 'exits 1 for a test file that throws while it is discovered' {
        $out = & $script:Pwsh -NoProfile -NonInteractive -File $script:Runner -Path (Join-Path $PSScriptRoot 'fixtures') 2>&1
        $LASTEXITCODE | Should -Be 1
        ($out -join "`n") | Should -BeLike '*1 test files failed to run; result Failed*'
    }

    It 'leaves the fixtures out of the normal run' {
        $config = Get-Content -Raw -LiteralPath $script:Runner
        $config | Should -BeLike "*ExcludePath = @(Join-Path `$PSScriptRoot 'fixtures' '*')*"
    }
}
