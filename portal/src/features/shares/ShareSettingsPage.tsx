import { useId, useState } from "react";
import { Link, useParams } from "react-router";
import { authIsRecent, useSession } from "../../app/session";
import {
  useHolds,
  usePatchShareSettings,
  useReleaseHold,
  useShare,
  useShareSettingsPreview,
} from "../../api/queries";
import type { NfsSec, OnOpen, Share, ShareSettingsPatch } from "../../api/types";
import { usePageTitle } from "../../app/usePageTitle";
import { ChecksList } from "../../components/Checks";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { ShieldIcon } from "../../components/icons";
import { useToast } from "../../components/Toasts";
import { formatDateTime, formatRelative, pluralize } from "../../lib/format";
import { canEditOpenPolicy, canEditShareSecurity, isArchiveAdmin } from "../../lib/permissions";
import { useDebouncedJson } from "../../lib/useDebounced";
import { signIn } from "../../api/client";
import { RemoveShareCard } from "./RemoveShareCard";
import { QuietCard, QuietShortConfirm, quietOutOfRange, WORM_QUIET_S } from "./QuietCard";
import { LazyPart, lazyNamed } from "../../app/lazy";
import { WormCard } from "./WormCard";

// The map is its own chunk (app/lazy.tsx).
const ShareMap = lazyNamed(() => import("./visual/ShareMap"), "ShareMap");

const SEC: { value: NfsSec; title: string; body: string }[] = [
  {
    value: "sys",
    title: "sys",
    body: "No Kerberos. The NFS client says who the user is; only named hosts are trusted.",
  },
  { value: "krb5", title: "krb5", body: "Kerberos proves who the user is." },
  { value: "krb5i", title: "krb5i", body: "Kerberos, plus a checksum on every request." },
  { value: "krb5p", title: "krb5p", body: "Kerberos, plus encryption of all traffic." },
];

export const KERBEROS_NOTE =
  "Kerberos costs throughput: Microsoft measured about 53% fewer IOPS with krb5, and about 77% fewer with krb5p, than with sec=sys on NFS.";

interface Draft {
  on_open: OnOpen;
  threshold: string;
  depth: string;
  maxDepth: string;
  nfsSec: NfsSec[];
  clients: string;
  rootSquash: boolean;
  override: boolean;
  overrideReason: string;
  /** A quiet period of its own, rather than the share's default (decision 67). */
  quietOwn: boolean;
  quiet: string;
}

function draftOf(share: Share): Draft {
  return {
    on_open: share.on_open,
    threshold: String(share.wait_threshold_minutes ?? 15),
    depth: String(share.rewind.depth_days),
    maxDepth: String(share.rewind.max_days),
    nfsSec: [...share.nfs_sec],
    clients: (share.nfs_clients ?? []).join("\n"),
    rootSquash: share.root_squash ?? true,
    override: share.host_trust_override ?? false,
    overrideReason: "",
    quietOwn: share.quiet_s != null,
    quiet: String(share.quiet_s ?? share.quiet_default_s),
  };
}

function patchOf(share: Share, d: Draft): ShareSettingsPatch {
  const patch: ShareSettingsPatch = {};
  if (d.on_open !== share.on_open) patch.on_open = d.on_open;
  const threshold = d.on_open === "auto" ? Number(d.threshold) || null : null;
  if (d.on_open === "auto" && threshold !== (share.wait_threshold_minutes ?? null)) {
    patch.wait_threshold_minutes = threshold;
  }
  if (Number(d.depth) !== share.rewind.depth_days) patch.rewind_depth_days = Number(d.depth);
  const quiet = d.quietOwn ? Number(d.quiet) : null;
  // Out of range: not sent, and Save is off until it's fixed.
  if (quiet !== (share.quiet_s ?? null) && !(d.quietOwn && quietOutOfRange(d.quiet))) {
    patch.quiet_s = quiet;
  }
  if (Number(d.maxDepth) !== share.rewind.max_days)
    patch.rewind_depth_max_days = Number(d.maxDepth);
  const sec = [...d.nfsSec].sort();
  if (JSON.stringify(sec) !== JSON.stringify([...share.nfs_sec].sort())) patch.nfs_sec = sec;
  const clients = d.clients
    .split(/\n/)
    .map((c) => c.trim())
    .filter(Boolean);
  if (JSON.stringify(clients) !== JSON.stringify(share.nfs_clients ?? []))
    patch.nfs_clients = clients;
  if (d.rootSquash !== (share.root_squash ?? true)) patch.root_squash = d.rootSquash;
  if (d.override !== (share.host_trust_override ?? false)) {
    patch.host_trust_override = { enabled: d.override, reason: d.overrideReason.trim() };
  }
  return patch;
}

