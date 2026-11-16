# Settings for one run of the Windows client matrix on one client (Invoke-Matrix.ps1).
# Copy this file, fill it in, and run:  .\Invoke-Matrix.ps1 -Settings .\matrix.psd1
# One run covers one share and one way of signing in. Run it again for each combination the
# matrix asks for (docs/pilots/windows-client-matrix.md, "Clients").
@{
    # The filer, for ssh: an AD administrator of the filer who may read its journal (see the matrix doc).
    Edge          = 'alice.admin@omnifile-hou1.corp.example'
    # The share, as this client reaches it.
    Share         = '\\omnifile-hou1.corp.example\projects-wait'
    # The share's open behaviour: wait, failfast or quick.
    ShareKind     = 'wait'
    # $true for a share with smb encrypt = required.
    Encrypted     = $false
    # kerberos (a domain-joined client's default) or ntlm (when NTLM is forced).
    Auth          = 'kerberos'
    # The share group the signed-in test user is in: Owner, Writer or Reader.
    Role          = 'Writer'
    # Where the results go.
    OutDir        = 'C:\omnifile-matrix\results\win11-wait-kerberos'
    # How the recalls are counted on the filer (tests\windows\edge\omnifile-recall-count.sh, copied to
    # the admin's home there).
    CountCommand  = '~/omnifile-recall-count.sh'
    # Seconds to wait after each action before counting recalls.
    SettleSeconds = 20

    # Each row and its inputs. Leave a row out to skip it. W4 needs a wait share, W5 a failfast one.
    Rows          = @{
        W01 = @{ Folder = '\\omnifile-hou1.corp.example\projects-wait\matrix\fifty-stubs' }
        W02 = @{ File = '\\omnifile-hou1.corp.example\projects-wait\matrix\preview\report.pdf' }
        W03 = @{ File = '\\omnifile-hou1.corp.example\projects-wait\matrix\props\line_0001.segy' }
        W04 = @{
            SmallFile   = '\\omnifile-hou1.corp.example\projects-wait\matrix\wait\small.txt'
            LargeFile   = '\\omnifile-hou1.corp.example\projects-wait\matrix\wait\line_2gb.segy'
            SmallSha256 = ''
            LargeSha256 = ''
            AppPath     = ''
        }
        # W05 needs a failfast share: in that run's settings, add
        #   W05 = @{ File = '\\omnifile-hou1.corp.example\projects-failfast\matrix\failfast\line_0002.segy' }
        W06 = @{
            ExplorerSource = '\\omnifile-hou1.corp.example\projects-wait\matrix\copy-explorer'
            RobocopySource = '\\omnifile-hou1.corp.example\projects-wait\matrix\copy-robocopy'
            XaoSource      = '\\omnifile-hou1.corp.example\projects-wait\matrix\copy-xao'
            Destination    = 'C:\omnifile-matrix\copies'
        }
        W07 = @{ Path = '\\omnifile-hou1.corp.example\projects-wait' }
        W08 = @{ Path = '\\omnifile-hou1.corp.example\projects-wait'; Minutes = 15 }
        W09 = @{ Folder = '\\omnifile-hou1.corp.example\projects-wait\matrix\offline' }
        W10 = @{ RelativePath = 'matrix\versions\notes.txt'; Label = '@GMT-2026.11.01-12.00.00'; ExpectedSha256 = '' }
        W11 = @{ Root = '\\omnifile-hou1.corp.example\projects-wait'; Subfolder = '\\omnifile-hou1.corp.example\projects-wait\matrix' }
        W12 = @{ Folder = '\\omnifile-hou1.corp.example\projects-wait\matrix\scratch' }
        W13 = @{ Folder = '\\omnifile-hou1.corp.example\projects-wait\matrix\scratch' }
        W14 = @{ Folder = '\\omnifile-hou1.corp.example\projects-wait\matrix\fifty-stubs'; Minutes = 60 }
    }
}
