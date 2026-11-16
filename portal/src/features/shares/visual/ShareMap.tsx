import { useId } from "react";
import { Link, useSearchParams } from "react-router";
import type { Share } from "../../../api/types";
import { ErrorCallout, Loading } from "../../../components/Feedback";
import { RecallIcon, XIcon } from "../../../components/icons";
import { TierChip } from "../../../components/TierChip";
import {
  accountLabel,
  backendLabel,
  formatBytes,
  formatDateTime,
  pluralize,
  stateLabel,
  tierLabel,
} from "../../../lib/format";
import "../../files/visual/visual.css";
import {
  useShareMap,
  useShareMapFiles,
  type MapCount,
  type MapNode,
  type ShareMap as MapData,
} from "../../files/visual/queries";
import { TierLegend, className, tierAttr, useReducedMotion } from "../../files/visual/tiers";

type Store = MapData["stores"][number];
type StoreTier = Store["tiers"][number];

const EDGE_NODES = [
  ["resident", "Whole files", "open instantly"],
  ["stub", "Stubs", "first MB kept, the rest in the cloud"],
  ["arriving", "Arriving", "landing or uploading"],
  ["remote", "Not on the gateway", "read from the cloud"],
] as const;

/** `?node=` in the URL, so a node's list can be linked and survives a reload. */
function parseNode(raw: string | null): MapNode | null {
  if (!raw) return null;
  const [kind, a, b] = raw.split(":");
  if (kind === "edge" && (a === "resident" || a === "stub" || a === "arriving" || a === "remote"))
    return { edge: a };
  if (kind === "tier" && a && ["hot", "warm", "cold", "archive", "smart"].includes(a)) {
    const tier = a as "hot";
    return b === "home" || b === "source" ? { tier, role: b } : { tier };
  }
  if (kind === "recalling") return { recalling: "true" };
  return null;
}

function nodeKey(node: MapNode): string {
  if ("edge" in node) return `edge:${node.edge}`;
  if ("tier" in node) return `tier:${node.tier}${node.role ? `:${node.role}` : ""}`;
  return "recalling";
}

function nodeTitle(node: MapNode, map: MapData | undefined): string {
  if ("edge" in node) {
    return `On the gateway: ${EDGE_NODES.find(([k]) => k === node.edge)?.[1] ?? node.edge}`;
  }
  if ("tier" in node) {
    const kind =
      node.role === "source"
        ? map?.source?.backend_kind
        : node.role
          ? map?.home.backend_kind
          : null;
    return `${tierLabel(node.tier)}${kind ? ` in ${backendLabel(kind)}` : ""}`;
  }
  return "Recalls in flight";
}

/**
 * Where a share's data is: people on site, the gateway (whole files or stubs),
 * the live and archive accounts (or S3 while a migration runs), with counts and
 * bytes per tier. Recalls and migrations in flight move along their lanes. Each
 * node opens the list of its files.
 */
export function ShareMap({ share }: { share: Share }) {
  const reduced = useReducedMotion();
  const map = useShareMap(share.share_id);
  const [params, setParams] = useSearchParams();
  const node = parseNode(params.get("node"));
  const titleId = useId();
  const select = (next: MapNode | null) =>
    setParams(
      (p) => {
        const out = new URLSearchParams(p);
        if (next && (!node || nodeKey(next) !== nodeKey(node))) out.set("node", nodeKey(next));
        else out.delete("node");
        return out;
      },
      { replace: true },
    );

  return (
    <section className="card" aria-labelledby={titleId}>
      <div className="card-head">
        <h2 id={titleId}>Where this share's data is</h2>
        <span className="spacer" />
        {map.data?.computed_at ? (
          <span className="muted" style={{ fontSize: 12 }}>
            As of {formatDateTime(map.data.computed_at)}
          </span>
        ) : null}
      </div>
      <div className="card-body">
        {map.isPending ? (
          <Loading label="Counting files…" />
        ) : map.isError ? (
          <ErrorCallout error={map.error} />
        ) : (
          <MapBody share={share} map={map.data} reduced={reduced} node={node} onSelect={select} />
        )}
      </div>
    </section>
  );
}