export function ShareSettingsPage() {
  const { shareId = "" } = useParams();
  const share = useShare(shareId);
  usePageTitle(share.data ? `${share.data.name} settings` : "Share settings");
  if (share.isPending) return <Loading />;
  if (share.isError) {
    return (
      <div className="page">
        <ErrorCallout error={share.error} />
      </div>
    );
  }
  return <ShareSettings key={share.data.etag} share={share.data} />;
}

function ShareSettings({ share }: { share: Share }) {
  const { me } = useSession();
  const toast = useToast();
  const patch = usePatchShareSettings(share.share_id);
  const [draft, setDraft] = useState<Draft>(() => draftOf(share));
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const openEditable = canEditOpenPolicy(me, share);
  const securityEditable = canEditShareSecurity(me);
  const changes = patchOf(share, draft);
  const count = Object.keys(changes).length;
  const debounced = useDebouncedJson(changes, 250);
  const preview = useShareSettingsPreview(share.share_id, count > 0 ? debounced : null);
  const checks = count > 0 && preview.data ? preview.data.checks : share.checks;
  const quietBad = draft.quietOwn && quietOutOfRange(draft.quiet);
  // Decision 67 allows a WORM share below its default, said in so many words.
  const quietShort =
    share.worm.enabled && changes.quiet_s != null && changes.quiet_s < WORM_QUIET_S;
  const [confirming, setConfirming] = useState(false);
  const save = (patchBody: ShareSettingsPatch) =>
    patch.mutate(
      { etag: share.etag, patch: patchBody },
      {
        onSuccess: () => toast(`Saved ${pluralize(count, "setting")} on ${share.name}.`),
      },
    );
  const kerberos = draft.nfsSec.some((s) => s.startsWith("krb5"));
  const needsReason =
    changes.host_trust_override !== undefined && draft.override && !draft.overrideReason.trim();
  const depthMax = Number(draft.maxDepth) || share.rewind.max_days;
  const thresholdId = useId();
  const depthId = useId();
  const maxId = useId();
  const clientsId = useId();
  const reasonId = useId();
  const proto = share.protocols
    .map((p) => (p === "nfs4" ? "NFSv4" : p === "nfs3" ? "NFSv3" : "SMB"))
    .join(", ");
  const nfs = share.protocols.some((p) => p.startsWith("nfs"));

  return (
    <div className="page">
      <nav aria-label="Breadcrumb">
        <ol className="crumbs">
          <li>
            <Link to="/shares">Shares</Link>
          </li>
          <li>
            <span aria-current="page">{share.name}</span>
          </li>
        </ol>
      </nav>
      <div className="page-head">
        <div>
          <h1>{share.name}</h1>
          <p className="page-question">What do people see on this share, and what is kept?</p>
          <p className="lede">
            {share.dataset_name} · {proto} ·{" "}
            {share.trust_level === "proven"
              ? "users proven by Kerberos or SMB"
              : "host-trusted (sec=sys)"}{" "}
            · ACLs last scanned {formatRelative(share.last_full_acl_scan_at)}
          </p>
        </div>
        <div className="actions">
          <Link className="btn" to={`/shares/${share.share_id}/access`}>
            Who has access
          </Link>
          {share.my_access.can_read_root ? (
            <Link className="btn" to={`/files/${share.share_id}/${share.root_dir_id}`}>
              Browse files
            </Link>
          ) : null}
        </div>
      </div>

      <LazyPart label="Loading the map of this share…">
        <ShareMap share={share} />
      </LazyPart>

      <div className="editor">
        <div className="editor-form">
          <form
            className="stack"
            style={{ gap: 16 }}
            onSubmit={(e) => {
              e.preventDefault();
              if (quietShort) setConfirming(true);
              else save(changes);
            }}
          >
            <section className="card" aria-labelledby="open-title">
              <div className="card-head">
                <h2 id="open-title">When someone opens an archived file</h2>
              </div>
              <div className="card-body stack">
                <fieldset className="options">
                  <legend className="sr-only">Open behaviour</legend>
                  {(
                    [
                      [
                        "fail",
                        "Tell them straight away",
                        "The open fails fast with a note. Best for people.",
                      ],
                      [
                        "wait",
                        "Make the program wait",
                        "The read waits until the file is back. Best for batch clusters.",
                      ],
                      [
                        "auto",
                        "Wait only if it's quick",
                        "Waits up to a limit, otherwise fails fast.",
                      ],
                    ] as const
                  ).map(([value, title, body]) => (
                    <label key={value} className="option">
                      <input
                        type="radio"
                        name="on_open"
                        checked={draft.on_open === value}
                        disabled={!openEditable}
                        onChange={() => set("on_open", value)}
                      />
                      <span className="option-title">{title}</span>
                      <span className="option-body">{body}</span>
                    </label>
                  ))}
                </fieldset>
                {draft.on_open === "auto" ? (
                  <div className="sentence">
                    <label htmlFor={thresholdId}>Wait at most</label>
                    <input
                      id={thresholdId}
                      className="inline-num"
                      type="number"
                      min={1}
                      max={1440}
                      disabled={!openEditable}
                      value={draft.threshold}
                      onChange={(e) => set("threshold", e.target.value)}
                    />
                    <span>minutes</span>
                  </div>
                ) : null}
                {!openEditable ? (
                  <p className="field-hint">
                    Only this share's Owners or an ArchiveAdmin can change this.
                  </p>
                ) : null}
              </div>
            </section>

            <section className="card" aria-labelledby="rewind-title">
              <div className="card-head">
                <h2 id="rewind-title">Rewind</h2>
              </div>
              <div className="card-body stack">
                <div className="sentence">
                  <label htmlFor={depthId}>Let people go back</label>
                  <input
                    id={depthId}
                    className="inline-num"
                    type="number"
                    min={0}
                    max={depthMax}
                    disabled={!openEditable}
                    value={draft.depth}
                    aria-invalid={Number(draft.depth) > depthMax}
                    onChange={(e) => set("depth", e.target.value)}
                  />
                  <span>days (at most {pluralize(depthMax, "day")}).</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={depthMax}
                  value={Math.min(Number(draft.depth) || 0, depthMax)}
                  disabled={!openEditable}
                  aria-label="Rewind depth in days"
                  onChange={(e) => set("depth", e.target.value)}
                />
                <p className="field-hint">
                  Older versions are pruned
                  {share.worm.enabled
                    ? ", except that WORM keeps every version until its retention ends"
                    : ""}
                  . Versions are billed at full size, so a longer rewind costs more storage.
                </p>
                {isArchiveAdmin(me) ? (
                  <div className="sentence">
                    <label htmlFor={maxId}>Owners may choose up to</label>
                    <input
                      id={maxId}
                      className="inline-num"
                      type="number"
                      min={0}
                      max={36500}
                      value={draft.maxDepth}
                      onChange={(e) => set("maxDepth", e.target.value)}
                    />
                    <span>days.</span>
                  </div>
                ) : null}
              </div>
            </section>

            <QuietCard
              share={share}
              editable={openEditable}
              own={draft.quietOwn}
              value={draft.quiet}
              onOwn={(v) => set("quietOwn", v)}
              onValue={(v) => set("quiet", v)}
            />

            {nfs ? (
              <section className="card" aria-labelledby="nfs-title">
                <div className="card-head">
                  <h2 id="nfs-title">NFS security</h2>
                  {!securityEditable ? <span className="muted">ArchiveAdmin only</span> : null}
                </div>
                <div className="card-body stack">
                  <fieldset className="options options-row">
                    <legend className="field-label">Allowed security flavours</legend>
                    {SEC.map((s) => (
                      <label key={s.value} className="option">
                        <input
                          type="checkbox"
                          disabled={!securityEditable}
                          checked={draft.nfsSec.includes(s.value)}
                          onChange={(e) =>
                            set(
                              "nfsSec",
                              e.target.checked
                                ? [...draft.nfsSec, s.value]
                                : draft.nfsSec.filter((v) => v !== s.value),
                            )
                          }
                        />
                        <span className="option-title mono">{s.title}</span>
                        <span className="option-body">{s.body}</span>
                      </label>
                    ))}
                  </fieldset>
                  {kerberos ? (
                    <Callout tone="info" title="Kerberos and throughput">
                      {KERBEROS_NOTE}
                    </Callout>
                  ) : null}
                  {draft.nfsSec.includes("sys") ? (
                    <>
                      <div className="field">
                        <label className="field-label" htmlFor={clientsId}>
                          Trusted client hosts
                        </label>
                        <textarea
                          id={clientsId}
                          className="mono"
                          disabled={!securityEditable}
                          value={draft.clients}
                          onChange={(e) => set("clients", e.target.value)}
                          placeholder={"10.20.0.0/16\nhpc-*.example.com"}
                        />
                        <span className="field-hint">
                          One per line. sys needs at least one, and root_squash on.
                        </span>
                      </div>
                      <label className="check-row">
                        <input
                          type="checkbox"
                          disabled={!securityEditable}
                          checked={draft.rootSquash}
                          onChange={(e) => set("rootSquash", e.target.checked)}
                        />
                        <span>root_squash (map root on clients to nobody)</span>
                      </label>
                      <label className="check-row">
                        <input
                          type="checkbox"
                          disabled={!securityEditable}
                          checked={draft.override}
                          onChange={(e) => set("override", e.target.checked)}
                        />
                        <span>
                          Allow host-trusted access even where folders carry restricted groups
                          <span className="field-hint">
                            Recorded with a reason; the portal shows it as a warning.
                          </span>
                        </span>
                      </label>
                      {changes.host_trust_override && draft.override ? (
                        <div className="field">
                          <label className="field-label" htmlFor={reasonId}>
                            Reason
                          </label>
                          <input
                            id={reasonId}
                            type="text"
                            value={draft.overrideReason}
                            maxLength={1000}
                            onChange={(e) => set("overrideReason", e.target.value)}
                          />
                        </div>
                      ) : null}
                    </>
                  ) : null}
                </div>
              </section>
            ) : null}

            <div className="save-bar" role="region" aria-label="Save settings">
              <strong className="spacer">
                {count ? pluralize(count, "unsaved change") : "No changes"}
              </strong>
              <button
                type="button"
                className="btn"
                disabled={!count}
                onClick={() => setDraft(draftOf(share))}
              >
                Reset
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={
                  !count ||
                  quietBad ||
                  patch.isPending ||
                  needsReason ||
                  checks.some((c) => c.severity === "error")
                }
              >
                {patch.isPending ? "Saving…" : "Save settings"}
              </button>
            </div>
            {patch.isError ? <ErrorCallout error={patch.error} title="Not saved" /> : null}
          </form>
          <QuietShortConfirm
            open={confirming}
            share={share}
            seconds={changes.quiet_s ?? 0}
            onCancel={() => setConfirming(false)}
            onConfirm={() => {
              setConfirming(false);
              save({ ...changes, quiet_s_confirmed: true });
            }}
          />
          {isArchiveAdmin(me) ? <WormCard share={share} /> : null}
          {isArchiveAdmin(me) ? <LegalHolds share={share} /> : null}
          {isArchiveAdmin(me) ? <RemoveShareCard share={share} /> : null}
        </div>

        <div className="editor-preview">
          <section
            className={`card card-pad stack${preview.isFetching ? " updating" : ""}`}
            aria-labelledby="share-checks"
          >
            <h2 id="share-checks">{count ? "Checks for your changes" : "Checks"}</h2>
            <div aria-live="polite">
              <ChecksList checks={checks} label="Share checks" />
            </div>
          </section>
          <section className="card card-pad stack">
            <h2>Your access</h2>
            <p>
              {share.my_access.owner
                ? "You're an Owner of this share."
                : share.my_access.writer
                  ? "You're a Writer on this share."
                  : share.my_access.reader
                    ? "You're a Reader on this share."
                    : "You have no share role here."}{" "}
              {isArchiveAdmin(me)
                ? "Your ArchiveAdmin role lets you manage its settings; only your AD groups let you read its files."
                : null}
            </p>
            <p className="muted">
              ACLs last scanned {formatDateTime(share.last_full_acl_scan_at)}.
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}

function LegalHolds({ share }: { share: Share }) {
  const { session } = useSession();
  const holds = useHolds({ shareId: share.share_id, kind: "legal" });
  const release = useReleaseHold();
  const toast = useToast();
  const recent = authIsRecent(session);
  const items = holds.data?.items ?? [];
  return (
    <section className="card" aria-labelledby="legal-title">
      <div className="card-head">
        <ShieldIcon />
        <h2 id="legal-title">Legal holds</h2>
      </div>
      <div className="card-body stack">
        <p className="muted">
          Set legal holds on files or folders from the file browser. A hold keeps every version past
          its retention until it's released here.
        </p>
        {holds.isPending ? (
          <Loading />
        ) : items.length === 0 ? (
          <p>No legal holds on files you can read.</p>
        ) : (
          <ul className="checks" aria-label="Legal holds">
            {items.map((h) => (
              <li key={h.hold_id} className="check" data-severity="info">
                <ShieldIcon />
                <span>
                  <strong>{h.name ?? `File ${h.file_id}`}</strong> · {h.reason}
                  <span className="field-hint">
                    Set by {h.set_by?.display_name ?? "—"} on {formatDateTime(h.set_at)}
                  </span>
                </span>
                {recent ? (
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={release.isPending}
                    onClick={() =>
                      release.mutate(h.hold_id, { onSuccess: () => toast("Legal hold released.") })
                    }
                  >
                    Release
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => signIn({ reauth: true })}
                  >
                    Sign in again to release
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        {release.isError ? <ErrorCallout error={release.error} /> : null}
      </div>
    </section>
  );
}
