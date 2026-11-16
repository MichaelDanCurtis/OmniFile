# The edge-side recall counter (edge/omnifile-recall-count.sh), run with bash against a fake journalctl
# (and, for the root case, a fake id).

BeforeAll {
    $script:Counter = Join-Path (Split-Path -Parent $PSScriptRoot) 'edge/omnifile-recall-count.sh'
    $script:Bin = Join-Path $TestDrive 'bin'
    New-Item -ItemType Directory -Path $script:Bin | Out-Null
    $script:ArgsLog = Join-Path $TestDrive 'journalctl.args'
    $script:Journal = Join-Path $TestDrive 'journal.txt'   # what a window shows
    $script:Probe = Join-Path $TestDrive 'probe.txt'       # what the unit's last entry shows (--lines 1)
    $fake = Join-Path $script:Bin 'journalctl'
    Set-Content -LiteralPath $fake -Value @(
        '#!/usr/bin/env bash',
        "printf '%s\n' `"`$*`" >> '$script:ArgsLog'",
        'if [ -n "${FAKE_JOURNAL_FAIL:-}" ]; then echo "Failed to open the journal" >&2; exit 1; fi',
        "case `" `$* `" in *' --lines '*) cat '$script:Probe' ;; *) cat '$script:Journal' ;; esac")
    & chmod +x $fake
    $script:RootBin = Join-Path $TestDrive 'rootbin'
    New-Item -ItemType Directory -Path $script:RootBin | Out-Null
    $fakeId = Join-Path $script:RootBin 'id'
    Set-Content -LiteralPath $fakeId -Value @('#!/usr/bin/env bash', 'case "$1" in -u) echo 0 ;; -nG) echo root ;; -un) echo root ;; esac')
    & chmod +x $fakeId
    function Invoke-Counter([string[]] $CounterArgs, [switch] $AsRoot) {
        $prefix = if ($AsRoot) { "$($script:RootBin):$($script:Bin)" } else { $script:Bin }
        $env:PATH = "$($prefix):$($env:PATH_BEFORE)"
        $out = & bash $script:Counter @CounterArgs 2>&1
        return [pscustomobject]@{ Code = $LASTEXITCODE; Out = ($out -join "`n") }
    }
    $env:PATH_BEFORE = $env:PATH
}

AfterAll {
    $env:PATH = $env:PATH_BEFORE
    Remove-Item Env:PATH_BEFORE -ErrorAction SilentlyContinue
}

Describe 'omnifile-recall-count.sh' {
    BeforeEach {
        Remove-Item -LiteralPath $script:ArgsLog -ErrorAction SilentlyContinue
        Remove-Item Env:FAKE_JOURNAL_FAIL -ErrorAction SilentlyContinue
        Set-Content -LiteralPath $script:Probe -Value '{"message":"startup checks passed"}'
        Set-Content -LiteralPath $script:Journal -Value @(
            '{"timestamp":"2026-11-03T10:00:01Z","level":"INFO","message":"startup checks passed"}',
            '{"timestamp":"2026-11-03T10:00:02Z","level":"INFO","message":"recall started","file_id":12,"version_id":3,"recall_id":45,"state":"requested"}',
            '{"timestamp":"2026-11-03T10:00:03Z","level":"INFO","message":"recall started","file_id":12,"version_id":3,"recall_id":45,"state":"rehydrating"}',
            '{"timestamp":"2026-11-03T10:00:04Z","level":"INFO","message":"recall started","file_id":13,"version_id":1,"recall_id":46,"state":"requested"}',
            '{"timestamp":"2026-11-03T10:00:05Z","level":"WARN","message":"could not write recall note","recall_id":99}')
    }

    It 'counts the recall events, the distinct recalls and the files, as one line of JSON' {
        $r = Invoke-Counter @('--since', '1793700000', '--until', '1793700060')
        $r.Code | Should -Be 0
        $j = $r.Out | ConvertFrom-Json
        $j.requests | Should -Be 3
        $j.recalls | Should -Be 2
        $j.recall_ids | Should -Be @(45, 46)
        $j.file_ids | Should -Be @(12, 13)
        $j.since | Should -Be 1793700000
        $j.until | Should -Be 1793700060
        (Get-Content -LiteralPath $script:ArgsLog)[-1] | Should -Be '--unit omnifile-edge --since @1793700000 --output cat --no-pager --quiet --until @1793700060'
    }

    It 'gives zero, not an error, for a quiet window of a journal it can read' {
        Set-Content -LiteralPath $script:Journal -Value '{"message":"startup checks passed"}'
        $j = (Invoke-Counter @('--since', '1')).Out | ConvertFrom-Json
        $j.recalls | Should -Be 0
        @($j.recall_ids).Count | Should -Be 0
        $j.until | Should -BeNullOrEmpty
    }

    It 'refuses to count for a user who sees none of the unit''s entries' {
        Set-Content -LiteralPath $script:Probe -Value ''
        Set-Content -LiteralPath $script:Journal -Value ''
        $r = Invoke-Counter @('--since', '1')
        $r.Code | Should -Be 1
        $r.Out | Should -BeLike '*sees no entries of omnifile-edge*systemd-journal*'
        $r.Out | Should -Not -BeLike '*"recalls"*'
    }

    It 'counts for root without asking whether entries are visible' {
        Set-Content -LiteralPath $script:Probe -Value ''
        Set-Content -LiteralPath $script:Journal -Value ''
        $r = Invoke-Counter @('--since', '1') -AsRoot
        $r.Code | Should -Be 0
        ($r.Out | ConvertFrom-Json).recalls | Should -Be 0
        @(Get-Content -LiteralPath $script:ArgsLog | Where-Object { $_ -like '*--lines*' }).Count | Should -Be 0
    }

    It 'refuses a time that is not seconds since 1970, or a strange unit' {
        (Invoke-Counter @('--since', 'yesterday')).Code | Should -Be 2
        (Invoke-Counter @('--since', '1', '--unit', 'x;rm')).Code | Should -Be 2
        (Invoke-Counter @('--frobnicate')).Code | Should -Be 2
    }

    It 'fails, saying why, when the journal cannot be read' {
        $env:FAKE_JOURNAL_FAIL = '1'
        $r = Invoke-Counter @('--since', '1') -AsRoot
        $r.Code | Should -Be 1
        $r.Out | Should -BeLike "*can't read the journal*"
    }
}
