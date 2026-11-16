# Pilots before committing to the build

Four spikes, run in parallel, about four weeks in total. Each one retires a risk that no document could settle.

## 1. Filer spike (Rust + C)

**Local run done, 2026-09-26: passed.** See `docs/pilots/filer-spike-results.md`. The on-prem items listed there are still open.

**Setup:** Ubuntu 26.04 LTS, minimized (kernel 7.0), on xfs, NFS-Ganesha with a prototype `FSAL_OMNI`, Samba, a 200 GB SEG-Y file, and clients on NFSv3, NFSv4.0 and SMB.

**Prove:**
- A read of a stub returns "wait" (`NFS4ERR_DELAY` / `NFS3ERR_JUKEBOX`) or "fail fast" (`EIO`) according to the share policy, from every client type.
- The FSAL sees the caller's uid and client IP on every read, with both `sec=sys` and `sec=krb5`.
- **Killing the daemon fails closed.** No stub ever reads as zeros. This is the most important result of the spike.
- Resident headers (first N MB) are readable without triggering a recall.
- Once a file is restored, full-speed sequential reads work.
- Settle the appliance image: which minimized Ubuntu image we start from, what we add to it, and how OS updates and reboots are managed without disrupting NFS clients.
- The FSAL design works over a remote backend (Azure/S3 range reads plus a chunk cache), not only over local xfs — needed for `docs/design/migration-namespace.md`.

## 2. Network test

**Measure:**
- Throughput to a Blob private endpoint through the client's actual ExpressRoute gateway SKU, compared with a relay VM using FastPath. Microsoft warns private-endpoint throughput "might be reduced by half".
- Stream-count and block-size sweep: 16–300 streams, 64 MiB–1 GiB blocks.
- Behaviour with the 1,400-byte MTU limit on ExpressRoute gateways.
- Private Link data-processing cost at the measured volume.

## 3. Data test

**Using the client's own SEG-Y files, measure:**
- Lossless compression ratio and throughput per core for zstd -1/-3, lz4, Blosc shuffle + zstd, and zfp reversible mode. Expectation: 1.1–1.5×; this has not been verified for seismic data.
- The share of byte-identical files across project trees, which is what whole-file dedupe can save.

## 4. Azure test

- Upload with no explicit tier into a Smart-default ZRS account, and confirm the blob tiers under Smart.
- Copy a smart-tier blob server-side into an Archive-tier blob in a second (LRS) account in the same region. Check SAS versus Entra authorization for the cross-account source.
- Recall 8 GB, 184 GB and 1.2 TB files at Standard and High priority, and record the real times. Microsoft documents nothing above 10 GB, and caps priority retrieval at 10 GiB/h per account.
- Deliver a proactive Teams card with `Action.Execute` buttons to a real user in the client's tenant, through the public relay. Confirm the Graph proactive install works under the tenant's app policies.
- Resolve an AD SID to an Entra user with `onPremisesSecurityIdentifier`.
- **Storage Mover (decision 20).** Run an S3 → Blob job into a target account that has a customer-created private endpoint, public network access **disabled** and shared keys off, using the private source path. If Storage Mover can't write to it, record what the setup guide must tell customers to allow. Include a mix of restored and unrestored Glacier objects in the prefix, and record how the unrestored ones are counted. Check what happens to `x-amz-meta-*` and to the destination tier. Try both patterns from decision 24: an Additive rerun after a restore wave, and a staging prefix for an urgent recall. Run the per-file CRC64 verifier over the result, and time it per TB.
