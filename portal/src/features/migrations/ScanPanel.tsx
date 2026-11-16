import { Link } from "react-router";
import { Callout, DotLoader, ErrorCallout } from "../../components/Feedback";
import { formatBytes, formatDuration, formatMoney, pluralize } from "../../lib/format";
import {
  scanSettled,
  useSourceScan,
  useStartScan,
  type SourceScan,
  type SourceScanCreate,
  type SourceScanResult,
} from "./api";

/**
 * Scan source (docs/design/cloud-connect.md 4): a capped, read-only listing of
 * the prefix (or its S3 Inventory) by the worker, before anything is
 * registered. It shows what is there, what needs a restore, and suggests waves.
 */

const n = (v: number) => v.toLocaleString();
/** Rows that summarise rather than name a prefix: they can't be used as one. */
const usable = (prefix: string) => prefix.endsWith("/") && !prefix.startsWith("(");

function cost(r: SourceScanResult["restore"]) {
  if (!r.prices_set || r.cost === null) {
    return (
      <>
        No S3 prices are set, so no cost is shown (
        <Link to="/settings/prices-alerts">Settings › Prices and alerts</Link>).
      </>
    );
  }
  return r.currency ? formatMoney({ amount: r.cost, currency: r.currency }) : r.cost;
}

function Progress({ scan }: { scan: SourceScan }) {
  return (
    <Callout
      role="status"
      title={scan.state === "pending" ? "Waiting for the worker…" : "Scanning…"}
    >
      <p>
        <DotLoader /> {n(scan.progress.objects)} objects, {formatBytes(scan.progress.bytes)}, in{" "}
        {formatDuration(scan.progress.elapsed_s)}.
      </p>
      <p className="muted">
        It stops at {n(scan.caps.max_objects)} objects or {formatDuration(scan.caps.max_seconds)},
        whichever comes first.
      </p>
    </Callout>
  );
}

