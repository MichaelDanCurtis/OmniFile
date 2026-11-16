# Windows client matrix (pilot 1, on site)

M7 asks for "the Windows client matrix (from the pilot)", and risk R4 in `docs/BUILD-PLAN.md` is Windows behaviour: long waits on archived files (STATUS_PENDING), and Explorer opening files for thumbnails and starting recalls. Neither can be run without Windows clients joined to the client's AD, so this is the test plan for pilot 1's on-site part (`docs/pilots/remote-pilots.md`), with what is already built and checked on Linux.

## Built and checked before the pilot

- **Stubs are offline to SMB clients.** The edge sets `FILE_ATTRIBUTE_OFFLINE` on every file whose marker says some bytes are not local, and clears it once the file is filled (`omnifile_marker::dosattr`; `[shares] smb_offline`, on by default). Samba reports it from its `user.DOSATTRIB` xattr; only that bit is changed, so the creation time and other attributes Samba stored stay. Windows defines the attribute as data "physically moved to offline storage" by hierarchical storage management, the case Explorer and the indexer are meant to leave unopened. Checked with Samba 4.23 in step 7 of `tests/e2e/m5_exit_test.sh`: a placeholder lists as `Ao`, a filled file as `A`. Stubs made before an upgrade get the bit from a one-time pass at start. Whether each Windows version honours it is the first thing the matrix measures.
- **Offline Files never caches a share.** Every rendered stanza sets `csc policy = disable`, so "Always available offline" can't read, and so recall, a whole folder of stubs.
- **Long waits:** smbd answers STATUS_PENDING while the guard holds a read, and the Linux CIFS client survived a 90 s recall (`docs/pilots/filer-spike-results.md`).
- **Previous Versions:** folders and files opened directly in an unfilled label (`vfs_omnifile_prevver`, decision 36), with Ubuntu's smbd and `smbclient`.

## Clients

| Client | Why |
|---|---|
| Windows 11 25H2 (and 24H2 if the client still runs it) | The desktop fleet. SMB signing is required by default from 24H2. |
| Windows Server 2025 and 2022 | Processing hosts and jump boxes that mount the shares. |
| Windows 10 22H2 with ESU | Only where the client still has machines on extended support. |

Each runs joined to the client's AD and signed in as a test user in the share's Readers, then Writers, group. Run each row on a `wait` share and a `failfast` share, one with `smb encrypt = required` and one without, and with Kerberos (the default for a domain-joined client) and with NTLM forced, if the client allows NTLM.

## Running the kit on site

`tests/windows/` holds a runner for each row below (`rows/W01.ps1` to `rows/W14.ps1`). Each runner does the row's action where a script can (lists the folder, opens Explorer and sets its view, reads files and times them, runs robocopy, Defender and the index query, reads ACLs, makes the renames), asks the tester what only a person can see (an overlay, a dialog, a Teams card), and counts the recalls the filer started meanwhile. It writes `Wnn.json` and a line of `results.md` in the columns of the table below, with a verdict: **pass**, **fail**, or **recorded** for a row that measures rather than checks (or whose recalls couldn't be counted).

**Before the visit, on a machine with the release folder:** copy `tests/windows/` to a USB stick or a share the clients can read.

**On the filer, once,** as an AD administrator of it:

