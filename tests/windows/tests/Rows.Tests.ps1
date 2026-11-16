# One block per row of the matrix: each runner is run against mocks of Windows, ssh and the tester, and
# its verdict and record are checked.

BeforeAll {
    . (Join-Path $PSScriptRoot 'TestHelpers.ps1')
    function Invoke-Row([string] $Row, $Context, [hashtable] $With) {
        $script = Join-Path $script:Rows "$Row.ps1"
        [void](& $script -Context $Context @With)
        return Get-RowResult $Context ('W' + [int]($Row -replace '^W', ''))
    }
}

Describe 'every row of the matrix has a runner' {
    It 'has W01 to W14' {
        $names = @(Get-ChildItem -LiteralPath $script:Rows -Filter 'W*.ps1' | ForEach-Object { $_.BaseName } | Sort-Object)
        $names | Should -Be @(1..14 | ForEach-Object { 'W{0:D2}' -f $_ })
    }
}

Describe 'W1: a folder of stubs in Explorer' {
    BeforeEach {
        Set-KitMocks
        Mock Get-FolderStates { [pscustomobject]@{ Files = @((New-FileState 'a'), (New-FileState 'b'), (New-FileState 'c')); Seconds = 0.4 } }
    }

    It 'passes when every file is offline and nothing is recalled' {
        $r = Invoke-Row 'W01' (New-TestContext) @{ Folder = '\\filer\projects\stubs' }
        $r.verdict | Should -Be 'pass'
        $r.measures.files | Should -Be 3
        $r.measures.recalls_started | Should -Be 0
        Should -Invoke Open-ExplorerFolder -Times 2
    }

    It 'fails when Explorer started a recall' {
        Set-RecallCounts @(2)
        (Invoke-Row 'W01' (New-TestContext) @{ Folder = 'x' }).verdict | Should -Be 'fail'
    }

    It 'fails when a stub is not marked offline' {
        Mock Get-FolderStates { [pscustomobject]@{ Files = @((New-FileState 'a'), (New-FileState 'b' -Offline $false)); Seconds = 0.4 } }
        (Invoke-Row 'W01' (New-TestContext) @{ Folder = 'x' }).verdict | Should -Be 'fail'
    }

    It 'is only recorded when the recalls could not be counted' {
        Set-RecallCounts @($null)
        $r = Invoke-Row 'W01' (New-TestContext) @{ Folder = 'x' } 3>$null
        $r.verdict | Should -Be 'recorded'
        $r.measures.recalls_started | Should -BeNullOrEmpty
    }
}

Describe 'W2: the preview pane' {
    BeforeEach { Set-KitMocks }

    It 'passes with no preview and no recall' {
        Mock Read-Observation { 'n' }
        (Invoke-Row 'W02' (New-TestContext) @{ File = '\\filer\projects\r.pdf' }).verdict | Should -Be 'pass'
        Should -Invoke Select-InExplorer -Times 1 -ParameterFilter { $Path -eq '\\filer\projects\r.pdf' }
    }

    It 'fails when the pane previewed the stub' {
        Mock Read-Observation { 'y' }
        (Invoke-Row 'W02' (New-TestContext) @{ File = '\\filer\projects\r.pdf' }).verdict | Should -Be 'fail'
    }
}

Describe 'W3: Properties' {
    BeforeEach { Set-KitMocks }

    It 'passes when the stub shows as Offline and nothing is recalled' {
        Mock Get-ItemState { New-FileState 'line.segy' }
        $r = Invoke-Row 'W03' (New-TestContext) @{ File = 'line.segy' }
        $r.verdict | Should -Be 'pass'
        $r.measures.attributes | Should -Be 'Archive, Offline'
    }

    It 'fails when the file lacks the Offline attribute' {
        Mock Get-ItemState { New-FileState 'line.segy' -Offline $false }
        (Invoke-Row 'W03' (New-TestContext) @{ File = 'line.segy' }).verdict | Should -Be 'fail'
    }
}

