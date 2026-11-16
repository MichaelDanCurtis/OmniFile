import { useEffect, useId, useState } from "react";
import { Link } from "react-router";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { Pills } from "../../components/PageHead";
import { useSession } from "../../app/session";
import { isArchiveAdmin } from "../../lib/permissions";
import { formatBytes, formatDate, formatRelative } from "../../lib/format";
import type { AnnexDetail } from "./api";
import { Flow } from "./RulesPage";
import {
  CHECK_LABEL,
  countText,
  reportOf,
  ruleDiff,
  rulesOf,
  useConfirmRemovals,
  useCreateDryRun,
  useDecideRuleset,
  useRemovals,
  useDryRun,
  useRuleset,
  useRulesets,
  type Ruleset,
} from "./rulesApi";

const STATE: Record<string, string> = {
  active: "Running",
  proposed: "Waiting for approval",
  superseded: "Replaced",
  rejected: "Rejected",
  withdrawn: "Withdrawn",
};

function Proposal({
  annex,
  etag,
  rs,
  onDone,
}: {
  annex: AnnexDetail;
  etag: string;
  rs: Ruleset;
  onDone: () => void;
}) {
  const { me } = useSession();
  const detail = useRuleset(annex.annex_id, rs.version);
  // A fresh count over today's listing when the proposal is opened (the one
  // made with it may be days old); the stored one if a fresh one can't be had.
  const fresh = useCreateDryRun(annex.annex_id);
  const body = detail.data?.body;
  const { mutate: count } = fresh;
  useEffect(() => {
    if (body) count(body);
  }, [body, count]);
  const runId = fresh.data?.run_id ?? rs.dry_run_id ?? null;
  const run = useDryRun(annex.annex_id, runId);
  const decide = useDecideRuleset(annex.annex_id);
  const report = reportOf(run.data);
  const rules = rulesOf(body);
  const running = detail.data?.running_body ? rulesOf(detail.data.running_body) : null;
  const diff = running ? ruleDiff(rules, running) : null;
  const canApprove = annex.can_manage && (!rs.needs_archive_admin || isArchiveAdmin(me));
  return (
    <li className="card card-pad">
      <h3>
        Version {rs.version}: {rs.mode === "custom" ? "custom rules" : rs.mode.replace("_", " ")}
      </h3>
      <p className="muted">
        Proposed {formatRelative(rs.created_at)} by {rs.owner_name ?? "a person"}
        {rs.proposed_by_app ? ` through ${rs.proposed_by_app}` : ""}.
        {rs.rationale ? ` “${rs.rationale}”` : ""}
      </p>
      {rs.removes ? (
        <p className="callout callout-warn">
          This removes files from the NAS.{" "}
          {rs.needs_archive_admin ? "Only an ArchiveAdmin approves it. " : ""}It runs as a dry run
          first, and removals start only when an ArchiveAdmin confirms the dry run.
        </p>
      ) : null}
      {(rs.replaces_on_nas ?? []).length > 0 ? (
        <p className="callout callout-warn" data-testid="replaces-on-nas">
          Replaces files on the NAS: rule {(rs.replaces_on_nas ?? []).join(", ")}. A file that comes
          back takes its own name in its original folder, replacing the file there when that file is
          exactly the copy OmniFile verified (the old one stays a version). The name is empty for
          the grace period while that happens.
        </p>
      ) : null}
      {diff && detail.data?.running_version ? (
        <div aria-label="Changes">
          <h4>Changes from version {detail.data.running_version}</h4>
          {diff.added.length + diff.removed.length + diff.changed.length + diff.moved.length ===
          0 ? (
            <p className="muted">The same rules.</p>
          ) : (
            <ul>
              {diff.added.map((r) => (
                <li key={`a-${r.id}`}>Adds rule {r.label ?? r.id}</li>
              ))}
              {diff.changed.map((r) => (
                <li key={`c-${r.id}`}>Changes rule {r.label ?? r.id}</li>
              ))}
              {diff.removed.map((r) => (
                <li key={`r-${r.id}`}>Drops rule {r.label ?? r.id}</li>
              ))}
              {diff.moved.map((r) => (
                <li key={`m-${r.id}`}>
                  Moves rule {r.label ?? r.id} (order decides which rule wins)
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
      {rules.length ? (
        <details open>
          <summary>The rules</summary>
          <Flow rules={rules} report={report} />
        </details>
      ) : null}
      <div aria-live="polite">
        {run.data?.report_hidden ? (
          <p className="muted">
            Its counts use conditions beyond paths in folders you can read: an ArchiveAdmin sees
            them.
          </p>
        ) : !report ? (
          <p className="muted">The dry run is being counted…</p>
        ) : (
          <p>
            Today: {countText(report.copied, formatBytes)} copied,{" "}
            {countText(report.excluded, formatBytes)} left on the NAS
            {report.removals && report.removals.files !== 0
              ? `, ${countText(report.removals, formatBytes)} leaving the NAS within a year${
                  report.removals.by_day?.[0]
                    ? `, from ${formatDate(report.removals.by_day[0].date)}`
                    : ""
                }`
              : ""}
            .
          </p>
        )}
      </div>
      {detail.data?.checks.length ? (
        <ul className="callout callout-warn" aria-label="Checks">
          {detail.data.checks.map((c) => (
            <li key={`${c.rule}-${c.check}`}>
              Rule {c.rule}: {CHECK_LABEL[c.check] ?? c.check}
            </li>
          ))}
        </ul>
      ) : null}
      {decide.isError ? <ErrorCallout error={decide.error} /> : null}
      {annex.can_manage ? (
        <div className="row-actions">
          {canApprove ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={decide.isPending}
              onClick={() =>
                decide.mutate({ version: rs.version, approve: true, etag }, { onSuccess: onDone })
              }
            >
              Approve version {rs.version}
            </button>
          ) : (
            <span className="muted">An ArchiveAdmin approves this one.</span>
          )}
          <button
            type="button"
            className="btn"
            disabled={decide.isPending}
            onClick={() =>
              decide.mutate({ version: rs.version, approve: false, etag }, { onSuccess: onDone })
            }
          >
            Reject
          </button>
        </div>
      ) : null}
    </li>
  );
}

const REMOVAL_STATE: Record<string, string> = {
  ticketed: "Waiting",
  hidden: "Hidden, in its grace period",
  deleting: "Being removed",
  deleted: "Removed",
  done: "Removed",
  cancelled: "Cancelled",
  stuck: "Left hidden: couldn't be put back (see Problems)",
};

/** A removal's state as a tag: what needs a person stands out. */
const STATE_TONE: Record<string, "ok" | "warn" | "signal" | undefined> = {
  hidden: "warn",
  deleting: "warn",
  deleted: "ok",
  done: "ok",
  stuck: "signal",
};

/** Removals: the end of the dry run (decision 59), and each removal. */
function Removals({
  annex,
  etag,
  onChanged,
}: {
  annex: AnnexDetail;
  etag: string;
  onChanged: () => void;
}) {
  const { me } = useSession();
  const rem = annex.removals;
  const [state, setState] = useState<"pending" | "done" | "cancelled" | "stuck" | "">("pending");
  const list = useRemovals(annex.annex_id, state);
  const confirm = useConfirmRemovals(annex.annex_id);
  const showId = useId();
  // Read once per render of the page: the dry run's end against it.
  const [now] = useState(() => Date.now());
  if (!rem) return null;
  const dryRunOver =
    rem.dry_run_until !== null && rem.dry_run_until !== undefined
      ? new Date(rem.dry_run_until).getTime() <= now
      : false;
  const needsConfirm =
    dryRunOver && !rem.enabled && rem.confirmed_version !== annex.ruleset_version;
  const would = list.data?.would_remove;
  return (
    <section className="card card-pad stack" aria-labelledby="annex-removals">
      <div className="row toolbar">
        <h2 id="annex-removals">Removals</h2>
        <span className="tag" data-tone={rem.enabled ? "ok" : needsConfirm ? "warn" : undefined}>
          {rem.enabled ? "Running" : needsConfirm ? "Waiting for an ArchiveAdmin" : "Not removing"}
        </span>
        <span className="spacer" />
        <Pills label="This list">
          <span className="pill">
            <label className="pill-key" htmlFor={showId}>
              Show
            </label>
            <select
              id={showId}
              value={state}
              onChange={(e) => setState(e.target.value as typeof state)}
              aria-label="Which removals"
            >
              <option value="pending">In progress</option>
              <option value="done">Removed</option>
              <option value="cancelled">Cancelled</option>
              <option value="stuck">Left hidden</option>
              <option value="">All</option>
            </select>
          </span>
        </Pills>
      </div>
      <p className="muted">
        {rem.enabled
          ? "Removals are running: each file leaves the NAS only after its copy is read back in Azure, hidden for its grace period first."
          : needsConfirm
            ? "The dry run is over. Nothing is removed until an ArchiveAdmin who reviewed it confirms."
            : "Nothing is removed from the NAS now: the annex records what its rules would remove."}
      </p>
      {needsConfirm && would ? (
        <div className="findings" aria-label="What the dry run would remove">
          <div className="findings-head">
            <h3>The dry run would remove</h3>
            <span className="findings-count">
              {would.files} files · {formatBytes(would.bytes)}
            </span>
            <span className="micro findings-note">From the NAS, once confirmed</span>
          </div>
          <p className="sr-only">
            {would.files} files ({formatBytes(would.bytes)}) from the NAS.
          </p>
          {would.samples.length ? (
            <ul className="findings-list" aria-label="Would remove">
              {would.samples.map((p, n) => (
                <li key={p} className="finding">
                  <span className="finding-rank" aria-hidden="true">
                    {String(n + 1).padStart(2, "0")}
                  </span>
                  <div className="finding-main">
                    <div className="finding-title mono">{p}</div>
                  </div>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {needsConfirm && isArchiveAdmin(me) ? (
        <div className="row-actions">
          <button
            type="button"
            className="btn btn-primary"
            disabled={confirm.isPending || !would}
            onClick={() => confirm.mutate(etag, { onSuccess: onChanged })}
          >
            Confirm removals for version {annex.ruleset_version}
          </button>
          <span className="micro">You'll be asked to sign in again if you haven't just now.</span>
        </div>
      ) : null}
      {confirm.isError ? <ErrorCallout error={confirm.error} /> : null}
      <dl className="kv">
        <dt>Paused</dt>
        <dd>{rem.paused ? "Yes" : "No"}</dd>
        <dt>Dry run until</dt>
        <dd>{rem.dry_run_until ? formatDate(rem.dry_run_until) : "No removing rules yet"}</dd>
        <dt>Rate</dt>
        <dd>{rem.rate_per_hour ?? 0} files an hour at most</dd>
        <dt>Grace period</dt>
        <dd>{rem.grace_minutes ?? 0} minutes</dd>
        <dt>Pointer files</dt>
        <dd>{rem.pointer_files ? "Left where each file was" : "None"}</dd>
      </dl>
      {list.data?.items.length ? (
        <div className="table-wrap">
          <table className="table table-dense" aria-label="Removals list">
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">State</th>
                <th scope="col">When</th>
                <th scope="col">Why cancelled</th>
              </tr>
            </thead>
            <tbody>
              {list.data.items.map((r) => (
                <tr key={r.removal_id}>
                  <td className="mono" title={r.path}>
                    {r.path}
                  </td>
                  <td>
                    <span className="tag" data-tone={STATE_TONE[r.state]}>
                      {REMOVAL_STATE[r.state] ?? r.state}
                    </span>
                  </td>
                  <td>{formatRelative(r.done_at ?? r.hidden_at ?? r.issued_at ?? "")}</td>
                  <td title={r.cancel_reason ?? ""}>{r.cancel_reason ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty">None.</p>
      )}
    </section>
  );
}

/** The annex's Rules and Removals (annex.md 8.2; phases A2.1 and A2.2). */
export function RulesSection({
  annex,
  etag,
  onChanged,
}: {
  annex: AnnexDetail;
  etag: string;
  onChanged: () => void;
}) {
  const sets = useRulesets(annex.annex_id);
  if (!sets.data) return <Loading />;
  const items = sets.data.items;
  const running = items.find((r) => r.state === "active");
  const proposed = items.filter((r) => r.state === "proposed");
  return (
    <>
      <section className="card card-pad" aria-labelledby="annex-rules">
        <h2 id="annex-rules">Rules</h2>
        <p>
          {running
            ? `Version ${running.version} is running (${running.mode === "custom" ? "custom rules" : running.mode.replace("_", " ")}).`
            : "No rule set is running yet."}{" "}
          {annex.can_manage ? (
            <Link to={`/annexes/${annex.annex_id}/rules`}>Edit the rules</Link>
          ) : null}
        </p>
        {proposed.length ? (
          <ul className="stack" aria-label="Proposals">
            {proposed.map((rs) => (
              <Proposal
                key={rs.version}
                annex={annex}
                etag={etag}
                rs={rs}
                onDone={() => {
                  void sets.refetch();
                  onChanged();
                }}
              />
            ))}
          </ul>
        ) : null}
        <details>
          <summary>All versions</summary>
          <ul>
            {items.map((r) => (
              <li key={r.version}>
                Version {r.version}: {STATE[r.state] ?? r.state}
                {r.decided_at ? `, ${formatRelative(r.decided_at)}` : ""}
              </li>
            ))}
          </ul>
        </details>
      </section>
      <Removals annex={annex} etag={etag} onChanged={onChanged} />
    </>
  );
}