1. Copy the recall counter to your home there and let your account read the filer's journal:
   ```
   scp tests/windows/edge/omnifile-recall-count.sh <admin>@<filer>:
   ssh -t <admin>@<filer> 'chmod +x omnifile-recall-count.sh && sudo gpasswd -a <admin> systemd-journal'
   ```
   `-t` gives sudo a terminal to ask for your password on (the admins' sudo rule asks for it). Log out and in again for the group to apply. `./omnifile-recall-count.sh --since $(date -d '-1 hour' +%s)` should print one line of JSON; it refuses to count, saying why, while your account can't read the journal, rather than report zero recalls. The counter reads the journal only. Remove the group afterwards: `sudo gpasswd -d <admin> systemd-journal`.
2. Make the test folders the settings name: a folder of 50 stubs; a few files for the preview, Properties, wait and failfast rows; three folders of stubs for the copies (each copy recalls its source); a folder with versions from before a rewind label; a scratch folder the test user may write. Leave them until the filer has stubbed them (about an hour without a policy), and check with the portal that each shows as not local.

**On each client:**

1. Sign in as the test user (in the share's Writers group, then Owners and Readers for W11), with an ssh key for the filer admin loaded (`ssh-add`), so the runners can reach the counter without a password: `ssh <admin>@<filer> true` must work with no prompt.
2. Copy `matrix.example.psd1` to `matrix.psd1` and fill it in: the filer, the share and its behaviour (`ShareKind`: `wait`, `failfast` or `quick`), encryption, Kerberos or NTLM, the user's role, the results folder, and each row's paths. W4 needs a `wait` share and W5 a `failfast` one, so they go in different runs.
3. In PowerShell (Windows PowerShell 5.1 is enough), from the kit's folder:
   ```
   Set-ExecutionPolicy -Scope Process Bypass
   .\Invoke-Matrix.ps1 -Settings .\matrix.psd1
   .\Invoke-Matrix.ps1 -Settings .\matrix.psd1 -Rows W1, W5     # only some rows
   ```
   Each row says what to do by hand when it needs you, and waits for Enter. W14 takes an hour: run it last, with someone ready to restart the filer's daemon and to cut its network when it says so (or set `RestartCommand` for the restart).
4. Repeat for each share and sign-in combination under "Clients" with another results folder.

The kit's own tests run anywhere with PowerShell 7 (`tests/windows/run-tests.sh`, Pester 5.7.1, every Windows cmdlet mocked): 64 tests, which check each runner's logic and output, not Windows itself.

## The matrix

Watch the portal's Activity page and the edge's journal (`journalctl -u omnifile-edge`) for recalls started, and record each row's result. The kit records the recalls for you, from the same journal.

| # | Action | Expected | Record |
|---|---|---|---|
| W1 | Open a folder of 50 stubs in Explorer, in Details, then Large icons view. | The files show as offline. No recall starts. | Overlay shown; recalls started (should be 0); time to list. |
| W2 | Select a stub with the preview pane open. | No preview, no recall. | Recalls started. |
| W3 | Right-click a stub, Properties. | Size and dates shown; attributes include Offline (Advanced). No recall. | Recalls started. |
| W4 | Open a stub of a few MB on the `wait` share in Notepad, then a 2 GB stub in the client's seismic tool. | The open waits (STATUS_PENDING) until the recall completes, then reads the right bytes. | Longest wait each app tolerates before an error; any "not responding"; the bytes' hash. |
| W5 | The same on the `failfast` share. | An error at once, and `<file>.RECALLING.txt` beside it. A Teams card reaches the user. | The error Windows shows (which NT status Samba maps EIO to); time to error. |
| W6 | Copy a folder with stubs in it, with Explorer, then `robocopy /E`, then `robocopy /E /XA:O`. | Explorer and plain robocopy read (and recall) every stub; `/XA:O` skips the offline files. | Recalls started per tool; robocopy's skipped count. |
| W7 | Run Microsoft Defender's quick scan and a custom scan of the mapped share. | No recalls from the quick scan. A custom scan that opens files would recall them: record it, and whether Defender skips offline files. | Recalls started. |
| W8 | Turn on Windows Search indexing for the mapped share (where policy allows it). | Offline files are not indexed. | Recalls started; indexed count. |
| W9 | Mark a folder "Always available offline". | Refused or no effect (`csc policy = disable`). | What Windows says; recalls started. |
| W10 | Previous Versions tab on a folder, then on a file; Open, Copy and Restore from a label. | Labels listed newest first; Open reads that version's bytes; Restore follows decision 34. | Labels listed; time to list (labels fill on first open); bytes' hash. |
| W11 | Security tab on a share root and a subfolder, as Owner, Writer and Reader. | The AD groups' entries shown; only Owners may change them (access-control.md). | Entries shown; what an edit does per role. |
| W12 | Rename, move and delete files and folders in Explorer. | The catalog follows (namespace-sync.md); the portal shows the new names. | Time until the portal agrees. |
| W13 | Save over an existing file from Office (Word or Excel), which writes a temp file and renames it. | A new version of the same file, not a new file. | The catalog's versions of that file. |
| W14 | Leave Explorer open on a stub folder for an hour while the edge restarts and the control API is briefly down. | No error dialogs for listing; an open in progress waits or fails by policy. | Anything Windows shows. |

## What decides what

- **W1–W3, W7, W8:** whether the offline bit is enough to stop accidental recalls. If a client opens offline files anyway, the fallback is a share setting that fails opens from that client's processes (Explorer, the indexer) fast instead of recalling: a design change, back to the owner.
- **W4, W5:** the defaults for `wait` and "wait only if quick" (how long Windows applications tolerate STATUS_PENDING), and the error text in the setup guide.
- **W6:** the migration and backup guidance (`/XA:O` for robocopy).
- **W10–W13:** the Windows column of the permission matrix and the restore guidance.

## Send back

For each client and run: the results folder (`Wnn.json` and `results.md`), the `matrix.psd1` used, the edge's journal for the run (`journalctl -u omnifile-edge --since <start>`), and screenshots of W1, W5 and W10.