Describe 'W4: opens that wait' {
    BeforeEach {
        Set-KitMocks
        Mock Read-FileTimed { [pscustomobject]@{ Ok = $true; Seconds = 95.2; Sha256 = 'aa'; Error = $null; HResult = $null } }
    }

    It 'passes when both opens wait and then read the right bytes' {
        Set-RecallCounts @(1)
        $r = Invoke-Row 'W04' (New-TestContext) @{ SmallFile = 's.txt'; LargeFile = 'l.segy'; SmallSha256 = 'AA'; LargeSha256 = 'aa' }
        $r.verdict | Should -Be 'pass'
        $r.measures.large_wait_seconds | Should -Be 95.2
        $r.measures.recalls_started | Should -Be @(1, 1)
        Should -Invoke Start-App -Times 1 -ParameterFilter { $Path -eq 'notepad.exe' -and $Argument -eq 's.txt' }
    }

    It 'fails when the bytes are not the ones written' {
        $r = Invoke-Row 'W04' (New-TestContext) @{ SmallFile = 's.txt'; LargeFile = 'l.segy'; LargeSha256 = 'bb' }
        $r.verdict | Should -Be 'fail'
    }

    It 'opens the large stub in the client''s application when one is given' {
        [void](Invoke-Row 'W04' (New-TestContext) @{ SmallFile = 's'; LargeFile = 'l'; AppPath = 'C:\seis\seis.exe' })
        Should -Invoke Start-App -Times 1 -ParameterFilter { $Path -eq 'C:\seis\seis.exe' -and $Argument -eq 'l' }
    }

    It 'refuses a share that does not wait' {
        { Invoke-Row 'W04' (New-TestContext -ShareKind failfast) @{ SmallFile = 's'; LargeFile = 'l' } } | Should -Throw '*wait share*'
    }
}

Describe 'W5: opens that fail fast' {
    BeforeEach {
        Set-KitMocks
        Set-RecallCounts @(1)
        Mock Read-FileTimed { [pscustomobject]@{ Ok = $false; Seconds = 0.3; Sha256 = $null; Error = 'The request is not supported.'; HResult = '0x80070032' } }
        Mock Test-ItemExists { $true } -ParameterFilter { $Path -eq '\\filer\projects\a.segy.RECALLING.txt' }
    }

    It 'passes with an error at once, the note, a recall and a Teams card' {
        $r = Invoke-Row 'W05' (New-TestContext -ShareKind failfast) @{ File = '\\filer\projects\a.segy' }
        $r.verdict | Should -Be 'pass'
        $r.measures.hresult | Should -Be '0x80070032'
        $r.measures.recalling_note | Should -BeTrue
    }

    It 'fails when the open succeeded' {
        Mock Read-FileTimed { [pscustomobject]@{ Ok = $true; Seconds = 0.1; Sha256 = 'aa'; Error = $null; HResult = $null } }
        (Invoke-Row 'W05' (New-TestContext -ShareKind failfast) @{ File = '\\filer\projects\a.segy' }).verdict | Should -Be 'fail'
    }

    It 'fails when no note appeared' {
        Mock Test-ItemExists { $false } -ParameterFilter { $Path -eq '\\filer\projects\a.segy.RECALLING.txt' }
        (Invoke-Row 'W05' (New-TestContext -ShareKind failfast) @{ File = '\\filer\projects\a.segy' }).verdict | Should -Be 'fail'
    }

    It 'refuses a wait share' {
        { Invoke-Row 'W05' (New-TestContext) @{ File = 'a' } } | Should -Throw '*failfast share*'
    }
}

Describe 'W6: copies' {
    BeforeEach {
        Set-KitMocks
        Mock Get-FolderStates { [pscustomobject]@{ Files = @(1..5 | ForEach-Object { New-FileState "f$_" }); Seconds = 0.1 } }
        Mock Copy-WithExplorer {}
        Mock Invoke-Robocopy {
            if ($Arguments -contains '/XA:O') {
                [pscustomobject]@{ ExitCode = 0; Output = @('   Files :         5         0         5         0         0         0') }
            } else {
                [pscustomobject]@{ ExitCode = 1; Output = @('   Files :         5         5         0         0         0         0') }
            }
        }
    }

    It 'passes when /XA:O skips every offline file and recalls nothing' {
        Set-RecallCounts @(0, 5, 5)   # /XA:O first, then Explorer, then plain robocopy
        $r = Invoke-Row 'W06' (New-TestContext) @{ ExplorerSource = 'e'; RobocopySource = 'r'; XaoSource = 'x'; Destination = (Join-Path $TestDrive 'copies') }
        $r.verdict | Should -Be 'pass'
        $r.measures.xao_skipped | Should -Be 5
        $r.measures.explorer_recalls | Should -Be 5
        $r.measures.robocopy_copied | Should -Be 5
        Should -Invoke Invoke-Robocopy -Times 1 -ParameterFilter { ($Arguments -join ' ') -like 'x *xao /E /XA:O*' }
    }

    It 'fails when /XA:O recalled' {
        Set-RecallCounts @(3, 5, 5)
        $r = Invoke-Row 'W06' (New-TestContext) @{ ExplorerSource = 'e'; RobocopySource = 'r'; XaoSource = 'x'; Destination = (Join-Path $TestDrive 'copies') }
        $r.verdict | Should -Be 'fail'
    }
}

