/**
 * A stand-in for the server's policy preview (policy/preview.py), for the mock
 * only. The real numbers and checks come from the service; this gives the
 * editor realistic, moving data while the Python tracks build the real one.
 */
import type {
  Check,
  CostAssumptions,
  Dataset,
  LogicalTier,
  Money,
  PolicyBody,
  PolicyPreview,
  Share,
  ShareOverride,
  TimelineSegment,
} from "../api/types";

const GB_MONTH: Record<string, number> = {
  Hot: 0.0184,
  Cool: 0.01,
  Cold: 0.0036,
  Archive: 0.00099,
};
const NATIVE: Record<Exclude<LogicalTier, "smart">, string> = {
  hot: "Hot",
  warm: "Cool",
  cold: "Cold",
  archive: "Archive",
};
const MIN_DAYS: Record<string, number> = { Cool: 30, Cold: 90, Archive: 180 };
const DAYS_PER_MONTH = 30.4;

const usd = (amount: number, high?: number): Money => ({
  amount: amount.toFixed(2),
  currency: "USD",
  ...(high !== undefined ? { high: high.toFixed(2) } : {}),
});

const check = (
  severity: Check["severity"],
  code: string,
  message: string,
  field: string | null = null,
  extra: Partial<Check> = {},
): Check => ({ severity, code, message, field, share_id: null, fee: null, ...extra });

