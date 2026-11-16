# Runs the kit's Pester tests (tests\windows\run-tests.sh calls this). Exits 0 only when Pester's own
# verdict is Passed: a test that fails, and a test file that fails to load (a container that never ran),
# both make it exit 1.
param(
    [string] $PesterVersion = '5.7.1',
    # The tests to run: by default this folder, without fixtures\ (files that fail on purpose).
    [string] $Path = $PSScriptRoot
)
$ErrorActionPreference = 'Stop'
Import-Module Pester -RequiredVersion $PesterVersion
$config = New-PesterConfiguration
$config.Run.Path = $Path
if ($Path -eq $PSScriptRoot) { $config.Run.ExcludePath = @(Join-Path $PSScriptRoot 'fixtures' '*') }
$config.Run.PassThru = $true
$config.Output.Verbosity = 'Normal'
$result = Invoke-Pester -Configuration $config
$broken = @($result.Containers | Where-Object { $_.Result -eq 'Failed' -and $_.FailedCount -eq 0 }).Count
Write-Host ("pester {0}: {1} passed, {2} failed, {3} skipped, {4} total; {5} test files failed to run; result {6}" -f
    $PesterVersion, $result.PassedCount, $result.FailedCount, $result.SkippedCount, $result.TotalCount, $broken,
    $result.Result)
if ($result.Result -ne 'Passed') { exit 1 }
exit 0
