import { Link } from "react-router";
import { Callout, ErrorCallout } from "../../../components/Feedback";
import { useToast } from "../../../components/Toasts";
import { useFix, type AttentionItem } from "./consoleApi";
import { attentionLabel } from "./consoleLabels";

const FIX_DONE = {
  rerun:
    "Copy again asked for: a new run starts at the engine's next step (for a plan's migration, inside its wave's window).",
  restore_again:
    "Restore again asked for: the next restore wave renews those copies (for a plan's migration, inside its wave's window).",
  retry: "Migration started again.",
} as const;

/** How much each kind matters: lower comes first. Unknown kinds go last, shown raw. */
const RANK: Record<string, number> = {
  failed: 0,
  access: 0,
  run_failed: 1,
  verify_failed: 1,
  check_given_up: 1,
  copy_stranded: 1,
  no_checksum: 1,
  restore_expiring: 2,
  agent_offline: 3,
  seeding: 4,
};

/** The small tag on each finding: how soon a person has to act. */
function urgency(kind: string): { tone: "signal" | "warn" | "neutral"; word: string } {
  const r = RANK[kind] ?? 5;
  if (r <= 1) return { tone: "signal", word: "Act now" };
  if (r === 2) return { tone: "warn", word: "Act soon" };
  if (r === 3) return { tone: "neutral", word: "Watch" };
  return { tone: "neutral", word: "Info" };
}

/**
 * What needs a person, ranked by how soon they must act, each with its fix in
 * place. Every fix is audited.
 */
export function AttentionPanel({ items }: { items: AttentionItem[] }) {
  const fix = useFix();
  const toast = useToast();
  if (items.length === 0) {
    return (
      <Callout tone="ok" title="Nothing needs attention">
        Every migration is moving on its own.
      </Callout>
    );
  }
  const run = (migrationId: string, kind: keyof typeof FIX_DONE) =>
    fix.mutate({ migrationId, fix: kind }, { onSuccess: () => toast(FIX_DONE[kind]) });
  const ranked = items
    .map((a, i) => ({ a, i }))
    .sort((x, y) => (RANK[x.a.kind] ?? 5) - (RANK[y.a.kind] ?? 5) || x.i - y.i);
  return (
    <section className="card findings" aria-labelledby="mig-attention">
      <div className="findings-head">
        <h2 id="mig-attention">
          Needs attention <span className="findings-count">({items.length})</span>
        </h2>
        <span className="micro findings-note">Ranked by how soon to act</span>
      </div>
      {fix.isError ? (
        <div className="card-body">
          <ErrorCallout error={fix.error} title="That fix didn't go through" />
        </div>
      ) : null}
      <ol className="findings-list console-attention">
        {ranked.map(({ a, i }, n) => {
          const mid = a.migration_id;
          const tag = urgency(a.kind);
          return (
            <li
              key={`${a.kind}-${mid ?? a.agent ?? a.source_id ?? i}`}
              data-kind={a.kind}
              className="finding"
            >
              <span className="finding-rank" aria-hidden="true">
                {String(n + 1).padStart(2, "0")}
              </span>
              <div className="finding-main">
                <div className="finding-title">
                  <span className="tag" data-tone={tag.tone}>
                    {tag.word}
                  </span>
                  <strong>{attentionLabel(a.kind)}</strong>
                  {mid ? (
                    <Link className="finding-link" to={`/migrations/${mid}`}>
                      migration {mid}
                      <span className="arrow" aria-hidden="true">
                        →
                      </span>
                    </Link>
                  ) : null}
                </div>
                <div className="finding-msg">{a.message}</div>
              </div>
              <div className="finding-actions">
                {a.actions.map((act) => {
                  if (act === "rerun" && mid) {
                    return (
                      <button
                        key={act}
                        type="button"
                        className="btn btn-sm"
                        disabled={fix.isPending}
                        onClick={() => run(mid, "rerun")}
                      >
                        Copy again
                      </button>
                    );
                  }
                  if (act === "restore_again" && mid) {
                    return (
                      <button
                        key={act}
                        type="button"
                        className="btn btn-sm"
                        disabled={fix.isPending}
                        onClick={() => run(mid, "restore_again")}
                      >
                        Restore again
                      </button>
                    );
                  }
                  if (act === "retry" && mid) {
                    return (
                      <button
                        key={act}
                        type="button"
                        className="btn btn-sm"
                        disabled={fix.isPending}
                        onClick={() => run(mid, "retry")}
                      >
                        Retry
                      </button>
                    );
                  }
                  if (act === "exclude" && mid) {
                    return (
                      <Link key={act} className="btn btn-sm" to={`/migrations/${mid}?exclude=1`}>
                        Exclude objects…
                      </Link>
                    );
                  }
                  if (act === "settings") {
                    return (
                      <Link key={act} className="btn btn-sm" to="/settings">
                        Open the Settings checks
                      </Link>
                    );
                  }
                  if (act === "agents") {
                    return (
                      <Link key={act} className="btn btn-sm" to="/migrations/agents">
                        See the agents
                      </Link>
                    );
                  }
                  if (act === "wait") {
                    return (
                      <span key={act} className="muted">
                        Nothing to do: it goes on by itself.
                      </span>
                    );
                  }
                  return (
                    <span key={act} className="muted">
                      {act}
                    </span>
                  );
                })}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
