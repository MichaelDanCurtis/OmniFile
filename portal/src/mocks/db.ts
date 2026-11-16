/**
 * In-memory world for the API mock (dev with `npm run dev:mock`, and tests).
 * Shapes follow portal-api.yaml through the generated types, so a contract
 * change that breaks the mock breaks the typecheck.
 */
import type {
  Entry,
  AuditRecord,
  Backend,
  Dataset,
  GroupRef,
  Hold,
  LogicalTier,
  Me,
  Pin,
  PolicyBody,
  PolicyVersion,
  Proposal,
  Recall,
  RecallState,
  Share,
  TierState,
  Version,
} from "../api/types";
import { example, examples } from "./examples.gen";
import { createMigrationMock, type MigrationMock } from "./migrations";

export type Persona = "admin" | "steward" | "reader";

const DAY = 86_400_000;

export interface MockFile {
  file_id: string;
  parent_id: string | null;
  share_id: string;
  name: string;
  kind: "file" | "dir";
  size: number;
  state: Entry["state"];
  tier: TierState;
  archive_at: string | null;
  next_action: "tier_warm" | "tier_cold" | "archive" | "stub" | "prune_versions" | null;
  next_action_at: string | null;
  created_at: string;
  deleted_at: string | null;
  last_read_at: string | null;
  versions: Version[];
  legal_hold: boolean;
  retention_until: string | null;
}

export interface MockRecall {
  recall: Omit<Recall, "steps" | "requesters"> & { requesters_count: number };
  log: { state: RecallState; at: string }[];
  /** Simulated: advances with time when set (recalls made in the mock). */
  simulate: boolean;
  requested_by: Persona;
}

export interface MockProposal {
  proposal: Proposal;
  proposer: Persona | "app";
}

export interface MockDb {
  now: number;
  persona: Persona;
  users: Record<Persona, Me & { identity_id: string }>;
  shares: Share[];
  datasets: Dataset[];
  backends: Backend[];
  files: Map<string, MockFile>;
  recalls: MockRecall[];
  pins: Pin[];
  holds: Hold[];
  policies: PolicyVersion[];
  proposals: MockProposal[];
  groups: GroupRef[];
  audit: AuditRecord[];
  restores: Map<string, { state: string; requested_at: number; body: Record<string, unknown> }>;
  downloads: Map<string, { requested_at: number; file_id: string; version_id: string }>;
  /** The folder each share serves (NewShare.path); a share not listed serves its dataset's root. */
  sharePaths: Map<string, string>;
  /** Source backends, sources and migrations (mocks/migrations.ts). */
  migration: MigrationMock;
  nextId: number;
  authTime: number;
}

export const SID = {
  domain: "S-1-5-21-3623811015-3361044348-30300820",
  seismicOwners: "S-1-5-21-3623811015-3361044348-30300820-1201",
  seismicWriters: "S-1-5-21-3623811015-3361044348-30300820-1202",
  dataReaders: "S-1-5-21-3623811015-3361044348-30300820-1203",
  legalOwners: "S-1-5-21-3623811015-3361044348-30300820-1301",
  legalReaders: "S-1-5-21-3623811015-3361044348-30300820-1302",
  hpcCluster: "S-1-5-21-3623811015-3361044348-30300820-1401",
  archiveWatchers: "S-1-5-21-3623811015-3361044348-30300820-1501",
};

const iso = (ms: number) => new Date(ms).toISOString();
const date = (ms: number) => iso(ms).slice(0, 10);

function tier(logical: LogicalTier, native: string, extra: Partial<TierState> = {}): TierState {
  return {
    logical,
    native_class: native,
    archived: logical === "archive",
    restore_state: null,
    restore_expires_at: null,
    explicit_tier_set: false,
    tier_changed_at: null,
    ...extra,
  };
}

const groups: GroupRef[] = [
  {
    sid: SID.seismicOwners,
    display_name: "GG-Seismic-Owners",
    sam_account_name: "gg-seismic-owners",
  },
  {
    sid: SID.seismicWriters,
    display_name: "GG-Seismic-Writers",
    sam_account_name: "gg-seismic-writers",
  },
  {
    sid: SID.dataReaders,
    display_name: "GG-Data-Readers",
    sam_account_name: "gg-data-readers",
  },
  {
    sid: SID.legalOwners,
    display_name: "GG-Legal-Records-Owners",
    sam_account_name: "gg-legal-owners",
  },
  {
    sid: SID.legalReaders,
    display_name: "GG-Legal-Records-Readers",
    sam_account_name: "gg-legal-readers",
  },
  { sid: SID.hpcCluster, display_name: "GG-HPC-Batch", sam_account_name: "gg-hpc-batch" },
  {
    sid: SID.archiveWatchers,
    display_name: "GG-Archive-Watchers",
    sam_account_name: "gg-archive-watch",
  },
  {
    sid: `${SID.domain}-1601`,
    display_name: "GG-Geophysics-Contractors",
    sam_account_name: "gg-geo-contractors",
  },
  { sid: `${SID.domain}-1602`, display_name: "GG-Geomatics", sam_account_name: "gg-geomatics" },
  // Straight from the YAML's GroupRef example.
  ...examples.GroupRef,
];

