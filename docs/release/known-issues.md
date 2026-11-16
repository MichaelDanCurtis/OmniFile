# Known issues

<!-- The release manager edits this before each release: release/notes.sh puts it in the notes. One bullet per
issue: what a user sees, who it affects, and the workaround or the fix's version. Remove an issue once fixed. -->

- **Not yet run on real Azure, AWS or a Teams tenant.** Every check that needs them is listed in
  `docs/milestones/real-azure-checks.md`, `real-aws-checks.md` and `real-tenant-checks.md`.
- **Windows clients, Hyper-V and vSphere** are untested until a client site (`docs/pilots/windows-client-matrix.md`).
- **NFSv3 clients** need `nolock` until NLM is decided (`Enable_NLM = false` in the base Ganesha config).
- **Archive recalls above 10 GB** have no promised time: Azure's priority retrieval is capped at 10 GiB/h per
  storage account.
