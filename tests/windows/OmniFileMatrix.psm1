# The Windows client matrix kit (docs/pilots/windows-client-matrix.md): what every row runner shares.
#
# Each row of the matrix (W1 to W14) has a runner in rows\. A runner does the row's action where it can
# be automated, asks the tester what only a person can see, counts the recalls the edge filer started
# meanwhile (edge\omnifile-recall-count.sh, over ssh), and writes one result: <OutDir>\Wnn.json, and a
# line of <OutDir>\results.md in the matrix's own columns.
#
# Everything that touches Windows, the network or the tester goes through a small function here, so the
# runners' logic can be tested anywhere with Pester mocks (tests\). Works in Windows PowerShell 5.1 and
# PowerShell 7.

Set-StrictMode -Version 2.0

function New-MatrixContext {
    <# The settings every runner reads: from a .psd1 file (matrix.example.psd1) or a hashtable. #>
    param(
        [Parameter(Mandatory = $true)] [hashtable] $Settings
    )
    foreach ($k in @('Edge', 'Share', 'ShareKind', 'OutDir')) {
        if (-not $Settings.ContainsKey($k) -or -not $Settings[$k]) { throw "the matrix settings need $k" }
    }
    if (@('wait', 'failfast', 'quick') -notcontains $Settings.ShareKind) {
        throw "ShareKind is wait, failfast or quick (the share's open behaviour)"
    }
    $ctx = [pscustomobject]@{
        Edge           = [string]$Settings.Edge                     # user@filer, for ssh
        Share          = [string]$Settings.Share.TrimEnd('\')       # \\filer\share
        ShareKind      = [string]$Settings.ShareKind
        Encrypted      = [bool]($Settings.ContainsKey('Encrypted') -and $Settings.Encrypted)
        Auth           = if ($Settings.ContainsKey('Auth')) { [string]$Settings.Auth } else { 'kerberos' }
        Role           = if ($Settings.ContainsKey('Role')) { [string]$Settings.Role } else { 'Writer' }
        OutDir         = [string]$Settings.OutDir
        CountCommand   = if ($Settings.ContainsKey('CountCommand')) { [string]$Settings.CountCommand } else { '~/omnifile-recall-count.sh' }
        SettleSeconds  = if ($Settings.ContainsKey('SettleSeconds')) { [int]$Settings.SettleSeconds } else { 20 }
        NonInteractive = [bool]($Settings.ContainsKey('NonInteractive') -and $Settings.NonInteractive)
        Client         = $null
    }
    if (-not (Test-Path -LiteralPath $ctx.OutDir)) { New-Item -ItemType Directory -Path $ctx.OutDir | Out-Null }
    $ctx.Client = Get-ClientInfo
    return $ctx
}

# ---- the outside world (each one mocked in the tests) -------------------------------------------------

function Get-ClientInfo {
    <# This client: Windows edition, version and build, and the machine's name. #>
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    return [pscustomobject]@{ Caption = $os.Caption; Version = $os.Version; Build = $os.BuildNumber; Machine = $env:COMPUTERNAME }
}

function Get-SmbSessionInfo {
    <# The SMB connection to the filer: dialect, signing and encryption, as Windows negotiated them. #>
    param([Parameter(Mandatory = $true)] [string] $Server)
    $c = @(Get-SmbConnection -ServerName $Server -ErrorAction SilentlyContinue) | Select-Object -First 1
    if (-not $c) { return $null }
    return [pscustomobject]@{ Dialect = $c.Dialect; Signed = $c.Signed; Encrypted = $c.Encrypted; User = $c.UserName }
}

function Invoke-EdgeCommand {
    <# Run one command on the filer over ssh (Windows' own OpenSSH client), and return its output. #>
    param([Parameter(Mandatory = $true)] $Context, [Parameter(Mandatory = $true)] [string] $Command)
    $out = & ssh.exe -o BatchMode=yes $Context.Edge $Command 2>&1
    if ($LASTEXITCODE -ne 0) { throw "ssh $($Context.Edge) '$Command' failed: $out" }
    return ($out -join "`n")
}

function Read-Observation {
    <# Ask the tester what only a person can see. Non-interactive runs record "not observed". #>
    param([Parameter(Mandatory = $true)] $Context, [Parameter(Mandatory = $true)] [string] $Question)
    if ($Context.NonInteractive) { return 'not observed' }
    return (Read-Host -Prompt $Question)
}

function Wait-Tester {
    <# Tell the tester what to do by hand, and wait until they say it's done. #>
    param([Parameter(Mandatory = $true)] $Context, [Parameter(Mandatory = $true)] [string] $Instruction)
    if ($Context.NonInteractive) { return }
    Read-Host -Prompt "$Instruction`nPress Enter when done" | Out-Null
}

function Get-UtcNow { return [DateTime]::UtcNow }

function Start-Pause { param([int] $Seconds) if ($Seconds -gt 0) { Start-Sleep -Seconds $Seconds } }

function Get-ItemState {
    <# One file as Windows sees it: size, times and attributes (Offline among them). #>
    param([Parameter(Mandatory = $true)] [string] $Path)
    $i = Get-Item -LiteralPath $Path -Force
    return [pscustomobject]@{
        Name = $i.Name; Length = $i.Length; LastWriteTimeUtc = $i.LastWriteTimeUtc
        Attributes = [string]$i.Attributes
        Offline = (($i.Attributes -band [IO.FileAttributes]::Offline) -ne 0)
    }
}

function Get-FolderStates {
    <# Every file in a folder (with -Recurse, below it too), as Get-ItemState sees it, and how long the
       listing took. #>
    param([Parameter(Mandatory = $true)] [string] $Path, [switch] $Recurse)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $files = @(Get-ChildItem -LiteralPath $Path -File -Force -Recurse:$Recurse | ForEach-Object { Get-ItemState -Path $_.FullName })
    $sw.Stop()
    return [pscustomobject]@{ Files = $files; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2) }
}