const group = (sid: string): GroupRef => groups.find((g) => g.sid === sid) ?? { sid };

export function createDb(now: number = Date.now(), persona: Persona = "admin"): MockDb {
  const users: MockDb["users"] = {
    admin: {
      ...example.Me,
      identity_id: "9001",
      object_id: "0f8e2a1c-5d4b-4c3a-9e21-7b6a5c4d3e21",
      display_name: "Avery Chen",
      upn: "avery.chen@contoso-energy.com",
      sid: `${SID.domain}-4101`,
      roles: ["ArchiveAdmin"],
      groups_source: "token",
      groups_resolved_at: iso(now - 20 * 60_000),
      owner_of: [],
      writer_of: [],
      reader_of: ["1", "2", "3", "4"],
    },
    steward: {
      ...example.Me,
      identity_id: "9002",
      object_id: "1a9b3c2d-6e5f-4a4b-8c32-8c7b6d5e4f32",
      display_name: "Sam Patel",
      upn: "sam.patel@contoso-energy.com",
      sid: `${SID.domain}-4102`,
      roles: ["DataSteward"],
      groups_source: "token",
      groups_resolved_at: iso(now - 5 * 60_000),
      owner_of: ["1"],
      writer_of: ["1"],
      reader_of: ["1", "2"],
    },
    reader: {
      ...example.Me,
      identity_id: "9003",
      object_id: "2b0c4d3e-7f60-4b5c-9d43-9d8c7e6f5a43",
      display_name: "Riley Moreno",
      upn: "riley.moreno@contoso-energy.com",
      sid: `${SID.domain}-4103`,
      roles: ["User"],
      groups_source: "token",
      groups_resolved_at: iso(now - 2 * 60_000),
      owner_of: [],
      writer_of: [],
      reader_of: ["1"],
    },
  };

  const shares: Share[] = [
    {
      share_id: "1",
      name: "long-term-storage",
      dataset_id: "1",
      dataset_name: "Seismic survey archive",
      edge_id: "11",
      root_dir_id: "100",
      protocols: ["nfs4", "smb"],
      nfs_sec: ["krb5"],
      nfs_clients: ["hpc-*.contoso-energy.com"],
      root_squash: true,
      trust_level: "proven",
      host_trust_override: false,
      groups: {
        owners: group(SID.seismicOwners),
        writers: group(SID.seismicWriters),
        readers: group(SID.dataReaders),
      },
      on_open: "auto",
      wait_threshold_minutes: 30,
      quiet_s: null,
      quiet_default_s: 10,
      quiet_supported: true,
      edge_name: "edge-houston-1",
      rewind: { depth_days: 30, max_days: 90 },
      worm: { enabled: false, mode: null, policy_state: "none" },
      last_full_acl_scan_at: iso(now - 7 * 3600_000),
      my_access: { owner: false, writer: false, reader: true, can_read_root: true },
      checks: [
        {
          severity: "info",
          code: "kerberos_throughput",
          message:
            "This share uses Kerberos (krb5). Microsoft measured about 53% fewer IOPS with krb5, and about 77% fewer with krb5p, than with sec=sys on NFS.",
          field: "nfs_sec",
          share_id: "1",
          fee: null,
        },
      ],
      etag: '"14"',
    },
    {
      share_id: "2",
      name: "processing-scratch",
      dataset_id: "1",
      dataset_name: "Seismic survey archive",
      edge_id: "11",
      root_dir_id: "200",
      protocols: ["nfs3"],
      nfs_sec: ["sys"],
      nfs_clients: ["10.20.0.0/16"],
      root_squash: true,
      trust_level: "host_trusted",
      host_trust_override: false,
      groups: { owners: group(SID.seismicOwners), writers: group(SID.hpcCluster), readers: null },
      on_open: "wait",
      wait_threshold_minutes: null,
      quiet_s: null,
      quiet_default_s: 10,
      quiet_supported: true,
      edge_name: "edge-houston-1",
      rewind: { depth_days: 7, max_days: 30 },
      worm: { enabled: false, mode: null, policy_state: "none" },
      last_full_acl_scan_at: iso(now - 26 * 3600_000),
      my_access: { owner: false, writer: false, reader: true, can_read_root: true },
      checks: [
        {
          severity: "warning",
          code: "host_trusted",
          message:
            "This share uses sec=sys: the uid is whatever the NFS client says. Only the listed client hosts are trusted.",
          field: "nfs_sec",
          share_id: "2",
          fee: null,
        },
      ],
      etag: '"6"',
    },
    {
      share_id: "3",
      name: "contracts-worm",
      dataset_id: "2",
      dataset_name: "Legal records",
      edge_id: "11",
      root_dir_id: "300",
      protocols: ["smb"],
      nfs_sec: [],
      nfs_clients: [],
      root_squash: true,
      trust_level: "proven",
      host_trust_override: false,
      groups: { owners: group(SID.legalOwners), writers: null, readers: group(SID.legalReaders) },
      on_open: "fail",
      wait_threshold_minutes: null,
      quiet_s: null,
      quiet_default_s: 60,
      quiet_supported: true,
      edge_name: "edge-houston-1",
      rewind: { depth_days: 365, max_days: 3650 },
      worm: {
        enabled: true,
        mode: "locked",
        retention_days: 2555,
        enabled_at: iso(now - 200 * DAY),
        lock_deadline: iso(now - 199 * DAY),
        locked_at: iso(now - 199 * DAY),
        policy_state: "locked",
      },
      last_full_acl_scan_at: iso(now - 3 * 3600_000),
      my_access: { owner: false, writer: false, reader: true, can_read_root: true },
      checks: [],
      etag: '"31"',
    },
  ];

  const scheduled: PolicyBody = {
    mode: "scheduled",
    clock: "last_read",
    warm_day: 30,
    cold_day: 120,
    archive_day: 365,
    stub_at: "archive",
    resident_mb: 4,
    restub_after_hours: 24,
    notify: { requester: true, owners: false, watchers_group_sid: null, email_fallback: true },
  };

  const admin = { ...example.PersonRef, display_name: "Avery Chen", identity_id: "9001" };
  const policies: PolicyVersion[] = [
    {
      policy_id: "41",
      dataset_id: "1",
      version: 4,
      body: scheduled,
      state: "active",
      proposal_id: null,
      approved_by: admin,
      approved_at: iso(now - 21 * DAY),
      note: "Cold at 120 days after the Q3 read-pattern review.",
    },
    {
      policy_id: "31",
      dataset_id: "1",
      version: 3,
      body: { ...scheduled, cold_day: 90 },
      state: "superseded",
      proposal_id: null,
      approved_by: admin,
      approved_at: iso(now - 80 * DAY),
      note: null,
    },
    {
      policy_id: "22",
      dataset_id: "2",
      version: 2,
      body: {
        mode: "scheduled",
        clock: "written",
        warm_day: 30,
        cold_day: 90,
        archive_day: 180,
        stub_at: "archive",
        resident_mb: 0,
        restub_after_hours: 24,
        notify: { requester: true, owners: true, watchers_group_sid: null, email_fallback: true },
      },
      state: "active",
      proposal_id: "703",
      approved_by: admin,
      approved_at: iso(now - 120 * DAY),
      note: null,
    },
  ];

  const datasets: Dataset[] = [
    {
      dataset_id: "1",
      name: "Seismic survey archive",
      backend_kind: "azure_blob",
      private_network: true,
      edge_id: "1",
      root_dir_id: "100",
      storage_layout: "two_account",
      migration_state: "steady",
      shares: [
        { share_id: "1", name: "long-term-storage", worm_enabled: false },
        { share_id: "2", name: "processing-scratch", worm_enabled: false },
      ],
      active_policy: { version: 4, mode: "scheduled", archive_day: 365 },
      pending_proposals: 2,
    },
    {
      dataset_id: "2",
      name: "Legal records",
      backend_kind: "azure_blob",
      private_network: true,
      edge_id: "1",
      root_dir_id: "300",
      storage_layout: "single_account",
      migration_state: "steady",
      shares: [{ share_id: "3", name: "contracts-worm", worm_enabled: true }],
      active_policy: { version: 2, mode: "scheduled", archive_day: 180 },
      pending_proposals: 0,
    },
  ];

  // On an edge, with no share yet: what "Add a share" offers as an existing dataset.
  datasets.push({
    dataset_id: "4",
    name: "gravity-surveys",
    backend_id: "1",
    backend_kind: "azure_blob",
    private_network: true,
    edge_id: "1",
    key_prefix: "",
    root_dir_id: "500",
    storage_layout: "two_account",
    migration_state: "steady",
    shares: [],
    active_policy: null,
    pending_proposals: 0,
  });

  // GET /v1/backends: S3 is a migration source, never a dataset's home (decision 18).
  const backends: Backend[] = [
    ...examples.Backend,
    {
      backend_id: "2",
      name: "azure-weu-worm",
      kind: "azure_blob",
      private_network: true,
      can_be_home: true,
    },
    {
      backend_id: "7",
      name: "aws-us-east-1",
      kind: "s3",
      private_network: true,
      can_be_home: false,
    },
  ];

  // Decision 35: a backend this portal version doesn't know (an on-premises NAS),
  // already handed off. Every open-enum value below is unknown to the SPA on purpose.
  shares.push({
    ...example.Share,
    share_id: "4",
    name: "field-ontap",
    dataset_id: "3",
    dataset_name: "Field archive (ONTAP)",
    edge_id: "11",
    root_dir_id: "400",
    protocols: ["nfs4"],
    nfs_sec: ["krb5"],
    groups: { owners: group(SID.seismicOwners), writers: null, readers: group(SID.dataReaders) },
    on_open: "fail",
    wait_threshold_minutes: null,
    quiet_s: null,
    quiet_default_s: 10,
    quiet_supported: true,
    edge_name: "edge-houston-1",
    rewind: { depth_days: 14, max_days: 30 },
    checks: [],
    etag: '"3"',
  });
  datasets.push({
    ...example.Dataset,
    dataset_id: "3",
    name: "Field archive (ONTAP)",
    backend_kind: "netapp_ontap",
    private_network: false,
    storage_layout: "ontap_fabricpool",
    migration_state: "handed_off",
    shares: [{ share_id: "4", name: "field-ontap", worm_enabled: false }],
    active_policy: null,
    pending_proposals: 0,
  });

  const files = new Map<string, MockFile>();
  let versionSeq = 50_000;
  const add = (
    f: Omit<MockFile, "versions" | "legal_hold" | "retention_until" | "deleted_at"> &
      Partial<Pick<MockFile, "legal_hold" | "retention_until" | "deleted_at">> & {
        history?: { daysAgo: number; size: number; tierAt?: TierState; kind?: Version["kind"] }[];
      },
  ) => {
    const history = f.history ?? [
      { daysAgo: (now - new Date(f.created_at).getTime()) / DAY, size: f.size },
    ];
    const versions: Version[] = history
      .map((h, i) => {
        versionSeq += 1;
        const committed = now - h.daysAgo * DAY;
        const next = history[i + 1];
        return {
          version_id: String(versionSeq),
          seq: i + 1,
          kind: h.kind ?? "content",
          state: "committed" as const,
          content_version_id: String(versionSeq),
          name: f.name,
          parent_id: f.parent_id,
          committed_at: iso(committed),
          superseded_at: next ? iso(now - next.daysAgo * DAY) : null,
          size: h.size,
          mtime_ns: String(committed * 1_000_000),
          tier: h.tierAt ?? f.tier,
          retention_until: f.retention_until ?? null,
          legal_hold: f.legal_hold ?? false,
          restorable: (h.kind ?? "content") !== "tombstone",
        };
      })
      .reverse();
    const { history: _history, ...rest } = f;
    files.set(f.file_id, {
      legal_hold: false,
      retention_until: null,
      deleted_at: null,
      ...rest,
      versions,
    });
  };

  const dir = (file_id: string, parent_id: string | null, share_id: string, name: string) =>
    add({
      file_id,
      parent_id,
      share_id,
      name,
      kind: "dir",
      size: 0,
      state: "resident",
      tier: tier("hot", "Hot"),
      archive_at: null,
      next_action: null,
      next_action_at: null,
      created_at: iso(now - 400 * DAY),
      last_read_at: null,
    });

  dir("100", null, "1", "long-term-storage");
  dir("101", "100", "1", "dataset");
  dir("102", "100", "1", "reports");
  dir("200", null, "2", "processing-scratch");
  dir("300", null, "3", "contracts-worm");
  dir("400", null, "4", "field-ontap");
  add({
    file_id: "4001",
    parent_id: "400",
    share_id: "4",
    name: "core_scan_0007.tif",
    kind: "file",
    size: 2_400_000_000,
    state: "tiered",
    tier: tier("capacity", "FabricPool capacity tier", { restore_state: "ontap_cold_read" }),
    archive_at: null,
    next_action: null,
    next_action_at: null,
    created_at: iso(now - 90 * DAY),
    last_read_at: iso(now - 80 * DAY),
  });

  add({
    file_id: "103",
    parent_id: "100",
    share_id: "1",
    name: "README.txt",
    kind: "file",
    size: 4_812,
    state: "resident",
    tier: tier("hot", "Hot"),
    archive_at: date(now + 340 * DAY),
    next_action: "tier_warm",
    next_action_at: iso(now + 5 * DAY),
    created_at: iso(now - 25 * DAY),
    last_read_at: iso(now - 25 * DAY),
  });
  add({
    file_id: "1001",
    parent_id: "101",
    share_id: "1",
    name: "line_1042.segy",
    kind: "file",
    size: 184_000_000_000,
    state: "stub",
    tier: tier("archive", "Archive", { tier_changed_at: iso(now - 45 * DAY) }),
    archive_at: date(now - 45 * DAY),
    next_action: null,
    next_action_at: null,
    created_at: iso(now - 410 * DAY),
    last_read_at: iso(now - 410 * DAY),
  });
  add({
    file_id: "1002",
    parent_id: "101",
    share_id: "1",
    name: "line_1043.segy",
    kind: "file",
    size: 8_200_000_000,
    state: "stub",
    tier: tier("archive", "Archive", { tier_changed_at: iso(now - 12 * DAY) }),
    archive_at: date(now - 12 * DAY),
    next_action: null,
    next_action_at: null,
    created_at: iso(now - 377 * DAY),
    last_read_at: iso(now - 377 * DAY),
  });
  add({
    file_id: "1003",
    parent_id: "101",
    share_id: "1",
    name: "line_1044.segy",
    kind: "file",
    size: 2_100_000_000,
    state: "resident",
    tier: tier("hot", "Hot", { explicit_tier_set: true, tier_changed_at: iso(now - 3 * DAY) }),
    archive_at: date(now + 300 * DAY),
    next_action: null,
    next_action_at: null,
    created_at: iso(now - 60 * DAY),
    last_read_at: iso(now - 1 * DAY),
    history: [
      { daysAgo: 60, size: 2_050_000_000, tierAt: tier("warm", "Cool") },
      { daysAgo: 18, size: 2_080_000_000, tierAt: tier("warm", "Cool") },
      { daysAgo: 4, size: 2_100_000_000 },
    ],
  });
  add({
    file_id: "1004",
    parent_id: "101",
    share_id: "1",
    name: "line_1045.segy",
    kind: "file",
    size: 61_000_000_000,
    state: "resident",
    tier: tier("cold", "Cold", { tier_changed_at: iso(now - 20 * DAY) }),
    archive_at: date(now + 225 * DAY),
    next_action: "archive",
    next_action_at: iso(now + 225 * DAY),
    created_at: iso(now - 140 * DAY),
    last_read_at: iso(now - 140 * DAY),
  });
  add({
    file_id: "1005",
    parent_id: "101",
    share_id: "1",
    name: "nav_2026.p190",
    kind: "file",
    size: 14_300_000,
    state: "resident",
    tier: tier("warm", "Cool", { tier_changed_at: iso(now - 9 * DAY) }),
    archive_at: date(now + 326 * DAY),
    next_action: "tier_cold",
    next_action_at: iso(now + 81 * DAY),
    created_at: iso(now - 39 * DAY),
    last_read_at: iso(now - 39 * DAY),
  });
  add({
    file_id: "1006",
    parent_id: "101",
    share_id: "1",
    name: "survey_notes.docx",
    kind: "file",
    size: 220_400,
    state: "resident",
    tier: tier("hot", "Hot"),
    archive_at: date(now + 362 * DAY),
    next_action: "tier_warm",
    next_action_at: iso(now + 27 * DAY),
    created_at: iso(now - 26 * DAY),
    last_read_at: iso(now - 3 * DAY),
    history: [
      { daysAgo: 26, size: 180_100 },
      { daysAgo: 14, size: 201_900 },
      { daysAgo: 9, size: 201_900, kind: "metadata" },
      { daysAgo: 3, size: 220_400 },
    ],
  });
  add({
    file_id: "1007",
    parent_id: "101",
    share_id: "1",
    name: "line_0999.segy",
    kind: "file",
    size: 95_000_000_000,
    state: "stub",
    tier: tier("archive", "Archive", { tier_changed_at: iso(now - 120 * DAY) }),
    archive_at: date(now - 120 * DAY),
    next_action: null,
    next_action_at: null,
    created_at: iso(now - 520 * DAY),
    last_read_at: iso(now - 520 * DAY),
    legal_hold: true,
  });
  add({
    file_id: "1008",
    parent_id: "101",
    share_id: "1",
    name: "line_1040.segy",
    kind: "file",
    size: 6_400_000_000,
    state: "recalling",
    tier: tier("archive", "Archive", {
      restore_state: "in_progress",
      tier_changed_at: iso(now - 30 * DAY),
    }),
    archive_at: date(now - 30 * DAY),
    next_action: null,
    next_action_at: null,
    created_at: iso(now - 395 * DAY),
    last_read_at: iso(now - 2 * 3600_000),
  });
  add({
    file_id: "1009",
    parent_id: "101",
    share_id: "1",
    name: "line_0950.segy",
    kind: "file",
    size: 12_000_000_000,
    state: "stub",
    tier: tier("cold", "Cold"),
    archive_at: null,
    next_action: null,
    next_action_at: null,
    created_at: iso(now - 200 * DAY),
    last_read_at: null,
    deleted_at: iso(now - 6 * DAY),
    history: [
      { daysAgo: 200, size: 12_000_000_000 },
      { daysAgo: 6, size: 0, kind: "tombstone" },
    ],
  });
  add({
    file_id: "1101",
    parent_id: "102",
    share_id: "1",
    name: "qc_report_2026Q3.pdf",
    kind: "file",
    size: 18_400_000,
    state: "resident",
    tier: tier("hot", "Hot"),
    archive_at: date(now + 355 * DAY),
    next_action: "tier_warm",
    next_action_at: iso(now + 20 * DAY),
    created_at: iso(now - 10 * DAY),
    last_read_at: iso(now - 10 * DAY),
  });
  add({
    file_id: "1102",
    parent_id: "102",
    share_id: "1",
    name: "acquisition_log_2025.xlsx",
    kind: "file",
    size: 3_200_000,
    state: "resident",
    tier: tier("archive", "Archive", {
      archived: false,
      restore_state: "completed",
      restore_expires_at: iso(now + 6 * DAY),
    }),
    archive_at: date(now - 60 * DAY),
    next_action: "stub",
    next_action_at: iso(now + 1 * DAY),
    created_at: iso(now - 425 * DAY),
    last_read_at: iso(now - 1 * DAY),
  });
  add({
    file_id: "2001",
    parent_id: "200",
    share_id: "2",
    name: "job_8812_migration.out",
    kind: "file",
    size: 740_000_000,
    state: "resident",
    tier: tier("hot", "Hot"),
    archive_at: date(now + 360 * DAY),
    next_action: "tier_warm",
    next_action_at: iso(now + 25 * DAY),
    created_at: iso(now - 5 * DAY),
    last_read_at: iso(now - 5 * DAY),
  });
  add({
    file_id: "3001",
    parent_id: "300",
    share_id: "3",
    name: "MSA-Acme-Drilling-2019.pdf",
    kind: "file",
    size: 2_800_000,
    state: "stub",
    tier: tier("archive", "Archive"),
    archive_at: date(now - 500 * DAY),
    next_action: null,
    next_action_at: null,
    created_at: iso(now - 680 * DAY),
    last_read_at: iso(now - 680 * DAY),
    retention_until: iso(now + 1875 * DAY),
    legal_hold: true,
  });
  add({
    file_id: "3002",
    parent_id: "300",
    share_id: "3",
    name: "NDA-Halden-Geo-2026.pdf",
    kind: "file",
    size: 410_000,
    state: "resident",
    tier: tier("hot", "Hot"),
    archive_at: date(now + 150 * DAY),
    next_action: "tier_warm",
    next_action_at: iso(now + 0.5 * DAY),
    created_at: iso(now - 30 * DAY),
    last_read_at: iso(now - 30 * DAY),
    retention_until: iso(now + 2525 * DAY),
  });

  const recall = (
    recall_id: string,
    file_id: string,
    state: RecallState,
    hoursAgo: number,
    extra: Partial<MockRecall["recall"]> = {},
    log: { state: RecallState; hoursAgo: number }[] = [],
  ): MockRecall => {
    const file = files.get(file_id);
    return {
      recall: {
        recall_id,
        file_id,
        version_id: file?.versions[0]?.version_id ?? "0",
        name: file?.name ?? null,
        share_id: file?.share_id ?? null,
        state,
        priority: "standard",
        eta: null,
        eta_confidence: "none",
        due_by: null,
        requested_at: iso(now - hoursAgo * 3600_000),
        completed_at: null,
        failure: null,
        requesters_count: 1,
        ...extra,
      },
      log: [
        { state: "requested", at: iso(now - hoursAgo * 3600_000) },
        ...log.map((l) => ({ state: l.state, at: iso(now - l.hoursAgo * 3600_000) })),
      ],
      simulate: false,
      requested_by: "reader",
    };
  };

  const recalls: MockRecall[] = [
    recall("501", "1001", "rehydrating", 3, { requesters_count: 2 }, [
      { state: "rehydrating", hoursAgo: 2.9 },
    ]),
    recall(
      "502",
      "1008",
      "downloading",
      9,
      { eta: iso(now + 20 * 60_000), eta_confidence: "provider_sla", priority: "high" },
      [
        { state: "rehydrating", hoursAgo: 8.9 },
        { state: "copying", hoursAgo: 1.5 },
        { state: "downloading", hoursAgo: 0.4 },
      ],
    ),
    recall("503", "1102", "available", 30, { completed_at: iso(now - 20 * 3600_000) }, [
      { state: "rehydrating", hoursAgo: 29.9 },
      { state: "downloading", hoursAgo: 21 },
      { state: "available", hoursAgo: 20 },
    ]),
    recall(
      "504",
      "1002",
      "failed",
      50,
      {
        failure:
          "The storage provider couldn't finish the restore. Nothing was lost; you can recall the file again.",
        completed_at: iso(now - 40 * 3600_000),
      },
      [
        { state: "rehydrating", hoursAgo: 49.9 },
        { state: "failed", hoursAgo: 40 },
      ],
    ),
  ];

  recalls.push(recall("505", "4001", "requested", 1, { priority: "bulk" }));
  // Its file was replaced while it was coming back: stopped, not failed.
  recalls.push(
    recall(
      "506",
      "1002",
      "superseded",
      30,
      {
        completed_at: iso(now - 26 * 3600_000),
        superseded: {
          reason: "replaced",
          text: "The file was replaced while it was being recalled.",
          by: null,
        },
      },
      [
        { state: "rehydrating", hoursAgo: 29.9 },
        { state: "superseded", hoursAgo: 26 },
      ],
    ),
  );

  const personRef = (who: Persona) => ({
    kind: "identity" as const,
    display_name: users[who].display_name ?? null,
    app_id: null,
    identity_id: users[who].identity_id,
  });

  const pins: Pin[] = [
    {
      pin_id: "801",
      file_id: "1003",
      name: "line_1044.segy",
      kind: "file",
      tier: "hot",
      until: iso(now + 20 * DAY),
      override_archive: false,
      reason: "Reprocessing run for the 2026 velocity model",
      set_by: personRef("steward"),
      set_at: iso(now - 3 * DAY),
      released_at: null,
    },
  ];

  const holds: Hold[] = [
    {
      hold_id: "851",
      file_id: "1006",
      name: "survey_notes.docx",
      kind: "keep_online",
      until: iso(now + 10 * DAY),
      reason: "Kept online from Teams",
      set_by: personRef("reader"),
      set_at: iso(now - 4 * DAY),
      released_at: null,
    },
    {
      hold_id: "852",
      file_id: "1007",
      name: "line_0999.segy",
      kind: "legal",
      until: null,
      reason: "Matter 2026-114: licence-round dispute",
      set_by: personRef("admin"),
      set_at: iso(now - 90 * DAY),
      released_at: null,
    },
    {
      hold_id: "853",
      file_id: "3001",
      name: "MSA-Acme-Drilling-2019.pdf",
      kind: "legal",
      until: null,
      reason: "Litigation hold, Acme v. Contoso",
      set_by: personRef("admin"),
      set_at: iso(now - 300 * DAY),
      released_at: null,
    },
  ];

  // An API client (Settings, API clients; portal-api.md 5.21) and its owner.
  const apiApp = {
    kind: "application" as const,
    display_name: null,
    app_id: "5c7e1b2a-4d3c-4b2a-9e8f-7a6b5c4d3e2f",
    identity_id: null,
  };
  const apiClient = {
    api_client_id: "3",
    display_name: "Migration planner",
    app_id: apiApp.app_id,
    owner: personRef("steward"),
  };
  const proposals: MockProposal[] = [
    {
      proposer: "app",
      proposal: {
        proposal_id: "701",
        dataset_id: "1",
        base_version: 4,
        stale: false,
        body: { ...scheduled, cold_day: 90, archive_day: 270 },
        rationale:
          "Read-pattern analysis of 18,400 SEG-Y lines: 92% are not read again after 75 days, and none after 240. Archiving at 270 days instead of 365 saves about 31% of first-year storage per TB.",
        proposed_by: apiApp,
        api_client: apiClient,
        state: "pending",
        decided_by: null,
        decided_at: null,
        decision_note: null,
        created_at: iso(now - 2 * DAY),
        share_overrides: [{ share_id: "2", on_open: "fail" }],
      },
    },
    {
      proposer: "steward",
      proposal: {
        proposal_id: "702",
        dataset_id: "1",
        base_version: 3,
        stale: true,
        body: {
          ...scheduled,
          cold_day: 90,
          notify: {
            requester: true,
            owners: true,
            watchers_group_sid: SID.archiveWatchers,
            email_fallback: true,
          },
        },
        rationale: "Tell the share Owners and the archive watchers group about every recall.",
        proposed_by: personRef("steward"),
        state: "pending",
        decided_by: null,
        decided_at: null,
        decision_note: null,
        created_at: iso(now - 30 * DAY),
      },
    },
    {
      proposer: "app",
      proposal: {
        proposal_id: "703",
        dataset_id: "2",
        base_version: 1,
        stale: false,
        body: policies[2]?.body ?? scheduled,
        rationale: "WORM share: tier in place, archive at 180 days from the write.",
        proposed_by: apiApp,
        api_client: apiClient,
        state: "approved",
        decided_by: admin,
        decided_at: iso(now - 120 * DAY),
        decision_note: "Matches the records schedule.",
        created_at: iso(now - 122 * DAY),
      },
    },
    {
      proposer: "app",
      proposal: {
        proposal_id: "704",
        dataset_id: "1",
        base_version: 3,
        stale: false,
        body: {
          mode: "smart",
          clock: "written",
          archive_day: 180,
          stub_at: "archive",
          resident_mb: 4,
          restub_after_hours: 24,
        },
        rationale: "Let Smart tier handle hot/cool/cold, archive at 180 days.",
        proposed_by: apiApp,
        api_client: apiClient,
        state: "rejected",
        decided_by: admin,
        decided_at: iso(now - 60 * DAY),
        decision_note: "Too early: crews re-read lines for up to eight months.",
        created_at: iso(now - 64 * DAY),
      },
    },
  ];

  const audit: AuditRecord[] = [];
  const auditRow = (
    hoursAgo: number,
    actor: Persona | "teams" | "edge" | "system" | "app",
    action: string,
    decision: AuditRecord["decision"],
    extra: Partial<AuditRecord> = {},
  ) => {
    const who =
      actor === "edge"
        ? { actor: "edge:11", actor_display_name: "edge-hou-01" }
        : actor === "system"
          ? { actor: "system:scheduler", actor_display_name: "Tiering scheduler" }
          : actor === "app"
            ? {
                actor: "app:5c7e1b2a-4d3c-4b2a-9e8f-7a6b5c4d3e2f",
                actor_display_name: "API · Migration planner",
              }
            : actor === "teams"
              ? { actor: "user:9003", actor_display_name: "Riley Moreno" }
              : {
                  actor: `user:${users[actor].identity_id}`,
                  actor_display_name: users[actor].display_name ?? null,
                  actor_sid: users[actor].sid ?? null,
                };
    audit.push({
      audit_id: String(60_000 + audit.length),
      at: iso(now - hoursAgo * 3600_000),
      proof: actor === "edge" ? "claimed" : "proven",
      trust_level: "proven",
      decision,
      action,
      dataset_id: "1",
      share_id: "1",
      file_id: null,
      version_id: null,
      client_addr: actor === "system" || actor === "app" ? null : "10.40.2.17",
      details: {},
      ...who,
      ...extra,
    });
  };
  auditRow(0.4, "edge", "recall.start", "allow", {
    file_id: "1008",
    details: { via: "nfs", on_open: "auto" },
  });
  auditRow(2.9, "reader", "recall.request", "allow", {
    file_id: "1001",
    details: { via: "portal", priority: "standard" },
  });
  auditRow(3.0, "teams", "teams.keep_online", "allow", { file_id: "1006", details: { days: 14 } });
  auditRow(5, "reader", "file.read", "deny", {
    file_id: null,
    details: { path_hidden: true, reason: "acl" },
    share_id: "1",
  });
  auditRow(26, "steward", "pin.create", "allow", {
    file_id: "1003",
    details: { tier: "hot", until_days: 20 },
  });
  auditRow(48, "app", "policy.propose", "info", { details: { proposal_id: "701" } });
  auditRow(70, "system", "tier.change", "info", {
    file_id: "1002",
    details: { from: "Cold", to: "Archive" },
  });
  auditRow(120, "admin", "share.settings", "allow", {
    details: { on_open: ["wait", "auto"], wait_threshold_minutes: 30 },
  });
  auditRow(504, "admin", "policy.create", "allow", { details: { version: 4 } });
  auditRow(2160, "admin", "hold.legal.set", "allow", {
    file_id: "1007",
    details: { hold_id: "852" },
  });
  auditRow(4800, "admin", "worm.lock", "info", {
    share_id: "3",
    dataset_id: "2",
    details: { retention_days: 2555 },
  });

  // The YAML's AuditRecord example, as one more (older) row.
  audit.push({ ...example.AuditRecord, at: iso(now - 6000 * 3600_000) });

  return {
    now,
    persona,
    users,
    shares,
    datasets,
    backends,
    files,
    recalls,
    pins,
    holds,
    policies,
    proposals,
    groups,
    audit,
    restores: new Map(),
    downloads: new Map(),
    sharePaths: new Map(),
    migration: createMigrationMock(datasets),
    nextId: 100_000,
    authTime: now - 2 * 60_000,
  };
}