function MapBody({
  share,
  map,
  reduced,
  node,
  onSelect,
}: {
  share: Share;
  map: MapData;
  reduced: boolean;
  node: MapNode | null;
  onSelect: (n: MapNode | null) => void;
}) {
  const home = map.stores.find((s) => s.role === "home");
  const source = map.stores.find((s) => s.role === "source");
  const two = map.home.storage_layout === "two_account";
  const live = (home?.tiers ?? []).filter((t) => t.account !== "archive");
  const archive = (home?.tiers ?? []).filter((t) => t.account === "archive");
  const total = Math.max(
    1,
    ...Object.values(map.edge ?? {}).map((c) => c.bytes),
    ...map.stores.flatMap((s) => s.tiers.map((t) => t.bytes)),
  );
  const pressed = (n: MapNode) => !!node && nodeKey(n) === nodeKey(node);
  const proto = share.protocols
    .map((p) => (p === "nfs4" ? "NFSv4" : p === "nfs3" ? "NFSv3" : "SMB"))
    .join(", ");
  const cols = 3 + (two ? 1 : 0) + (source ? 1 : 0);

  return (
    <div className="map" data-motion={reduced ? "reduce" : "full"} data-testid="share-map">
      <p className="map-scope">
        {map.basis === "counting"
          ? "Counting this share's files. The totals show once the service has counted them."
          : map.basis === "none"
            ? "There are too many files to count for you just now. Try again later."
            : map.scope === "all"
              ? "Counts cover every file in the share. The lists below name only files you can read."
              : "Counts cover only the files you can read."}
      </p>
      <div className="map-flow" data-cols={cols > 4 ? "5" : "4"}>
        <div className="map-col" role="group" aria-label="People on site">
          <div className="map-col-head">
            <span className="micro">On site</span>
            <h3>People and clusters</h3>
            <span className="map-col-sub">{proto}</span>
          </div>
          <div className="map-users">
            <span>
              Opening an archived file:{" "}
              <strong>
                {share.on_open === "fail"
                  ? "told straight away"
                  : share.on_open === "wait"
                    ? "the program waits"
                    : `waits up to ${share.wait_threshold_minutes ?? "?"} min`}
              </strong>
            </span>
          </div>
        </div>

        <div className="map-col" role="group" aria-label="The gateway">
          <div className="map-col-head">
            <span className="micro">Edge</span>
            <h3>The gateway</h3>
            <span className="map-col-sub">{share.name}</span>
          </div>
          {EDGE_NODES.map(([key, name, hint]) => (
            <NodeButton
              key={key}
              tier="edge"
              name={name}
              hint={hint}
              count={map.edge?.[key] ?? null}
              local={key === "stub" ? (map.edge?.[key].local_bytes ?? null) : null}
              total={total}
              pressed={pressed({ edge: key })}
              onClick={() => onSelect({ edge: key })}
            />
          ))}
        </div>

        <StoreColumn
          label={
            two
              ? "Live account"
              : map.home.storage_layout === "single_account"
                ? "One account"
                : "Cloud"
          }
          kind={map.home.backend_kind}
          sub={two ? "Hot, Cool, Cold and Smart" : "every tier, set in place"}
          tiers={two ? live : (home?.tiers ?? [])}
          part="home"
          total={total}
          pressed={pressed}
          onSelect={onSelect}
        />
        {two ? (
          <StoreColumn
            label="Archive account"
            kind={map.home.backend_kind}
            sub="hours to come back"
            tiers={archive}
            part="home"
            total={total}
            pressed={pressed}
            onSelect={onSelect}
            empty="Nothing archived yet."
          />
        ) : null}
        {source ? (
          <StoreColumn
            label="Migration source"
            kind={source.backend_kind}
            sub="still served from here until cut over"
            tiers={source.tiers}
            part="source"
            total={total}
            pressed={pressed}
            onSelect={onSelect}
          />
        ) : null}
      </div>

      <InFlight map={map} pressed={pressed({ recalling: "true" })} onSelect={onSelect} />
      <TierLegend label="Colour key for the map" />
      {node ? (
        <NodeFiles share={share} node={node} map={map} onClose={() => onSelect(null)} />
      ) : null}
    </div>
  );
}