function Test-ItemExists {
    param([Parameter(Mandatory = $true)] [string] $Path)
    return (Test-Path -LiteralPath $Path)
}

function Start-App {
    <# Start an application on a file, as a double-click would; returns its process. #>
    param([Parameter(Mandatory = $true)] [string] $Path, [string] $Argument)
    if ($Argument) { return (Start-Process -FilePath $Path -ArgumentList "`"$Argument`"" -PassThru) }
    return (Start-Process -FilePath $Path -PassThru)
}

function ConvertFrom-RobocopySummary {
    <# The Files line of robocopy's summary: Total, Copied, Skipped, Mismatch, Failed, Extras. #>
    param([Parameter(Mandatory = $true)] [AllowEmptyCollection()] [string[]] $Output)
    foreach ($line in $Output) {
        if ($line -match '^\s*Files\s*:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)') {
            return [pscustomobject]@{ Total = [int]$Matches[1]; Copied = [int]$Matches[2]; Skipped = [int]$Matches[3]
                Mismatch = [int]$Matches[4]; Failed = [int]$Matches[5]; Extras = [int]$Matches[6] }
        }
    }
    return $null
}

function Read-FileTimed {
    <# Read a whole file, as an application would: how long it took, its SHA-256, or the error. #>
    param([Parameter(Mandatory = $true)] [string] $Path)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            $sha = [Security.Cryptography.SHA256]::Create()
            $hash = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally { $stream.Dispose() }
        $sw.Stop()
        return [pscustomobject]@{ Ok = $true; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2); Sha256 = $hash; Error = $null; HResult = $null }
    } catch {
        $sw.Stop()
        $e = $_.Exception
        while ($e.InnerException) { $e = $e.InnerException }
        return [pscustomobject]@{ Ok = $false; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2); Sha256 = $null
            Error = $e.Message; HResult = ('0x{0:X8}' -f $e.HResult) }
    }
}

function Open-ExplorerFolder {
    <# Open a folder in Explorer and set its view (Shell.Application). Views: details, largeicons. #>
    param([Parameter(Mandatory = $true)] [string] $Path, [string] $View = 'details')
    $modes = @{ largeicons = 1; details = 4 }
    $shell = New-Object -ComObject Shell.Application
    $shell.Open($Path)
    Start-Sleep -Seconds 3
    $w = @($shell.Windows()) | Where-Object { $_.Document -and $_.Document.Folder.Self.Path -eq $Path } | Select-Object -Last 1
    if ($w) { $w.Document.CurrentViewMode = $modes[$View] }
    return [bool]$w
}

function Select-InExplorer {
    <# Select one file in the open Explorer window of its folder (as a click would). #>
    param([Parameter(Mandatory = $true)] [string] $Path)
    $folder = Split-Path -Parent $Path
    $shell = New-Object -ComObject Shell.Application
    $w = @($shell.Windows()) | Where-Object { $_.Document -and $_.Document.Folder.Self.Path -eq $folder } | Select-Object -Last 1
    if (-not $w) { return $false }
    $item = $w.Document.Folder.ParseName((Split-Path -Leaf $Path))
    $w.Document.SelectItem($item, 1 + 4 + 8 + 16)   # select, deselect others, show, focus
    return $true
}

function Show-Properties {
    <# Open a file's Properties dialog, as a right-click, Properties would. #>
    param([Parameter(Mandatory = $true)] [string] $Path)
    $shell = New-Object -ComObject Shell.Application
    $f = $shell.Namespace((Split-Path -Parent $Path)).ParseName((Split-Path -Leaf $Path))
    $f.InvokeVerb('properties')
}

function Copy-WithExplorer {
    <# Copy a folder with Explorer's own copy engine (Shell.Application CopyHere), and wait for it. #>
    param([Parameter(Mandatory = $true)] [string] $Source, [Parameter(Mandatory = $true)] [string] $Destination)
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $shell = New-Object -ComObject Shell.Application
    $shell.Namespace($Destination).CopyHere($Source, 16 + 1024)   # yes to all, no error UI
    $name = Split-Path -Leaf $Source
    $n = @(Get-ChildItem -LiteralPath $Source -Recurse -File -Force).Count
    for ($i = 0; $i -lt 3600; $i++) {
        $target = Join-Path $Destination $name
        if ((Test-Path -LiteralPath $target) -and @(Get-ChildItem -LiteralPath $target -Recurse -File -Force).Count -ge $n) { return }
        Start-Sleep -Seconds 1
    }
}

function Invoke-Robocopy {
    <# robocopy with the given arguments; its output lines and exit code (0 to 7 are success). #>
    param([Parameter(Mandatory = $true)] [string[]] $Arguments)
    $out = & robocopy.exe @Arguments 2>&1
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($out | ForEach-Object { [string]$_ }) }
}

function Invoke-DefenderScan {
    <# A Microsoft Defender scan: QuickScan, or CustomScan of one path. #>
    param([Parameter(Mandatory = $true)] [string] $ScanType, [string] $Path)
    if ($ScanType -eq 'CustomScan') { Start-MpScan -ScanType CustomScan -ScanPath $Path } else { Start-MpScan -ScanType QuickScan }
}

function Get-IndexedItems {
    <# The paths under a scope that Windows Search has indexed (its SystemIndex, through OLE DB). #>
    param([Parameter(Mandatory = $true)] [string] $Scope)
    $conn = New-Object -ComObject ADODB.Connection
    $rs = New-Object -ComObject ADODB.Recordset
    $conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
    $scopeUrl = 'file:' + $Scope.Replace('\', '/')
    $rs.Open("SELECT System.ItemPathDisplay FROM SYSTEMINDEX WHERE SCOPE='$scopeUrl'", $conn)
    $paths = @()
    while (-not $rs.EOF) { $paths += [string]$rs.Fields.Item('System.ItemPathDisplay').Value; $rs.MoveNext() }
    $rs.Close(); $conn.Close()
    return $paths
}

function Get-OfflineFilesState {
    <# Whether Windows Offline Files (client-side caching) is on, and what it holds under a path. #>
    param([Parameter(Mandatory = $true)] [string] $Path)
    $cache = Get-CimInstance -ClassName Win32_OfflineFilesCache -ErrorAction SilentlyContinue
    $items = @(Get-CimInstance -ClassName Win32_OfflineFilesItem -ErrorAction SilentlyContinue |
        Where-Object { $_.ItemPath -like "$Path*" })
    return [pscustomobject]@{ Enabled = [bool]($cache -and $cache.Enabled); ItemsCached = $items.Count }
}

function Get-PathAcl {
    <# The ACL Windows shows on the Security tab: each entry's identity, rights and type. #>
    param([Parameter(Mandatory = $true)] [string] $Path)
    $acl = Get-Acl -LiteralPath $Path
    return @($acl.Access | ForEach-Object {
        [pscustomobject]@{ Identity = [string]$_.IdentityReference; Rights = [string]$_.FileSystemRights; Type = [string]$_.AccessControlType }
    })
}

function Test-AclEdit {
    <# Try what the Security tab's Edit would do: add, then remove, one harmless entry (Everyone may read).
       True if the filer allowed it. #>
    param([Parameter(Mandatory = $true)] [string] $Path)
    try {
        $acl = Get-Acl -LiteralPath $Path
        $rule = New-Object Security.AccessControl.FileSystemAccessRule('Everyone', 'ReadData', 'Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $Path -AclObject $acl
        $acl = Get-Acl -LiteralPath $Path
        [void]$acl.RemoveAccessRule($rule)
        Set-Acl -LiteralPath $Path -AclObject $acl
        return $true
    } catch {
        return $false
    }
}

# ---- recalls and results ---------------------------------------------------------------------------

function ConvertTo-Epoch { param([DateTime] $Utc) return [int64]([DateTimeOffset]$Utc).ToUnixTimeSeconds() }

function Get-EdgeRecallCount {
    <# The recalls the filer started between two UTC times, from its journal (edge\omnifile-recall-count.sh).
       Returns @{ requests; recalls; recall_ids; file_ids }, or $null with a warning when it can't count. #>
    param([Parameter(Mandatory = $true)] $Context, [Parameter(Mandatory = $true)] [DateTime] $Since, [DateTime] $Until)
    $cmd = "$($Context.CountCommand) --since $(ConvertTo-Epoch $Since)"
    if ($PSBoundParameters.ContainsKey('Until')) { $cmd += " --until $(ConvertTo-Epoch $Until)" }
    try {
        $json = Invoke-EdgeCommand -Context $Context -Command $cmd
        return ($json | ConvertFrom-Json)
    } catch {
        Write-Warning "could not count the edge's recalls: $_"
        return $null
    }
}

function Measure-Recalls {
    <# Run an action, wait for the filer to react, and count the recalls it started meanwhile.
       Returns @{ Result = what the action returned; Recalls = the count (Get-EdgeRecallCount) }. #>
    param([Parameter(Mandatory = $true)] $Context, [Parameter(Mandatory = $true)] [scriptblock] $Action)
    $start = (Get-UtcNow).AddSeconds(-1)
    $result = & $Action
    Start-Pause -Seconds $Context.SettleSeconds
    $count = Get-EdgeRecallCount -Context $Context -Since $start -Until ((Get-UtcNow).AddSeconds(1))
    return [pscustomobject]@{ Result = $result; Recalls = $count }
}

function Get-RecallNumber {
    <# The number of distinct recalls in a count, or $null when it wasn't counted. #>
    param($Count)
    if ($null -eq $Count) { return $null }
    return [int]$Count.recalls
}

function Test-NoRecall {
    <# True when a count shows no recall, false when it shows one, $null when nothing was counted. #>
    param($Count)
    $n = Get-RecallNumber $Count
    if ($null -eq $n) { return $null }
    return ($n -eq 0)
}

function Resolve-Verdict {
    <# A row's verdict from its checks: any false fails it; any not measured ($null) leaves it recorded;
       otherwise it passes. #>
    param([AllowEmptyCollection()] [object[]] $Checks = @())
    if (@($Checks | Where-Object { $_ -is [bool] -and -not $_ }).Count -gt 0) { return 'fail' }
    if (@($Checks | Where-Object { $null -eq $_ }).Count -gt 0) { return 'recorded' }
    return 'pass'
}

function Write-MatrixResult {
    <# One row's result: <OutDir>\<Row>.json, a line of results.md, and the object itself.
       Verdict: pass, fail, or recorded (a row that measures rather than checks). #>
    param(
        [Parameter(Mandatory = $true)] $Context,
        [Parameter(Mandatory = $true)] [string] $Row,
        [Parameter(Mandatory = $true)] [string] $Action,
        [Parameter(Mandatory = $true)] [string] $Expected,
        [Parameter(Mandatory = $true)] [ValidateSet('pass', 'fail', 'recorded')] [string] $Verdict,
        [hashtable] $Measures = @{},
        [hashtable] $Observations = @{},
        [string] $Notes = '',
        [DateTime] $StartedUtc
    )
    if (-not $PSBoundParameters.ContainsKey('StartedUtc')) { $StartedUtc = Get-UtcNow }
    $result = [ordered]@{
        row          = $Row
        action       = $Action
        expected     = $Expected
        verdict      = $Verdict
        client       = $Context.Client
        share        = $Context.Share
        share_kind   = $Context.ShareKind
        encrypted    = $Context.Encrypted
        auth         = $Context.Auth
        role         = $Context.Role
        started_utc  = $StartedUtc.ToString('o')
        finished_utc = (Get-UtcNow).ToString('o')
        measures     = $Measures
        observations = $Observations
        notes        = $Notes
    }
    $json = $result | ConvertTo-Json -Depth 6
    Set-Content -LiteralPath (Join-Path $Context.OutDir "$Row.json") -Value $json -Encoding UTF8
    $record = @(
        @($Measures.Keys | Sort-Object | ForEach-Object { "$_=$(Format-MatrixValue $Measures[$_])" }) +
        @($Observations.Keys | Sort-Object | ForEach-Object { "$_=$(Format-MatrixValue $Observations[$_])" })
    ) -join '; '
    $md = Join-Path $Context.OutDir 'results.md'
    if (-not (Test-Path -LiteralPath $md)) {
        Set-Content -LiteralPath $md -Encoding UTF8 -Value @(
            "# Windows client matrix: $($Context.Client.Caption) $($Context.Client.Version) on $($Context.Client.Machine)",
            '', '| # | Share | Verdict | Record | Notes |', '|---|---|---|---|---|')
    }
    $kind = "$($Context.ShareKind)$(if ($Context.Encrypted) { ', encrypted' })$(if ($Context.Auth -ne 'kerberos') { ", $($Context.Auth)" })"
    Add-Content -LiteralPath $md -Encoding UTF8 -Value "| $Row | $kind | $Verdict | $($record -replace '\|', '/') | $($Notes -replace '\|', '/') |"
    Write-Output ([pscustomobject]$result)
    Write-Host "$Row $Verdict  $record"
}

function Format-MatrixValue {
    param($Value)
    if ($null -eq $Value) { return 'n/a' }
    if ($Value -is [bool]) { if ($Value) { return 'yes' } else { return 'no' } }
    if ($Value -is [array]) { return ($Value -join ',') }
    return [string]$Value
}

Export-ModuleMember -Function *