Describe 'W7: Defender' {
    BeforeEach {
        Set-KitMocks
        Mock Get-FolderStates { [pscustomobject]@{ Files = @(New-FileState 'a'); Seconds = 0.1 } }
        Mock Invoke-DefenderScan {}
    }

    It 'passes when the quick scan recalls nothing, and records the custom scan' {
        Set-RecallCounts @(0, 4)
        $r = Invoke-Row 'W07' (New-TestContext) @{ Path = 'Z:\' }
        $r.verdict | Should -Be 'pass'
        $r.measures.custom_recalls | Should -Be 4
        $r.measures.custom_skipped_offline | Should -BeFalse
        Should -Invoke Invoke-DefenderScan -Times 1 -ParameterFilter { $ScanType -eq 'CustomScan' -and $Path -eq 'Z:\' }
    }

    It 'fails when the quick scan recalled' {
        Set-RecallCounts @(1, 0)
        (Invoke-Row 'W07' (New-TestContext) @{ Path = 'Z:\' }).verdict | Should -Be 'fail'
    }
}

Describe 'W8: Windows Search' {
    BeforeEach {
        Set-KitMocks
        Mock Get-FolderStates { [pscustomobject]@{ Files = @((New-FileState 'stub.segy'), (New-FileState 'filled.txt' -Offline $false)); Seconds = 0.1 } }
    }

    It 'passes when only files that are online are indexed' {
        Mock Get-IndexedItems { @('Z:\filled.txt') }
        $r = Invoke-Row 'W08' (New-TestContext) @{ Path = 'Z:\'; Minutes = 1 }
        $r.verdict | Should -Be 'pass'
        $r.measures.indexed | Should -Be 1
        Should -Invoke Start-Pause -ParameterFilter { $Seconds -eq 60 }
    }

    It 'fails when an offline file was indexed' {
        Mock Get-IndexedItems { @('Z:\filled.txt', 'Z:\stub.segy') }
        $r = Invoke-Row 'W08' (New-TestContext) @{ Path = 'Z:\'; Minutes = 1 }
        $r.verdict | Should -Be 'fail'
        $r.measures.indexed_offline | Should -Be 1
    }
}

Describe 'W9: Always available offline' {
    BeforeEach { Set-KitMocks }

    It 'passes when Offline Files caches nothing' {
        Mock Get-OfflineFilesState { [pscustomobject]@{ Enabled = $true; ItemsCached = 0 } }
        (Invoke-Row 'W09' (New-TestContext) @{ Folder = 'Z:\offline' }).verdict | Should -Be 'pass'
    }

    It 'fails when it cached the folder' {
        Mock Get-OfflineFilesState { [pscustomobject]@{ Enabled = $true; ItemsCached = 3 } }
        (Invoke-Row 'W09' (New-TestContext) @{ Folder = 'Z:\offline' }).verdict | Should -Be 'fail'
    }
}

Describe 'W10: Previous Versions' {
    BeforeEach {
        Set-KitMocks
        Mock Read-FileTimed { [pscustomobject]@{ Ok = $true; Seconds = 3.1; Sha256 = 'cc'; Error = $null; HResult = $null } }
    }

    It 'reads the file through its @GMT label and checks the bytes' {
        $r = Invoke-Row 'W10' (New-TestContext) @{ RelativePath = 'notes\a.txt'; Label = '@GMT-2026.11.01-12.00.00'; ExpectedSha256 = 'CC' }
        $r.verdict | Should -Be 'pass'
        Should -Invoke Read-FileTimed -Times 1 -ParameterFilter { $Path -like '*@GMT-2026.11.01-12.00.00*notes*a.txt' }
    }

    It 'fails when the version''s bytes differ' {
        (Invoke-Row 'W10' (New-TestContext) @{ RelativePath = 'a.txt'; Label = '@GMT-2026.11.01-12.00.00'; ExpectedSha256 = 'dd' }).verdict |
            Should -Be 'fail'
    }

    It 'refuses a label that is not an @GMT token' {
        { Invoke-Row 'W10' (New-TestContext) @{ RelativePath = 'a.txt'; Label = '2026-11-01' } } | Should -Throw
    }
}

Describe 'W11: the Security tab' {
    BeforeEach {
        Set-KitMocks
        Mock Get-PathAcl { @([pscustomobject]@{ Identity = 'CORP\Proj-Owners'; Rights = 'FullControl'; Type = 'Allow' }) }
    }

    It 'passes when an Owner may edit' {
        Mock Test-AclEdit { $true }
        (Invoke-Row 'W11' (New-TestContext -Role Owner) @{ Root = 'r'; Subfolder = 's' }).verdict | Should -Be 'pass'
    }

    It 'passes when a Reader is refused' {
        Mock Test-AclEdit { $false }
        $r = Invoke-Row 'W11' (New-TestContext -Role Reader) @{ Root = 'r'; Subfolder = 's' }
        $r.verdict | Should -Be 'pass'
        $r.measures.root_entries | Should -Be @('CORP\Proj-Owners:Allow')
    }

    It 'fails when a Writer may change the ACL' {
        Mock Test-AclEdit { $true }
        (Invoke-Row 'W11' (New-TestContext -Role Writer) @{ Root = 'r'; Subfolder = 's' }).verdict | Should -Be 'fail'
    }
}

Describe 'W12: rename, move and delete' {
    BeforeEach { Set-KitMocks }

    It 'makes each change on the share and records how long the portal took' {
        $share = Join-Path $TestDrive 'share12'
        New-Item -ItemType Directory -Path $share | Out-Null
        $r = Invoke-Row 'W12' (New-TestContext) @{ Folder = $share }
        $r.verdict | Should -Be 'recorded'
        $r.measures.on_the_share | Should -Be @('a-renamed.txt', 'b.txt', 'folder-renamed')
        Should -Invoke Wait-Tester -Times 5
        @($r.observations.PSObject.Properties.Name | Sort-Object) |
            Should -Be @('delete_seconds', 'folder_rename_seconds', 'move_seconds', 'rename_seconds')
    }
}

Describe 'W13: an Office save' {
    BeforeEach { Set-KitMocks }

    It 'saves the way Word does and passes on two versions of one file' {
        $share = Join-Path $TestDrive 'share13'
        New-Item -ItemType Directory -Path $share | Out-Null
        Mock Read-Observation { '2' } -ParameterFilter { $Question -like 'How many versions*' }
        Mock Read-Observation { 'version' } -ParameterFilter { $Question -like 'For the Word document*' }
        $r = Invoke-Row 'W13' (New-TestContext) @{ Folder = $share }
        $r.verdict | Should -Be 'pass'
        $r.measures.temp_files_left | Should -Be 0
        (Get-Content -Raw -LiteralPath (Join-Path $share 'w13-office-save.txt')).Trim() | Should -Be 'version 2, saved the way Word saves'
    }

    It 'fails when the portal shows a new file instead of a version' {
        $share = Join-Path $TestDrive 'share13b'
        New-Item -ItemType Directory -Path $share | Out-Null
        Mock Read-Observation { '1' } -ParameterFilter { $Question -like 'How many versions*' }
        Mock Read-Observation { 'new' } -ParameterFilter { $Question -like 'For the Word document*' }
        (Invoke-Row 'W13' (New-TestContext) @{ Folder = $share }).verdict | Should -Be 'fail'
    }
}

Describe 'W14: an hour in Explorer through a restart and an outage' {
    BeforeEach { Set-KitMocks }

    It 'passes when every listing worked, and runs the restart command once' {
        Mock Get-FolderStates { [pscustomobject]@{ Files = @(); Seconds = 0.2 } }
        $r = Invoke-Row 'W14' (New-TestContext) @{ Folder = 'f'; Minutes = 10; PollSeconds = 60; RestartAtMinute = 2
            OutageAtMinute = 5; RestartCommand = 'sudo -n systemctl restart omnifile-edge' }
        $r.verdict | Should -Be 'pass'
        $r.measures.polls | Should -Be 10
        @($global:OmniTestEdgeCommands | Where-Object { $_ -like '*systemctl restart*' }).Count | Should -Be 1
    }

    It 'fails, naming the poll, when a listing failed' {
        $global:OmniTestPoll = 0
        Mock Get-FolderStates {
            $global:OmniTestPoll++
            if ($global:OmniTestPoll -eq 3) { throw 'The network path was not found.' }
            [pscustomobject]@{ Files = @(); Seconds = 12.5 }
        }
        $r = Invoke-Row 'W14' (New-TestContext) @{ Folder = 'f'; Minutes = 5; PollSeconds = 60 }
        $r.verdict | Should -Be 'fail'
        $r.measures.listing_errors | Should -Be 1
        $r.measures.slow_listings | Should -Be 4
        $r.notes | Should -BeLike '*poll 2: The network path was not found.*'
    }
}
