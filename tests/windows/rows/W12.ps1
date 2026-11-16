<#
.SYNOPSIS
    W12: rename, move and delete files and folders.
.DESCRIPTION
    Expected: the catalog follows (namespace-sync.md), and the portal shows the new names. The script
    makes a scratch folder in -Folder, does each change, and times how long the portal takes to agree,
    as the tester sees it. Explorer and this script make the same SMB calls; do it once in Explorer too
    if the client's people mostly use Explorer.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] $Context,
    # A folder on the share where the test user may write.
    [Parameter(Mandatory = $true)] [string] $Folder
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'OmniFileMatrix.psm1')

$started = Get-UtcNow
$stamp = (Get-UtcNow).ToString('yyyyMMddHHmmss')
$scratch = Join-Path $Folder "w12-$stamp"
New-Item -ItemType Directory -Path $scratch | Out-Null
New-Item -ItemType Directory -Path (Join-Path $scratch 'moved-here') | Out-Null
Set-Content -LiteralPath (Join-Path $scratch 'a.txt') -Value 'W12 rename'
Set-Content -LiteralPath (Join-Path $scratch 'b.txt') -Value 'W12 move'
Set-Content -LiteralPath (Join-Path $scratch 'c.txt') -Value 'W12 delete'
Wait-Tester -Context $Context -Instruction "In the portal, open $($scratch): a.txt, b.txt, c.txt and moved-here should be listed (it can take a moment)."

function Time-Change([string] $What, [scriptblock] $Change, [string] $See) {
    & $Change
    $t0 = Get-UtcNow
    Wait-Tester -Context $Context -Instruction "$What. Reload the portal until it shows $See."
    return [math]::Round(((Get-UtcNow) - $t0).TotalSeconds, 1)
}
$rename = Time-Change 'a.txt was renamed to a-renamed.txt' { Rename-Item -LiteralPath (Join-Path $scratch 'a.txt') -NewName 'a-renamed.txt' } 'a-renamed.txt and no a.txt'
$move = Time-Change 'b.txt was moved into moved-here' { Move-Item -LiteralPath (Join-Path $scratch 'b.txt') -Destination (Join-Path $scratch 'moved-here') } 'b.txt inside moved-here'
$delete = Time-Change 'c.txt was deleted' { Remove-Item -LiteralPath (Join-Path $scratch 'c.txt') } 'no c.txt'
$folderRename = Time-Change 'moved-here was renamed to folder-renamed' { Rename-Item -LiteralPath (Join-Path $scratch 'moved-here') -NewName 'folder-renamed' } 'folder-renamed, with b.txt in it'
$here = @(Get-ChildItem -LiteralPath $scratch -Recurse -Force | ForEach-Object { $_.Name } | Sort-Object)
$want = @('a-renamed.txt', 'b.txt', 'folder-renamed')
Write-MatrixResult -Context $Context -Row 'W12' -StartedUtc $started `
    -Action 'Rename, move and delete files and a folder' -Expected 'The catalog follows; the portal shows the new names.' `
    -Verdict (Resolve-Verdict @((($here -join ',') -eq ($want -join ',')), $null)) `
    -Measures @{ scratch = $scratch; on_the_share = $here } `
    -Observations @{ rename_seconds = $rename; move_seconds = $move; delete_seconds = $delete; folder_rename_seconds = $folderRename } `
    -Notes 'The seconds are until the portal agreed, as the tester saw it.'
