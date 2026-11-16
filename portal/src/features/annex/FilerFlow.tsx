import type { ReactNode } from "react";
import { Link } from "react-router";
import { useEdges } from "../../api/queries";
import type { components } from "../../api/schema.gen";
import { LockIcon } from "../../components/icons";
import { formatRelative } from "../../lib/format";
import { useAnnexes, type Annex, type Filer } from "./api";
import "./flow.css";

type Edge = components["schemas"]["Edge"];

/** An edge serves annexes when it is enrolled and heard from (ok or needs attention). */
export const edgeOnline = (e: Edge | undefined) => e?.status === "ok" || e?.status === "attention";

const EDGE_WORD: Record<string, string> = {
  ok: "Online",
  attention: "Needs attention",
  silent: "Not reporting",
  pending: "Not enrolled",
};

type Tone = "ok" | "warn" | "danger" | "locked" | "neutral";

function Node({
  step,
  title,
  tone,
  children,
}: {
  step: string;
  title: ReactNode;
  tone: Tone;
  children?: ReactNode;
}) {
  return (
    <li className="flow-node" data-tone={tone}>
      <span className="flow-step">
        {tone === "locked" ? <LockIcon aria-hidden="true" /> : null}
        {step}
      </span>
      <span className="flow-title">{title}</span>
      {children ? <span className="flow-detail">{children}</span> : null}
      {tone === "locked" ? (
        <span className="sr-only">Locked until the step before it is done.</span>
      ) : null}
    </li>
  );
}

/** The chain every annex needs, drawn once at the top (WP18). */
export function FlowLegend({ edges }: { edges: Edge[] | undefined }) {
  const online = (edges ?? []).filter(edgeOnline);
  const none = edges !== undefined && online.length === 0;
  return (
    <section className="card card-pad stack flow-legend" aria-labelledby="flow-h">
      <h3 id="flow-h">How a NAS reaches Azure</h3>
      <ol className="flow" aria-label="What an annex needs">
        <Node step="1" title="NAS filer" tone="neutral">
          Read over NFS or SMB; nothing is written to it.
        </Node>
        <Node step="2" title="An edge, deployed and online" tone={none ? "danger" : "ok"}>
          {edges === undefined
            ? "Checking…"
            : none
              ? edges.length === 0
                ? "No edge is deployed."
                : "No edge is reporting."
              : `${online.length} of ${edges.length} online`}
        </Node>
        <Node step="3" title="Annex" tone={none ? "locked" : "neutral"}>
          The edge copies the folder, verifies each copy, and keeps watching.
        </Node>
        <Node step="4" title="Azure" tone={none ? "locked" : "neutral"}>
          Plain blobs in your storage account, tiered by policy.
        </Node>
      </ol>
      {none ? (
        <div className="flow-cta">
          <p>
            <strong>Deploy an edge first.</strong> A filer is read by an edge on the same network,
            so nothing here works until one is online.
          </p>
          <Link className="btn btn-primary" to="/edges/new">
            Deploy an edge
          </Link>
        </div>
      ) : null}
    </section>
  );
}

function mountChecks(f: Filer): { label: string; ok: boolean | null }[] {
  const h = (f.health ?? null) as {
    ok?: boolean | null;
    read_only?: boolean | null;
    readable?: boolean | null;
    sec?: string | null;
  } | null;
  if (!h) return [{ label: "Not checked yet", ok: null }];
  return [
    { label: h.ok ? "Mounted" : "Mount failed", ok: !!h.ok },
    { label: h.readable ? "Readable" : "Not readable", ok: !!h.readable },
    ...(typeof h.read_only === "boolean"
      ? [{ label: h.read_only ? "Read-only" : "Writable mount", ok: h.read_only }]
      : []),
    ...(h.sec ? [{ label: `sec=${h.sec}`, ok: true }] : []),
  ];
}

