# Pilots 2–4: runbook for running them outside the dev machine

Pilots 2, 3 and 4 in `docs/PILOTS.md` need the client's network, data or Azure tenant, so they can't run on a laptop. This runbook says what each one needs, who has to provide it, how to run it, and what to send back. Pilot 1 (the filer spike) runs locally first; see `docs/pilots/filer-spike.md`. Its on-site part includes the Windows client matrix, `docs/pilots/windows-client-matrix.md`.

## Pilot 2: network test

**Question:** how fast can the edge push data into Blob through a private endpoint over the client's ExpressRoute (or VPN)? Microsoft warns that throughput through the ExpressRoute gateway "might be reduced by half".

**Needs from the client:**
- A Linux VM on-prem, on the network segment the edge filer will use.
- A storage account in the target region, with a private endpoint that the customer creates (decision 22), and private DNS that resolves from on-prem.
- The ExpressRoute gateway SKU and circuit bandwidth.
- An Entra identity with Storage Blob Data Contributor on a test container.
- A small VNet VM in the same region, to act as the "relay" comparison point.

**Run:**
1. Use AzCopy's built-in benchmark (`azcopy bench`, a native tool, so nothing to build):
   - Sign in with `azcopy login`, using Entra.
   - Run `azcopy bench` against the test container's private-endpoint URL.
   - Sweep concurrency (`AZCOPY_CONCURRENCY_VALUE` at 16, 32, 64, 128, 300) and block size (`--block-size-mb` at 64, 256, 1024).
   - Use files of 4 GB or more.
   - Confirm the flags with `azcopy bench --help` on the version installed.
2. Repeat from the relay VM inside the VNet, which doesn't cross the gateway. The difference is the gateway's cost. If the client's gateway SKU supports ExpressRoute FastPath for private endpoints, repeat with it on.
3. Check the path MTU across ExpressRoute with `ping -M do -s <size>`, stepping down from 1472; the gateway limit is 1,400 bytes. Record whether large transfers stall.
4. Read the Private Link "data processed" meter after the runs and work out the cost per TB.

**Send back:** the table of throughput by concurrency × block size, for on-prem and for the relay VM; the gateway SKU; the MTU result; and the cost per TB.

**Decides:** gateway sizing guidance, whether the relay mode is worth offering, and the uploader's default concurrency and block size.

## Pilot 3: data test

**Question:** how much do real SEG-Y files compress losslessly, and how much of the estate is byte-identical duplicates?

**Needs from the client:**
- Read-only access to a representative set of project trees (ideally several TB).
- A Linux or Mac machine near the data, with Python 3.12 and `uv`.

**Run:** follow the kit in `spikes/data-test/README.md`.
1. `uv run segy_profile.py --self-test` checks the kit.
2. `--dry-run` sizes the job.
3. The real run profiles every file for duplicates, and a seeded random sample (50 GB by default) for compression: zstd -1 and -3, lz4, Blosc shuffle + zstd, and zfp reversible on IEEE-float files.

File paths are hashed in the output unless the client allows them.

**Send back:** the JSON results file and the printed summary.

**Decides:** whether compression is worth its CPU cost at the edge (the planning figure is 1.1–1.5×, unverified for seismic), and how much whole-file dedupe saves.

## Pilot 4: Azure test

**Question:**
- Does the Smart → Archive copy work as designed?
- How long do real Archive recalls take above 10 GB?
- Can we reach a real user with a Teams card?

**Needs from the client:**
- An Azure subscription and resource group, with Storage Account Contributor and Storage Blob Data Contributor for the tester.
- An Entra admin able to grant Microsoft Graph `User.Read.All` and to allow a custom Teams app.
- A test user who is synced from on-prem AD.
- Two or three real SEG-Y files (about 8 GB, about 184 GB, and above 1 TB if possible).
- A budget: Archive has a 180-day early-deletion charge, and High-priority rehydration costs more.

**Run:** follow the kit in `spikes/azure-test/README.md`.
1. `01-create.sh` creates the live account (ZRS, Smart default) and the archive account (LRS). The customer then adds private endpoints.
2. `02-smart-to-archive.sh`:
   - upload with no explicit tier, and confirm Smart manages the blob;
   - copy it server-side to Archive and delete the original;
   - record whether the cross-account copy needed a SAS.
3. `03-recall.sh` rehydrates each file by copy, at Standard and at High priority. It logs timestamps every 10 minutes to CSV. This runs for hours to days.
4. Manual Teams steps (in the kit README):
   - register the bot;
   - install the Teams app for the test user through Graph proactive install;
   - send an Adaptive Card with `Action.Execute` buttons through the public relay;
   - resolve the user's AD SID with `onPremisesSecurityIdentifier`.

**Send back:**
- the timing CSVs;
- the copy and auth findings;
- screenshots of the card and of the button click reaching the relay;
- any Teams app-policy blocks.

**Decides:**
- the recall-time table the portal shows;
- whether Smart → Archive works as designed;
- whether proactive Teams install works under the client's app policies.

## Order and effort

Pilots 2–4 are independent and can run together. Pilot 2 takes about a day, once the endpoint and VMs exist. Pilot 3 runs unattended for hours. Pilot 4 takes an hour or two of set-up, then days of waiting on recalls.
