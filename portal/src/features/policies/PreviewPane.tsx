import type { PolicyPreview } from "../../api/types";
import { ChecksList, severityCounts } from "../../components/Checks";
import { Strata } from "../../components/Strata";
import { useSession } from "../../app/session";
import { costItemLabel, formatDate, formatMoney, priorityLabel, tierLabel } from "../../lib/format";
import { canSeePrices } from "../../lib/permissions";

export function CostCard({ cost }: { cost: PolicyPreview["cost"] }) {
  const { me } = useSession();
  const saving = cost.saving;
  if (!canSeePrices(me)) {
    return (
      <section className="card card-pad stack" aria-labelledby="cost-title">
        <h2 id="cost-title">First-year cost per TB</h2>
        <p className="muted">An ArchiveAdmin has hidden prices from your role.</p>
      </section>
    );
  }
  return (
    <section className="card card-pad stack" aria-labelledby="cost-title">
      <div className="row">
        <h2 id="cost-title">First-year cost per TB</h2>
        <span className="spacer" />
        {cost.price_table.verify ? (
          <span
            className="badge badge-warn"
            title="Some prices aren't yet checked against the provider's page"
          >
            Prices unverified
          </span>
        ) : null}
      </div>
      <div className="cost-hero">
        <div className="cost-figure">
          <span className="micro">This policy</span>
          <span className="cost-num" data-testid="cost-per-tb">
            {formatMoney(cost.per_tb_first_year)}
          </span>
          {saving ? (
            <span className="saving tabular">
              saves {formatMoney(saving)} against keeping it Hot
            </span>
          ) : null}
        </div>
        <div className="cost-figure">
          <span className="micro">Kept Hot all year</span>
          <span className="cost-num cost-num-sm">
            {formatMoney(cost.keep_hot_per_tb_first_year)}
          </span>
        </div>
      </div>
      <table className="cost-table" aria-label="Cost breakdown">
        <tbody>
          {cost.breakdown.map((item) => (
            <tr key={`${item.item}-${item.tier ?? ""}`}>
              <th scope="row">
                {costItemLabel(item.item)}
                {item.tier && item.item !== "storage" ? ` (${tierLabel(item.tier)})` : ""}
                {item.note ? <span className="muted"> · {item.note}</span> : null}
              </th>
              <td>{formatMoney(item.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {cost.recall_per_tb && cost.recall_per_tb.length > 0 ? (
        <div className="stack" style={{ gap: 4 }}>
          <span className="micro">Bringing 1 TB back from Archive</span>
          {cost.recall_per_tb.map((r) => (
            <span key={r.priority} className="tabular">
              {priorityLabel(r.priority)}: {formatMoney(r.per_tb)}
              {r.hours ? (
                <span className="muted">
                  {" "}
                  · {r.hours[0]}–{r.hours[1]} h for files under 10 GB (provider estimate)
                </span>
              ) : null}
            </span>
          ))}
        </div>
      ) : null}
      <p className="field-hint">
        Prices as of {formatDate(cost.price_table.as_of)}
        {cost.price_table.region ? ` (${cost.price_table.region})` : ""}
        {cost.price_table.overridden ? ", set by your admin" : ""}. Assumes{" "}
        {cost.assumptions?.tb ?? 1} TB
        {cost.assumptions?.recall_fraction_per_year
          ? ` and ${Math.round(cost.assumptions.recall_fraction_per_year * 100)}% recalled per year`
          : ""}
        .
      </p>
    </section>
  );
}

export function PreviewPane({
  preview,
  updating,
  onGo,
  shareName,
}: {
  preview: PolicyPreview;
  updating?: boolean;
  onGo?: (field: string, shareId?: string | null) => void;
  shareName?: (shareId: string) => string | undefined;
}) {
  const counts = severityCounts(preview.checks);
  return (
    <>
      <section
        className={`card card-pad stack${updating ? " updating" : ""}`}
        aria-labelledby="timeline-title"
        aria-busy={updating}
      >
        <div className="row">
          <h2 id="timeline-title">A file's life under this policy</h2>
          <span className="spacer" />
          {updating ? <span className="muted">Updating…</span> : null}
        </div>
        <Strata timeline={preview.timeline} />
      </section>
      <CostCard cost={preview.cost} />
      <section className="card card-pad stack" aria-labelledby="checks-title">
        <div className="row">
          <h2 id="checks-title">Checks</h2>
          <span className="spacer" />
          {counts.error ? <span className="badge badge-danger">{counts.error} to fix</span> : null}
          {counts.warning ? (
            <span className="badge badge-warn">{counts.warning} warnings</span>
          ) : null}
          {counts.info ? <span className="badge badge-info">{counts.info} notes</span> : null}
        </div>
        <div aria-live="polite">
          <ChecksList
            checks={preview.checks}
            shareName={shareName}
            onGo={onGo ? (check) => onGo(check.field ?? "", check.share_id ?? null) : undefined}
          />
        </div>
      </section>
    </>
  );
}
