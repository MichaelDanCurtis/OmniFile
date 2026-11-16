# S3 Metadata tables as an inventory and journal (M5)

**Status: BUILT behind a per-source choice (2026-10-20), spike on real AWS pending.** Ported from line B (PR #10, commits 2a630a3 and 88c525b) onto this line's seeding: `sources/metadata_s3.py` (the reader and the journal), `worker/journal_sync.py`, migration 0028. A source uses the tables when it names them (`inventory.kind = s3_metadata`) or, naming none, when its backend's `metadata_tables` lists its bucket; otherwise S3 Inventory or a listing, as before (`catalog-seeding.md` §7, §8). It follows §3 and §4 below, with one change found while building: the live inventory table lists every version and delete marker, so the reader picks each key's newest row by `sequence_number`. It stays a choice, not the default, until the spike in §5 passes. This note says what S3 Metadata replaces, what it costs, and how it was added. Facts marked [D] are from the AWS documentation (sources at the end). Items marked **VERIFY** need a check on real AWS (`real-aws-checks.md`).

## 1. What S3 Metadata is

For a general purpose bucket, S3 keeps read-only, AWS-managed Apache Iceberg tables in a table bucket named `aws-s3`. There is one per account and Region, and the bucket's tables are in the namespace `b_<bucket>` [D]:

- **`journal`** (always on): one row per change, with `record_type` one of:
  - `CREATE` (a new object or version);
  - `UPDATE_METADATA` (for example the storage class or tags);
  - `DELETE` (a delete marker, `is_delete_marker = true`, or a permanent delete);
  - three annotation types.

  Rows are "near real time" and cover changes after the configuration was created. They carry the version, size, ETag, storage class, `checksum_algorithm`, `user_metadata` and `requester`. `sequence_number` orders the records of one key [D].
- **`inventory`** (optional): one row per object and version, current state. It is backfilled at creation ("minimum 15 minutes" to hours) and charged for that, then is "typically" up to date "within one hour" [D].

Limits that matter here [D]:
- **Whole bucket only:** a configuration can't be set per prefix.
- **Snapshots:** S3 keeps "a minimum of 1 snapshot for a maximum of 24 hours".
- **Journal retention:** records never expire by default; an expiry, if set, is at least 7 days.
- **Missing fields:**
  - the Intelligent-Tiering tier isn't shown;
  - nor are Object Lock retention, ACLs or replication status.
- **Regions:** available in most commercial Regions, China and GovCloud.

## 2. What it would replace

| Today (built) | With S3 Metadata |
|---|---|
| **Seeding** from S3 Inventory CSV (daily or weekly, CSV only) or a listing (`catalog-seeding.md` §7) | From the `inventory` table: current at most about an hour, with every version, and with **`user_metadata`**. So the POSIX attributes a File Gateway wrote (`x-amz-meta-file-owner/-group/-permissions`) come with the seed, where Inventory has none and seeding falls back to the source's defaults. |
| **Following** from EventBridge → SQS, each event a prompt that is HEADed (`catalog-seeding.md` §8) | From the `journal`, read in `sequence_number` order per key. Storage class changes arrive as `UPDATE_METADATA` rows, and `checksum_algorithm` says whether a CRC64NVME exists before any HEAD. |
| Restore completion and expiry from EventBridge → SQS (`AGENTS.md`) | **Unchanged.** The journal records PUT and DELETE changes, not restores. |

The SQS queue stays either way, for restores. The journal would replace only the object-change events.

## 3. How the service would read it

- **Client:** PyIceberg's REST catalog against the S3 Tables Iceberg REST endpoint (`https://s3tables.<region>.amazonaws.com/iceberg`, SigV4 signing name `s3tables`) [D]. Licences: Apache-2.0, with its Parquet reader **pyarrow** (Apache-2.0), a large dependency. **VERIFY** PyIceberg's SigV4 options against that endpoint.
- **Private networking:**
  - An interface endpoint `com.amazonaws.<region>.s3tables` serves table and namespace operations.
  - The table's data and metadata files are read through S3 (gateway or interface endpoint) [D].
  - With private DNS, the public names resolve privately [D]. This fits the rule that OmniFile never falls back to a public path.
  - **VERIFY** that the `aws-s3` table bucket's files are readable through the S3 interface endpoint OmniFile already uses, and TLS 1.2 on both.
- **Seeding:** a `metadata` reader beside `inventory_csv` and `listing`. It scans `inventory` with a filter `key LIKE '<prefix>%' AND is_delete_marker = false` and the latest row per key. Its `resume` is `(snapshot id, last key)`: a seed pins the snapshot it started from. A pinned snapshot can expire after 24 hours [D], so a seed that outlives it starts a new snapshot from its last key. Keys are applied idempotently, as today.
- **Following:**
  - The cursor is `(snapshot id, record_timestamp)`. Each pass reads the rows added since the last snapshot it saw (an Iceberg incremental append scan), for the source's prefixes.
  - If that snapshot has expired (more than 24 hours behind), the pass falls back to a `record_timestamp >` scan, which the 7-day minimum retention covers. Further behind than retention: a re-seed, like a feed cursor beyond retention.
  - A row is still a prompt: §8's HEAD and its table apply unchanged, so the journal changes only where prompts come from.
- **Cost:** the journal has no per-read charge for us beyond S3 Tables requests and storage, and the inventory backfill is charged once (**VERIFY** current S3 Metadata pricing). The journal's own storage is the customer's, kept down by its record expiry, which the setup guide sets to 14 days.

## 4. Recommendation

Add S3 Metadata as an **option**, not the default:

1. **Seeding:** use the `inventory` table when the bucket has an S3 Metadata configuration; otherwise Inventory CSV, then listing, as today. It is fresher, carries user metadata, and needs no Inventory report to be set up first.
2. **Following:** use the `journal` when the bucket has one. Keep EventBridge → SQS for restores, and for object changes on buckets without a journal.

**Why not the default:**
- it is a whole-bucket configuration the customer pays for;
- it adds pyarrow to the service image;
- the current path already works over private networking.

The native-features rule is still met: where the customer has S3 Metadata, OmniFile uses it.

## 5. Spike before building (about 2 days, real AWS)

1. Create a metadata configuration with an inventory table on a test bucket of 10^5 objects. Note the backfill time and cost.
2. From a VPC with only the `s3tables` and S3 interface endpoints (no internet), read `inventory` and `journal` with PyIceberg: REST catalog, SigV4, TLS 1.2.
3. Measure the journal's lag from a PUT to its row, and the incremental-scan pattern across a snapshot expiry.
4. Check that File Gateway's `x-amz-meta-file-*` keys show in `user_metadata` as written.

## Sources

- S3 Metadata overview: https://docs.aws.amazon.com/AmazonS3/latest/userguide/metadata-tables-overview.html
- Journal schema: https://docs.aws.amazon.com/AmazonS3/latest/userguide/metadata-tables-schema.html
- Inventory schema: https://docs.aws.amazon.com/AmazonS3/latest/userguide/metadata-tables-inventory-schema.html
- Limitations and Regions: https://docs.aws.amazon.com/AmazonS3/latest/userguide/metadata-tables-restrictions.html
- S3 Tables over PrivateLink: https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-VPC.html
- S3 Tables Iceberg REST endpoint: https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-open-source.html