function StoreColumn({
  label,
  kind,
  sub,
  tiers,
  part,
  total,
  pressed,
  onSelect,
  empty = "No files here.",
}: {
  label: string;
  kind: string;
  sub: string;
  tiers: StoreTier[];
  part: "home" | "source";
  total: number;
  pressed: (n: MapNode) => boolean;
  onSelect: (n: MapNode) => void;
  empty?: string;
}) {
  return (
    <div className="map-col" role="group" aria-label={`${label}, ${backendLabel(kind)}`}>
      <div className="map-col-head">
        <span className="micro">{backendLabel(kind)}</span>
        <h3>{label}</h3>
        <span className="map-col-sub">{sub}</span>
      </div>
      {tiers.length === 0 ? (
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>
          {empty}
        </p>
      ) : null}
      {tiers.map((t) => {
        const known = ["hot", "warm", "cold", "archive", "smart"].includes(t.logical_tier ?? "");
        const n: MapNode | null = known ? { tier: t.logical_tier as "hot", role: part } : null;
        return (
          <NodeButton
            key={`${t.logical_tier}-${t.native_class}`}
            tier={tierAttr(t.logical_tier)}
            name={tierLabel(t.logical_tier)}
            hint={t.account && t.account !== "single" ? accountLabel(t.account) : null}
            title={className(t.native_class)}
            count={{ files: t.files, bytes: t.bytes, local_bytes: 0 }}
            total={total}
            pressed={n ? pressed(n) : false}
            onClick={n ? () => onSelect(n) : undefined}
          />
        );
      })}
    </div>
  );
}

function NodeButton({
  tier,
  name,
  hint,
  title,
  count,
  local = null,
  total,
  pressed,
  onClick,
}: {
  tier: string;
  name: string;
  hint: string | null;
  title?: string;
  count: MapCount | null;
  local?: number | null;
  total: number;
  pressed: boolean;
  onClick?: () => void;
}) {
  const share = count ? count.bytes / total : 0;
  const detail = count
    ? `${pluralize(count.files, "file")}, ${formatBytes(count.bytes)}${
        local !== null && count.files > 0 ? ` (${formatBytes(local)} kept on the gateway)` : ""
      }`
    : "not counted";
  return (
    <button
      type="button"
      className="map-node"
      data-tier={tier}
      data-empty={!count || count.files === 0 ? "" : undefined}
      aria-pressed={pressed}
      disabled={!onClick}
      title={title || undefined}
      aria-label={`${name}${title ? ` (${title})` : ""}${hint ? `, ${hint}` : ""}: ${detail}.${
        onClick ? " Show these files." : ""
      }`}
      onClick={onClick}
    >
      <span className="map-node-name">{name}</span>
      <span className="map-node-count">{count ? pluralize(count.files, "file") : "—"}</span>
      <span className="map-col-sub" style={{ gridColumn: "1 / -1" }}>
        {count ? formatBytes(count.bytes) : "not counted"}
        {hint ? ` · ${hint}` : ""}
      </span>
      <span className="map-node-bar" aria-hidden="true">
        <span style={{ width: `${Math.max(count?.files ? 2 : 0, share * 100)}%` }} />
      </span>
    </button>
  );
}

