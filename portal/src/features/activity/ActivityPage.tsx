import { useId, useState } from "react";
import { useActivity, useDatasets } from "../../api/queries";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { HeroMetric, Pills } from "../../components/PageHead";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { formatBytes, formatDate, formatDuration } from "../../lib/format";
import { isArchiveAdmin } from "../../lib/permissions";
import { LivePanel } from "./live/LivePanel";

const WINDOWS = [7, 30, 90] as const;

type Day = {
  date: string;
  uploads: { files: number; bytes: number };
  recalls: { started: number; available: number; failed: number };
};

/** One bar per day: uploaded bytes, scaled to the busiest day. */
function UploadChart({ days }: { days: Day[] }) {
  const max = Math.max(1, ...days.map((d) => d.uploads.bytes));
  const width = 600;
  const height = 120;
  const bar = width / Math.max(1, days.length);
  const total = days.reduce((n, d) => n + d.uploads.bytes, 0);
  return (
    <svg
      className="activity-chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Bytes uploaded per day: ${formatBytes(total)} over ${days.length} days`}
      preserveAspectRatio="none"
    >
      {days.map((d, i) => {
        const h = Math.round((d.uploads.bytes / max) * (height - 4));
        return (
          <rect
            key={d.date}
            x={i * bar + 1}
            y={height - h}
            width={Math.max(1, bar - 2)}
            height={h}
            rx={2}
          >
            <title>{`${formatDate(d.date)}: ${formatBytes(d.uploads.bytes)}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

export function ActivityPage() {
  usePageTitle("Activity");
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const datasets = useDatasets();
  const [days, setDays] = useState<number>(30);
  const [datasetId, setDatasetId] = useState("");
  const activity = useActivity(days, datasetId || undefined, admin);
  const ids = { dataset: useId(), days: useId() };

  if (!admin) {
    return (
      <div className="page">
        <h1>Activity</h1>
        <ErrorCallout error={new Error("Activity is for ArchiveAdmin.")} />
      </div>
    );
  }
  const a = activity.data;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Activity</h1>
          <p className="page-question">What is moving through the gateway now, and day by day?</p>
          <p className="lede">
            Uploads, recalls, tier moves, heartbeats, migrations and annexes as they happen, then
            the daily totals, straight from the catalog.
          </p>
        </div>
        {a ? (
          <HeroMetric
            label="Committed"
            value={formatBytes(a.uploads.bytes).split(" ")[0] ?? ""}
            unit={formatBytes(a.uploads.bytes).split(" ")[1]}
            sub={`uploaded in the last ${days} days`}
          />
        ) : null}
      </div>
      <LivePanel />
      <h2>Day by day</h2>
      <div className="toolbar">
        <Pills label="This page">
          <span className="pill">
            <label className="pill-key" htmlFor={ids.dataset}>
              Dataset
            </label>
            <select
              id={ids.dataset}
              value={datasetId}
              onChange={(e) => setDatasetId(e.target.value)}
            >
              <option value="">All datasets</option>
              {(datasets.data?.items ?? []).map((d) => (
                <option key={d.dataset_id} value={d.dataset_id}>
                  {d.name}
                </option>
              ))}
            </select>
          </span>
          <span className="pill">
            <label className="pill-key" htmlFor={ids.days}>
              Window
            </label>
            <select id={ids.days} value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {WINDOWS.map((n) => (
                <option key={n} value={n}>
                  Last {n} days
                </option>
              ))}
            </select>
          </span>
        </Pills>
      </div>
      {activity.isError ? <ErrorCallout error={activity.error} /> : null}
      {!a ? (
        activity.isError ? null : (
          <Loading />
        )
      ) : (
        <>
          <div className="grid-2">
            <section className="card card-pad" aria-labelledby="act-up">
              <h2 id="act-up">Uploads</h2>
              <dl className="kv">
                <dt>Files committed</dt>
                <dd>{a.uploads.files}</dd>
                <dt>Bytes committed</dt>
                <dd>{formatBytes(a.uploads.bytes)}</dd>
                <dt>Waiting for read-back</dt>
                <dd>{a.uploads.unverified}</dd>
                <dt>Still on an edge</dt>
                <dd>{a.uploads.landing}</dd>
              </dl>
            </section>
            <section className="card card-pad" aria-labelledby="act-rec">
              <h2 id="act-rec">Recalls</h2>
              <dl className="kv">
                <dt>Open now</dt>
                <dd>{a.recalls.open}</dd>
                <dt>Available</dt>
                <dd>{a.recalls.available}</dd>
                <dt>Failed</dt>
                <dd>{a.recalls.failed}</dd>
                <dt>Bytes recalled</dt>
                <dd>{formatBytes(a.recalls.bytes)}</dd>
                <dt>Typical wait</dt>
                <dd>
                  {formatDuration(a.recalls.time_to_available_s.p50)}
                  {a.recalls.time_to_available_s.p90 !== null
                    ? ` (90% within ${formatDuration(a.recalls.time_to_available_s.p90)})`
                    : ""}
                </dd>
              </dl>
            </section>
          </div>
          <section className="card card-pad">
            <h2>Uploaded per day</h2>
            <UploadChart days={a.days} />
          </section>
          <div className="table-wrap card">
            <table className="table" aria-label="Daily activity">
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  <th scope="col" className="col-num">
                    Files uploaded
                  </th>
                  <th scope="col" className="col-num">
                    Bytes uploaded
                  </th>
                  <th scope="col" className="col-num">
                    Recalls started
                  </th>
                  <th scope="col" className="col-num">
                    Available
                  </th>
                  <th scope="col" className="col-num">
                    Failed
                  </th>
                </tr>
              </thead>
              <tbody>
                {[...a.days].reverse().map((d) => (
                  <tr key={d.date}>
                    <td>{formatDate(d.date)}</td>
                    <td className="col-num">{d.uploads.files}</td>
                    <td className="col-num">{formatBytes(d.uploads.bytes)}</td>
                    <td className="col-num">{d.recalls.started}</td>
                    <td className="col-num">{d.recalls.available}</td>
                    <td className="col-num">{d.recalls.failed}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
