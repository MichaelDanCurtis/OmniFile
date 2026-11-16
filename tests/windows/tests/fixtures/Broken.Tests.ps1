# A test file that fails while Pester discovers it, on purpose: Runner.Tests.ps1 checks that
# Invoke-Tests.ps1 exits non-zero for it, though no test inside it ever failed (none ran).
Describe 'a file that cannot be loaded' {
    throw 'this file fails at discovery, on purpose'
    It 'never runs' { $true | Should -BeTrue }
}
