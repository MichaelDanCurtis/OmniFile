# Observability (M6)

**Status: BUILT (2026-10-15; decision 37).** The owner approved sections 1 and 2 as proposed, with metrics. One part of the proposal could not be built as written, and is recorded in section 1. This note covers the M6 item "metrics and logs from edge and service, with recall and upload dashboards in the portal" (`docs/BUILD-PLAN.md`).

It follows the research rule: native platform features first. A feature is rejected only for a recorded reason.

## 1. Service: Azure Monitor

- **Logs.** The Container Apps environment sends stdout and stderr to the Log Analytics workspace (`deploy/azure/modules/cae.bicep`). The service logs JSON lines, so queries filter on fields without parsing.
- **Metrics: OpenTelemetry, straight to Application Insights.**
  - **Not through the managed agent.** The proposal sent them through the Container Apps managed OpenTelemetry agent. Microsoft's documentation (checked 2026-10-15) says that agent sends only logs and traces to Application Insights, and speaks gRPC only. So the service exports metrics itself, with the Azure Monitor OpenTelemetry exporter (MIT).
  - **Authentication:** each app's managed identity, which has *Monitoring Metrics Publisher* on the Application Insights component. The component has local (key) authentication off.
  - **Private networking:** ingestion can be kept private with an Azure Monitor Private Link Scope (`monitorPrivateLink`). It is off by default, because a scope changes DNS for every Azure Monitor endpoint the VNet resolves.
  - **OTLP for others:** `OMNIFILE_METRICS_EXPORTER=otlp` sends to the managed agent instead, for a customer whose metrics destination is an OTLP endpoint of their own.
  - **Code:** `service/src/omnifile_service/metrics.py`; the instruments use the vendor-neutral API, so the exporter is configuration.
- **Instruments** (all in `metrics.py`):
  - **queues:** depth and oldest message age per PGMQ queue, dead-letter queues included;
  - **handlers:** duration by queue and outcome (`ok`, `retry`, `failed`, `dead`);
  - **API calls:** requests and duration by app, route template (never the raw path), method and status, for the edge API and the portal API;
  - **recalls:** time from requested to available, by priority and backend;
  - **uploads:** time from committed to verified, by backend;
  - **migration prefixes** by state, and **restores open** by trigger;
  - **change feed:** the latest maximum lag per container;
  - **provider throttling:** S3 throttling answers by operation, and the local restore budget running out;
  - **edges:** heartbeat age, smb-guard armed, outbox depth, data-volume free share and recalls waiting, per edge.
  
  The worker's collector reads the gauges from the catalog once a minute. Recalls and uploads that finished since the last pass are recorded as histograms then.
- **Alerts** (`deploy/azure/modules/app-metrics.bicep`): log search alerts over the `AppMetrics` table, sent to the action group in `alertActionGroupId`:
  - a queue's oldest message older than 15 minutes;
  - a dead-letter queue not empty;
  - change-feed lag over an hour;
  - a migration prefix in `failed`;
  - S3 throttling in each of the last 10 minutes;
  - an edge silent for 15 minutes;
  - an edge whose smb-guard isn't armed;
  - an edge with less than 10% of its data volume free;
  - an edge whose break-glass account was used and not sealed again;
  - feed or audit rows in a DEFAULT time partition (the worker's daily partitions sweep failed or was down).
  - a file waiting for its own copy of a shared blob, which keeps firing until the copy can be made (`tier-held-back`, gauge `omnifile.tier.own_copy_stuck`, `docs/milestones/results/wp26.md`);
  - a file past the end of its archive date and not in Archive, while it is (`tier-archive-overdue`, gauge `omnifile.tier.archive_overdue`), and one that landed late (`tier-archive-late`);
  - a file sent to Archive before its date by another file's date, against any pin it had (`tier-forced-archive`).
  - a `sec=sys` share now over folders restricted to AD groups, with no host-trust override recorded (threat model T7; gauge `omnifile.shares.host_trusted_restricted`).
  - an annex filer whose NAS shows no ACLs, not acknowledged by an ArchiveAdmin (decision 64; gauge `omnifile.annex.filers_acls_hidden`).
  - an annexed file whose copy Azure's read-back refused three times in a row, so its edge isn't asked to copy it again (`annex.md` section 3.3; gauge `omnifile.annex.copies_refused`). A file whose copying an ArchiveAdmin stopped is in the gauge too, whatever its count. An acknowledgement takes a file out of the gauge until a version verifies, or until its copy is refused again while copying isn't stopped; a stopped file stays on the annex's Problems.
  - the share map's counts more than two hours behind: the worker's roll-up isn't keeping up, or isn't running (`share-map-stale`, gauge `omnifile.share_map.age`, `usage/share_map.py`). The map keeps showing its counts, with the time they are as of.
- **Why not Prometheus and Grafana:** they are one more thing for the customer to run and secure in their subscription. Azure Managed Grafana could read the same Azure Monitor data later, if a customer wants it.

## 2. Edge: the heartbeat, and the Azure Monitor Agent through Arc

- **The heartbeat now runs.** Building this found that the daemon never sent its heartbeat (`control-api.md` §4.7), and never renewed its 30-day client certificate either: an edge would have been locked out of the service a month after enrolment.
  - The daemon now beats every `[feed] heartbeat_s` (60 s), starting at once.
  - After each beat it renews the certificate when it is due: day 20 of 30, or the service's `certificate_renew_after`.
  - Test: `the_heartbeat_reports_health_and_renews_the_certificate`.
- **Health in the catalog.** The heartbeat carries the version, kernel, smb-guard state and feed cursor, plus these gauges:
  - outbox depth;
  - fills running and recalls waiting;
  - free and total space on the data volume;
  - FSAL connections.

  The service keeps them in `catalog.edge` (migration 0021). The portal's **Edges** page shows them (`GET /v1/edges`, `portal-api.md` §5.14), and the worker exports them as metrics. No metrics endpoint is opened on the appliance.
- **Logs.** The daemon writes JSON lines to the journal. rsyslog forwards the journal to syslog. On an Arc-connected edge, the **Azure Monitor Agent** extension ships the `daemon` and `auth` facilities to the workspace, under a data collection rule (`deploy/azure/modules/edge-monitoring*.bicep`, enabled by `edgeArcMachineIds`). The appliance image must include rsyslog. The agent dials out; nothing new listens on the box.
- **Why not a scrape endpoint on the edge:** it would be a new listening port on a box that otherwise only dials out, and a new thing to secure.

## 3. The portal's recall and upload dashboards (built)

`GET /v1/activity` (`portal-api.md` §5.13; `service/src/omnifile_service/web/routes_activity.py`) and the portal's **Activity** page (ArchiveAdmin). Everything comes from the catalog, so the dashboards need neither section 1 nor 2.

- **Window:** per UTC day over the last 7, 30 or 90 days, optionally for one dataset.
- **Uploads:** files and bytes an edge committed, copies still waiting for their read-back verification, and files still landing on an edge.
- **Recalls:** started, available and failed; the median and 90th-percentile time to available; bytes recalled; and recalls open now.
- **On the page:** a chart of bytes uploaded per day. Each bar's value is also in the table below it, so it is accessible without the chart.

## 4. Open

- The alert thresholds are guesses until pilot 2 (throughput) and the first real migration. They are parameters of `app-metrics.bicep`.
- **VERIFY on real Azure** (`docs/milestones/real-azure-checks.md`):
  - metrics from the exporter reach `AppMetrics` with a managed identity and local authentication off;
  - the alert queries match the exporter's naming of attributes in `Properties`;
  - the Azure Monitor Agent on an Arc-connected appliance ships the journal.