/** One filer's chain: filer, its edge, the annexes using it, Azure. */
export function FilerFlow({
  filer,
  edge,
  annexes,
}: {
  filer: Filer;
  edge: Edge | undefined;
  annexes: Annex[];
}) {
  const online = edgeOnline(edge);
  const checks = mountChecks(filer);
  const health = filer.health as { ok?: boolean; error?: string | null } | null;
  const mounted = !!health?.ok;
  const where = `${filer.host}${filer.protocol === "smb" ? `\\${filer.export}` : `:${filer.export}`}`;
  return (
    <ol className="flow flow-row" aria-label={`${filer.name}: what it needs`}>
      <Node
        step="Filer"
        title={filer.name}
        tone={health === null ? "neutral" : mounted ? "ok" : "danger"}
      >
        <span className="mono">{where}</span>
        <span className="flow-checks">
          {checks.map((c) => (
            <span key={c.label} className="flow-check" data-ok={c.ok === null ? undefined : c.ok}>
              {c.label}
            </span>
          ))}
        </span>
        {health?.error ? <span className="flow-error">{health.error}</span> : null}
        {filer.health_checked_at ? (
          <span className="muted micro">checked {formatRelative(filer.health_checked_at)}</span>
        ) : null}
      </Node>
      <Node
        step="Edge"
        title={edge ? edge.name : `edge ${filer.edge_id}`}
        tone={!edge ? "danger" : online ? (edge.status === "ok" ? "ok" : "warn") : "danger"}
      >
        {edge ? (EDGE_WORD[edge.status] ?? edge.status) : "Not found: revoked?"}
        {edge?.last_heartbeat_at ? (
          <span className="muted micro">heard {formatRelative(edge.last_heartbeat_at)}</span>
        ) : null}
        {!online ? (
          <Link to={edge?.state === "pending" ? "/edges" : "/edges/new"}>
            {edge?.state === "pending" ? "Finish enrolling it" : "Deploy an edge"}
          </Link>
        ) : null}
      </Node>
      <Node
        step="Annex"
        title={
          annexes.length === 0
            ? "No annex yet"
            : `${annexes.length} ${annexes.length === 1 ? "annex" : "annexes"}`
        }
        tone={!online || !mounted ? "locked" : annexes.length ? "ok" : "neutral"}
      >
        {annexes.map((a) => (
          <Link key={a.annex_id} to={`/annexes/${a.annex_id}`}>
            {a.name} <span className="muted">· {a.state}</span>
          </Link>
        ))}
        {annexes.length === 0 && online && mounted ? (
          <Link to={`/annexes/new?filer=${filer.filer_id}`}>Create an Annex</Link>
        ) : null}
        {!online ? <span className="muted">Needs its edge online.</span> : null}
        {online && !mounted ? <span className="muted">Needs a working mount.</span> : null}
      </Node>
      <Node step="Azure" title="Azure Blob" tone={annexes.length && online ? "ok" : "locked"}>
        {annexes.length
          ? [...new Set(annexes.map((a) => a.dataset_name))].map((d) => (
              <span key={d}>dataset {d}</span>
            ))
          : "Copies land here once an annex runs."}
      </Node>
    </ol>
  );
}

/** Settings, Filers: the legend, then each filer's chain. */
export function FilerFlows({ filers }: { filers: Filer[] }) {
  const edges = useEdges();
  const annexes = useAnnexes();
  const list = edges.data?.edges;
  return (
    <>
      <FlowLegend edges={list} />
      {filers.length ? (
        <section className="stack" aria-labelledby="flows-h">
          <h3 id="flows-h">Each filer</h3>
          {filers.map((f) => (
            <div key={f.filer_id} className="card card-pad">
              <FilerFlow
                filer={f}
                edge={list?.find((e) => e.edge_id === f.edge_id)}
                annexes={(annexes.data?.items ?? []).filter(
                  (a) => a.filer_id === f.filer_id && a.state !== "retired",
                )}
              />
            </div>
          ))}
        </section>
      ) : null}
    </>
  );
}