function Result({
  scan,
  r,
  onUsePrefix,
}: {
  scan: SourceScan;
  r: SourceScanResult;
  onUsePrefix: (prefix: string) => void;
}) {
  return (
    <div className="stack">
      {scan.state === "capped" ? (
        <Callout tone="warn" title="The scan stopped at a cap">
          {scan.detail ?? "The totals are of what it saw before the cap; the source holds more."}
        </Callout>
      ) : null}
      <dl className="kv">
        <dt>Scanned</dt>
        <dd className="mono">
          s3://{scan.bucket}/{scan.prefix}{" "}
          <span className="muted">
            ({scan.reader === "s3_inventory" ? "from its S3 Inventory" : "by listing"})
          </span>
        </dd>
        <dt>Found</dt>
        <dd>
          {n(r.objects)} objects, {formatBytes(r.bytes)}
        </dd>
        <dt>Needs a restore</dt>
        <dd>
          {n(r.restore.objects)} objects, {formatBytes(r.restore.bytes)}
        </dd>
      </dl>

      <div className="table-wrap">
        <table className="table" aria-label="Prefixes found">
          <thead>
            <tr>
              <th scope="col">Prefix</th>
              <th scope="col" className="col-num">
                Objects
              </th>
              <th scope="col" className="col-num">
                Size
              </th>
              <th scope="col" className="col-num">
                To restore
              </th>
              <th scope="col" className="col-num">
                Restore size
              </th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {r.prefixes.map((p) => (
              <tr key={p.prefix}>
                <th scope="row" className="mono">
                  {p.prefix}
                </th>
                <td className="num">{n(p.objects)}</td>
                <td className="num">{formatBytes(p.bytes)}</td>
                <td className="num">{n(p.restore_objects)}</td>
                <td className="num">{formatBytes(p.restore_bytes)}</td>
                <td>
                  {usable(p.prefix) ? (
                    <button
                      type="button"
                      className="btn btn-sm"
                      aria-label={`Use the prefix ${p.prefix}`}
                      onClick={() => onUsePrefix(p.prefix)}
                    >
                      Use this prefix
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="table-wrap">
        <table className="table" aria-label="Storage classes">
          <thead>
            <tr>
              <th scope="col">Storage class</th>
              <th scope="col" className="col-num">
                Objects
              </th>
              <th scope="col" className="col-num">
                Size
              </th>
              <th scope="col">Before copying</th>
            </tr>
          </thead>
          <tbody>
            {r.classes.map((c) => (
              <tr key={c.storage_class}>
                <th scope="row" className="mono">
                  {c.storage_class}
                </th>
                <td className="num">{n(c.objects)}</td>
                <td className="num">{formatBytes(c.bytes)}</td>
                <td>
                  {c.restore_needed ? (
                    <span className="badge badge-warn">Restore needed</span>
                  ) : (
                    <span className="muted">Copied as it is</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="stack">
        <h4>Restore estimate</h4>
        {r.restore.objects === 0 ? (
          <p>Nothing here needs a restore: every object can be copied straight away.</p>
        ) : (
          <dl className="kv" aria-label="Restore estimate">
            <dt>To restore</dt>
            <dd>
              {n(r.restore.objects)} objects, {formatBytes(r.restore.bytes)}, in{" "}
              {pluralize(r.restore.waves, "wave")} (Bulk restores)
            </dd>
            <dt>Time per wave</dt>
            <dd>
              {r.restore.hours === null
                ? "—"
                : `Up to ${r.restore.hours} h, AWS's stated Bulk time for the slowest class`}
            </dd>
            <dt>Sending the requests</dt>
            <dd>
              {formatDuration(r.restore.request_seconds)}, at the 1,000 a second that user recalls
              share
            </dd>
            <dt>Cost</dt>
            <dd>{cost(r.restore)}</dd>
          </dl>
        )}
        {r.restore.notes.length ? (
          <ul className="muted">
            {r.restore.notes.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        ) : null}
      </div>

      {r.large_objects.objects > 0 ? (
        <Callout tone="warn" title={`${n(r.large_objects.objects)} objects over 10 GiB`}>
          {formatBytes(r.large_objects.bytes)} in all. OmniFile promises no restore or copy time for
          objects this large.
        </Callout>
      ) : null}

      {r.waves.length ? (
        <div className="stack">
          <h4>Suggested waves</h4>
          <p className="muted">
            Prefixes in order, at most 100,000 archived objects and 50 TiB a wave. Prefixes with
            nothing to restore come first, copied as they are.
          </p>
          <div className="table-wrap">
            <table className="table" aria-label="Suggested waves">
              <thead>
                <tr>
                  <th scope="col">Wave</th>
                  <th scope="col">Prefixes</th>
                  <th scope="col" className="col-num">
                    Objects
                  </th>
                  <th scope="col" className="col-num">
                    Size
                  </th>
                  <th scope="col" className="col-num">
                    To restore
                  </th>
                </tr>
              </thead>
              <tbody>
                {r.waves.map((w) => (
                  <tr key={w.wave}>
                    <th scope="row">
                      {w.wave}{" "}
                      {w.copy_only ? <span className="badge badge-outline">Copy only</span> : null}
                    </th>
                    <td className="mono">{w.prefixes.join(", ")}</td>
                    <td className="num">{n(w.objects)}</td>
                    <td className="num">{formatBytes(w.bytes)}</td>
                    <td className="num">
                      {n(w.restore_objects)} ({formatBytes(w.restore_bytes)})
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function ScanPanel({
  request,
  scanId,
  onScan,
  onUsePrefix,
}: {
  /** What to scan; null while the backend, bucket or prefix isn't valid yet. */
  request: SourceScanCreate | null;
  scanId: string | null;
  onScan: (scanId: string) => void;
  onUsePrefix: (prefix: string) => void;
}) {
  const start = useStartScan();
  const scan = useSourceScan(scanId);
  const s = scan.data;
  const running = !!scanId && (!s || !scanSettled(s.state));
  return (
    <section className="card card-pad stack" aria-labelledby="scan-h">
      <h3 id="scan-h">Scan the source first (optional)</h3>
      <p className="muted">
        See what is there before you start: the prefixes, the storage classes, what needs a restore
        from Glacier, and suggested waves. The scan only lists the objects (or reads the S3
        Inventory you named); it never writes to the source, or anywhere else.
      </p>
      <div>
        <button
          type="button"
          className="btn"
          disabled={!request || start.isPending || running}
          onClick={() => {
            if (request) start.mutate(request, { onSuccess: (x) => onScan(x.scan_id) });
          }}
        >
          {start.isPending || running ? "Scanning…" : s ? "Scan again" : "Scan"}
        </button>
      </div>
      {start.isError ? <ErrorCallout error={start.error} title="The scan didn't start" /> : null}
      {scan.isError ? <ErrorCallout error={scan.error} title="The scan couldn't be read" /> : null}
      {s && !scanSettled(s.state) ? <Progress scan={s} /> : null}
      {s && s.state === "failed" ? (
        <Callout tone="danger" role="status" title="The scan failed">
          {s.detail ?? "The worker couldn't read the source."}
        </Callout>
      ) : null}
      {s && s.result && (s.state === "done" || s.state === "capped") ? (
        <Result scan={s} r={s.result} onUsePrefix={onUsePrefix} />
      ) : null}
    </section>
  );
}

/** The scan's totals, estimate and waves, for the review step. */
export function ScanSummary({ scanId }: { scanId: string | null }) {
  const scan = useSourceScan(scanId);
  const s = scan.data;
  if (!s?.result) return null;
  const r = s.result;
  return (
    <>
      <dt>Scan</dt>
      <dd>
        <span className="mono">
          s3://{s.bucket}/{s.prefix}
        </span>
        : {n(r.objects)} objects, {formatBytes(r.bytes)}
        {s.state === "capped" ? " (stopped at a cap)" : ""}.{" "}
        {r.restore.objects
          ? `${n(r.restore.objects)} objects (${formatBytes(r.restore.bytes)}) need a restore, in ${pluralize(r.restore.waves, "wave")}${r.restore.hours !== null ? ` of up to ${r.restore.hours} h` : ""}.`
          : "Nothing needs a restore."}{" "}
        {r.waves.length ? `${pluralize(r.waves.length, "suggested wave")}.` : ""}
        {r.large_objects.objects
          ? ` ${n(r.large_objects.objects)} objects over 10 GiB: no time promised.`
          : ""}
      </dd>
    </>
  );
}