export function mockPolicyPreview(
  body: PolicyBody,
  dataset: Dataset,
  shares: Share[],
  overrides: ShareOverride[] = [],
  assumptions: CostAssumptions = {},
): PolicyPreview {
  const tb = assumptions.tb ?? 1;
  const gb = tb * 1000;
  const checks: Check[] = [];
  const mode = body.mode ?? "scheduled";
  const smart = mode === "smart";
  const archiveDay = body.archive_day;
  const warm = smart ? null : (body.warm_day ?? null);
  const cold = smart ? null : (body.cold_day ?? null);
  const worm = shares.some((s) => s.worm.enabled);
  const twoAccount = dataset.storage_layout === "two_account" && !worm;
  // A backend this mock (and the SPA) doesn't know: echo its own terms back.
  const foreign = !["azure_blob", "s3", "sim"].includes(dataset.backend_kind);
  const single = foreign ? dataset.storage_layout : "single";

  if (archiveDay === undefined) {
    checks.push(
      check("error", "archive_day_required", "Choose the day files go to Archive.", "archive_day"),
    );
  }
  if (smart && (body.warm_day != null || body.cold_day != null)) {
    checks.push(
      check(
        "error",
        "smart_has_no_warm_cold",
        "With Smart, Azure moves files among Hot, Cool and Cold itself, so there are no Cool or Cold days to set.",
        body.warm_day != null ? "warm_day" : "cold_day",
      ),
    );
  }
  const days = [
    ["warm_day", warm],
    ["cold_day", cold],
    ["archive_day", archiveDay ?? null],
  ].filter((d): d is [string, number] => d[1] !== null);
  for (let i = 1; i < days.length; i++) {
    const prev = days[i - 1];
    const cur = days[i];
    if (prev && cur && cur[1] <= prev[1]) {
      checks.push(
        check(
          "error",
          "days_not_increasing",
          `Day ${cur[1]} must come after day ${prev[1]}: each move has to be later than the one before.`,
          cur[0],
        ),
      );
    }
  }
  if (smart && worm) {
    checks.push(
      check(
        "error",
        "smart_on_worm",
        "This dataset has a WORM share. WORM shares tier in place and can't use Smart yet.",
        "mode",
      ),
    );
  } else if (smart && dataset.storage_layout !== "two_account") {
    checks.push(
      check(
        "error",
        "smart_archive_needs_two_accounts",
        "Smart with an archive day needs the two-account layout (a ZRS live account and an archive account). This dataset has one account.",
        "mode",
      ),
    );
  }

  const valid = !checks.some((c) => c.severity === "error");
  const archive = archiveDay ?? 365;
  const horizon = Math.max(400, archive + 30);
  const stubAt = body.stub_at ?? "archive";
  const local = (tier: LogicalTier): TimelineSegment["local"] => {
    if (stubAt === "never") return "resident";
    if (stubAt === "archive") return tier === "archive" ? "stub" : "resident";
    return tier === "hot" || tier === "smart" ? "resident" : "stub";
  };

  const segments: TimelineSegment[] = [];
  if (smart) {
    segments.push({
      from_day: 0,
      to_day: archive,
      logical_tier: "smart",
      native_class: "Smart",
      account: "live",
      local: local("smart"),
      note: "Azure moves each file among Hot, Cool and Cold by use",
    });
  } else {
    const stops: [LogicalTier, number][] = [["hot", 0]];
    if (warm !== null) stops.push(["warm", warm]);
    if (cold !== null) stops.push(["cold", cold]);
    stops.forEach(([tier, from], i) => {
      const to = stops[i + 1]?.[1] ?? archive;
      if (to <= from) return;
      segments.push({
        from_day: from,
        to_day: to,
        logical_tier: tier,
        native_class: NATIVE[tier],
        account: twoAccount ? "live" : single,
        local: local(tier),
        note: null,
      });
    });
  }
  segments.push({
    from_day: archive,
    to_day: null,
    logical_tier: "archive",
    native_class: "Archive",
    account: twoAccount ? "archive" : single,
    local: local("archive"),
    note: twoAccount ? "Copied to the archive account, read back, then archived" : null,
  });

  // First-year storage, per TB.
  let storage = 0;
  let storageHigh = 0;
  for (const seg of segments) {
    const from = Math.min(seg.from_day, 365);
    const to = Math.min(seg.to_day ?? 365, 365);
    if (to <= from) continue;
    const months = (to - from) / DAYS_PER_MONTH;
    if (seg.logical_tier === "smart") {
      storage += months * (GB_MONTH.Cold ?? 0) * gb;
      storageHigh += months * (GB_MONTH.Hot ?? 0) * gb;
    } else {
      const price = GB_MONTH[seg.native_class ?? "Hot"] ?? 0;
      storage += months * price * gb;
      storageHigh += months * price * gb;
    }
  }
  const objects = (tb * 1e12) / (assumptions.mean_file_bytes ?? 1_073_741_824);
  const transitions = ((smart ? 1 : segments.length - 1) * objects * 0.1) / 10_000;
  const archiveCopy = twoAccount ? (objects * 0.1) / 10_000 : 0;
  const readBack =
    twoAccount && archive < 365 ? (smart ? 0.01 : cold !== null ? 0.02 : 0.01) * gb : 0;
  let earlyDeletion = 0;
  const breakdown: PolicyPreview["cost"]["breakdown"] = [];

  const minimum = (tier: string, stayed: number, field: string) => {
    const min = MIN_DAYS[tier] ?? 0;
    if (stayed >= min) return;
    const fee = ((min - stayed) / DAYS_PER_MONTH) * (GB_MONTH[tier] ?? 0) * gb;
    earlyDeletion += fee;
    checks.push(
      check(
        "warning",
        "minimum_duration",
        `Files stay in ${tier} for ${stayed} days, but Azure bills ${tier} for at least ${min} days. Moving them earlier costs an early-deletion fee.`,
        field,
        { fee: usd(fee / tb) },
      ),
    );
  };
  if (!smart && warm !== null)
    minimum("Cool", (cold ?? archive) - warm, cold !== null ? "cold_day" : "archive_day");
  if (!smart && cold !== null) minimum("Cold", archive - cold, "archive_day");

  breakdown.push({
    item: "storage",
    tier: null,
    amount: smart ? usd(storage, storageHigh) : usd(storage),
    note: smart ? "Smart is a range: all of it cold to all of it hot" : null,
  });
  breakdown.push({ item: "transitions", tier: null, amount: usd(transitions), note: null });
  if (twoAccount) {
    breakdown.push({ item: "archive_copy", tier: "archive", amount: usd(archiveCopy), note: null });
    breakdown.push({
      item: "read_back",
      tier: "archive",
      amount: usd(readBack),
      note: "Reading the copy back to verify it",
    });
  }
  if (earlyDeletion > 0) {
    breakdown.push({ item: "early_deletion", tier: null, amount: usd(earlyDeletion), note: null });
  }
  if (foreign) {
    breakdown.push({
      item: "ontap_tiering",
      tier: null,
      amount: usd(0),
      note: "Priced by the array",
    });
    checks.push(
      check(
        "warning",
        "price_missing",
        `The price table has no prices for ${dataset.backend_kind}; the cost leaves its classes out.`,
      ),
    );
  }
  if (smart) {
    breakdown.push({
      item: "smart_monitoring",
      tier: "smart",
      amount: usd((objects * 0.025 * 12) / 10_000),
      note: null,
    });
  }

  const extras = transitions + archiveCopy + readBack + earlyDeletion;
  const total = storage + extras;
  const totalHigh = storageHigh + extras;
  const keepHot = 12.0 * (GB_MONTH.Hot ?? 0) * gb * (365 / (12 * DAYS_PER_MONTH));

  if (archiveDay !== undefined) {
    checks.push(
      check(
        "warning",
        "archive_recall_time",
        "Archived files take hours to come back: up to 15 hours at Standard priority for files under 10 GB. Above 10 GB no time is promised.",
        "archive_day",
      ),
    );
  }
  if (smart) {
    checks.push(
      check(
        "info",
        "smart_clock_written",
        "With Smart, the archive day counts from when a file was written, not from its last read.",
        "clock",
      ),
    );
  }
  if (twoAccount) {
    checks.push(
      check(
        "info",
        "archive_copy_two_accounts",
        "On the archive day each file is copied to the archive account, read back to verify it, and then archived.",
        "archive_day",
      ),
    );
  }
  if (worm) {
    checks.push(
      check(
        "info",
        "worm_tiers_in_place",
        "WORM shares tier in place in one account, with Set Blob Tier. Smart isn't offered for them.",
        null,
      ),
    );
  }
  const notify = body.notify ?? {};
  if (notify.requester === false && notify.owners !== true && !notify.watchers_group_sid) {
    checks.push(
      check(
        "warning",
        "nobody_told",
        "Nobody will be told when an archived file is opened. People will only see the note file next to it.",
        "notify.requester",
      ),
    );
  }
  if (notify.email_fallback === false) {
    checks.push(
      check(
        "info",
        "no_email_fallback",
        "If a Teams message can't be delivered, nobody gets an email instead.",
        "notify.email_fallback",
      ),
    );
  }
  for (const share of shares) {
    const override = overrides.find((o) => o.share_id === share.share_id);
    const onOpen = override?.on_open ?? share.on_open;
    const threshold =
      override && "wait_threshold_minutes" in override
        ? override.wait_threshold_minutes
        : share.wait_threshold_minutes;
    if (onOpen === "wait") {
      checks.push(
        check(
          "warning",
          "wait_without_promise",
          `On ${share.name}, programs wait for archived files. For files over 10 GB there is no promised time, so a program may wait for many hours.`,
          "on_open",
          { share_id: share.share_id },
        ),
      );
    }
    if (onOpen === "auto" && (threshold ?? 0) < 60) {
      checks.push(
        check(
          "warning",
          "auto_threshold_below_eta",
          `On ${share.name}, programs wait only ${threshold ?? 0} minutes, but the fastest restore takes about an hour. Reads of archived files will always fail fast.`,
          "wait_threshold_minutes",
          { share_id: share.share_id },
        ),
      );
    }
    if (share.nfs_sec.some((s) => s === "krb5" || s === "krb5p")) {
      checks.push(
        check(
          "info",
          "kerberos_throughput",
          `${share.name} uses Kerberos. Microsoft measured about 53% fewer IOPS with krb5 and about 77% fewer with krb5p on NFS.`,
          null,
          { share_id: share.share_id },
        ),
      );
    }
    if (share.trust_level === "host_trusted") {
      checks.push(
        check(
          "warning",
          "host_trusted",
          `${share.name} uses sec=sys: the uid is whatever the client says.`,
          null,
          { share_id: share.share_id },
        ),
      );
    }
  }

  return {
    valid,
    body,
    timeline: {
      segments,
      archive_day: archiveDay ?? null,
      horizon_days: horizon,
      clock: body.clock ?? "last_read",
    },
    cost: {
      per_tb_first_year: smart ? usd(total / tb, totalHigh / tb) : usd(total / tb),
      keep_hot_per_tb_first_year: usd(keepHot / tb),
      saving: smart
        ? usd((keepHot - totalHigh) / tb, (keepHot - total) / tb)
        : usd((keepHot - total) / tb),
      breakdown: breakdown.map((b) => ({
        ...b,
        amount: b.amount
          ? {
              ...b.amount,
              amount: (Number(b.amount.amount) / tb).toFixed(2),
              ...(b.amount.high ? { high: (Number(b.amount.high) / tb).toFixed(2) } : {}),
            }
          : null,
      })),
      recall_per_tb: [
        { priority: "standard", per_tb: usd(20.48), hours: [1, 15] },
        { priority: "high", per_tb: usd(102.4), hours: [0.5, 1] },
      ],
      assumptions: {
        tb,
        mean_file_bytes: assumptions.mean_file_bytes ?? 1_073_741_824,
        recall_fraction_per_year: assumptions.recall_fraction_per_year ?? 0,
      },
      price_table: {
        id: "azure_blob/eastus/2026-09-26",
        backend_kind: dataset.backend_kind,
        region: "eastus",
        as_of: "2026-09-26",
        verify: true,
        overridden: false,
      },
    },
    checks,
  };
}