/** Recalls coming back from Archive, and migrations coming in from the source. */
function InFlight({
  map,
  pressed,
  onSelect,
}: {
  map: MapData;
  pressed: boolean;
  onSelect: (n: MapNode) => void;
}) {
  const r = map.recalls ?? { open: 0, by_step: { requested: 0, rehydrating: 0, downloading: 0 } };
  const migrating = map.migration_state === "migrating" || (map.migrations?.length ?? 0) > 0;
  if (!map.recalls && !migrating) return null;
  if (r.open === 0 && !migrating) {
    return <p className="map-scope">No recalls or migrations in flight.</p>;
  }
  const items = [
    ["requested", "requested", 0.95],
    ["rehydrating", "rehydrating", 0.55],
    ["downloading", "downloading", 0.12],
  ] as const;
  return (
    <div className="map-lanes">
      {r.open > 0 ? (
        <div className="map-lane">
          <span>
            <strong>{pluralize(r.open, "recall")} coming back</strong>{" "}
            <span className="muted">
              ({r.by_step.requested} requested, {r.by_step.rehydrating} rehydrating,{" "}
              {r.by_step.downloading} downloading)
            </span>{" "}
            <button
              type="button"
              className="btn-link"
              aria-pressed={pressed}
              onClick={() => onSelect({ recalling: "true" })}
            >
              Show them
            </button>
          </span>
          <span
            className="map-lane-track"
            role="img"
            aria-label={`Archive to the gateway: ${r.by_step.requested} requested, ${r.by_step.rehydrating} rehydrating, ${r.by_step.downloading} downloading`}
          >
            {items.map(([key, label, at]) =>
              r.by_step[key] > 0 ? (
                <span
                  key={key}
                  className="map-item"
                  data-step={key}
                  style={{ ["--at" as string]: at }}
                >
                  <RecallIcon size={10} /> {r.by_step[key]} {label}
                </span>
              ) : null,
            )}
          </span>
        </div>
      ) : null}
      {migrating ? (
        <div className="map-lane">
          <span>
            <strong>
              Migrating
              {map.source ? ` from ${backendLabel(map.source.backend_kind)}` : ""} to{" "}
              {backendLabel(map.home.backend_kind)}
            </strong>{" "}
            <span className="muted">New writes go to {backendLabel(map.home.backend_kind)}.</span>
          </span>
          <span
            className="map-lane-track"
            role="img"
            aria-label={
              map.migrations
                ? map.migrations
                    .map(
                      (m) =>
                        `${m.prefix || "/"}: ${m.files.verified} of ${m.files.total ?? "?"} verified, ${m.state}`,
                    )
                    .join("; ") || "No open migration prefixes"
                : "Migration in progress"
            }
          >
            {map.migrations ? (
              map.migrations.map((m, i) => (
                <span
                  key={m.migration_id}
                  className="map-item"
                  data-kind="migration"
                  style={{ ["--at" as string]: 0.85 - i * 0.25, ["--drift" as string]: "-14px" }}
                >
                  {m.prefix || "/"} {m.files.verified}/{m.files.total ?? "?"}
                </span>
              ))
            ) : (
              <span
                className="map-item"
                data-kind="migration"
                style={{ ["--at" as string]: 0.85, ["--drift" as string]: "-14px" }}
              >
                files coming in
              </span>
            )}
          </span>
        </div>
      ) : null}
    </div>
  );
}

function NodeFiles({
  share,
  node,
  map,
  onClose,
}: {
  share: Share;
  node: MapNode;
  map: MapData;
  onClose: () => void;
}) {
  const files = useShareMapFiles(share.share_id, node);
  const headingId = useId();
  const items = files.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section className="map-files" aria-labelledby={headingId}>
      <div className="row">
        <h3 id={headingId}>{nodeTitle(node, map)}</h3>
        <span className="spacer" />
        <button type="button" className="icon-btn" aria-label="Close the list" onClick={onClose}>
          <XIcon />
        </button>
      </div>
      {files.isPending ? (
        <Loading />
      ) : files.isError ? (
        <ErrorCallout error={files.error} />
      ) : items.length === 0 ? (
        <p className="muted">No files here that you can read.</p>
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label={`Files: ${nodeTitle(node, map)}`}>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col" className="col-num">
                  Size
                </th>
                <th scope="col">Tier</th>
                <th scope="col">On the gateway</th>
              </tr>
            </thead>
            <tbody>
              {items.map((f) => (
                <tr key={f.file_id}>
                  <td>
                    {f.parent_id ? (
                      <Link to={`/files/${share.share_id}/${f.parent_id}?file=${f.file_id}`}>
                        {f.name}
                      </Link>
                    ) : (
                      f.name
                    )}
                  </td>
                  <td className="col-num num">{formatBytes(f.size)}</td>
                  <td>
                    <TierChip tier={f.tier} />
                  </td>
                  <td>{stateLabel(f.state)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {files.hasNextPage ? (
        <div className="row">
          <button
            type="button"
            className="btn btn-sm"
            disabled={files.isFetchingNextPage}
            onClick={() => void files.fetchNextPage()}
          >
            Show more
          </button>
        </div>
      ) : null}
    </section>
  );
}
