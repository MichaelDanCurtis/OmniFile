import { useId, type KeyboardEvent } from "react";
import { useShares } from "../../api/queries";
import { formatBytes } from "../../lib/format";
import { useFilers } from "./api";
import { CHECK_LABEL, type RuleCheck } from "./rulesApi";
import {
  CONFLICT_LABEL,
  REPLACE_HINT,
  ORIGIN,
  RETURN_TRIGGER,
  get,
  type Conflict,
  type ReturnAction,
  type ReturnLocation,
  type ReturnPreview,
  type ReturnRule,
} from "./returnsApi";

const GIB = 1024 ** 3;

/** Where a return rule places a file, as one choice in the builder. */
type Place = "origin" | "annex_folder" | "filer_folder" | "share" | "nowhere";

function placeOf(r: ReturnRule): Place {
  if (get(r, "exclude")) return "nowhere";
  const loc = get(r, "place_at") as ReturnLocation | undefined;
  if (!loc) return "nowhere";
  if (loc.kind === "share_folder") return "share";
  if (loc.origin) return "origin";
  if (loc.filer_id) return "filer_folder";
  return "annex_folder";
}

export const PLACE_LABEL: Record<Place, string> = {
  origin: "Back where it came from",
  annex_folder: "A folder of this annex",
  filer_folder: "A folder on a filer",
  share: "An OmniFile share",
  nowhere: "Nowhere: Azure only",
};

function locationFor(p: Place, prev: ReturnLocation | undefined): ReturnLocation | null {
  const path = prev && !prev.origin ? (prev.path ?? "") : "";
  switch (p) {
    case "origin":
      return ORIGIN;
    case "annex_folder":
      return { kind: "nas_folder", path };
    case "filer_folder":
      return { kind: "nas_folder", filer_id: prev?.filer_id ?? "", path };
    case "share":
      return { kind: "share_folder", share_id: prev?.share_id ?? "", path };
    case "nowhere":
      return null;
  }
}

/** What a location is, in plain words. */
export function locationText(
  loc: ReturnLocation | undefined,
  names: { filers: Map<string, string>; shares: Map<string, string> },
): string {
  if (!loc) return "nowhere";
  if (loc.kind === "share_folder") {
    const s = names.shares.get(loc.share_id ?? "") ?? `share ${loc.share_id ?? "?"}`;
    return `OmniFile share ${s}${loc.path ? `/${loc.path}` : ""}`;
  }
  if (loc.origin) return "back where it came from";
  if (loc.filer_id) {
    const f = names.filers.get(loc.filer_id) ?? `filer ${loc.filer_id}`;
    return `${f}: ${loc.path || "its root"}`;
  }
  return `this annex's folder ${loc.path || "(its root)"}`;
}

function setAction(r: ReturnRule, key: string, value: ReturnAction | null): ReturnRule {
  const rest = r.actions.filter((a) => !(key in a));
  return { ...r, actions: value ? [...rest, value] : rest };
}

function list(v: string): string[] | undefined {
  const out = v
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return out.length ? out : undefined;
}

export function returnChips(c: ReturnRule["conditions"]): string[] {
  const out: string[] = [];
  if (c.requester_in?.length) out.push(`requester in ${c.requester_in.join(", ")}`);
  if (c.path?.length) out.push(c.path.join(" or "));
  if (c.extension?.length) out.push(c.extension.map((e) => `.${e}`).join(", "));
  if (c.size_gte !== undefined) out.push(`≥ ${formatBytes(c.size_gte)}`);
  if (c.size_lt !== undefined) out.push(`< ${formatBytes(c.size_lt)}`);
  return out;
}

function useNames() {
  const filers = useFilers();
  const shares = useShares();
  return {
    filers: new Map((filers.data?.items ?? []).map((f) => [f.filer_id, f.name])),
    filerList: filers.data?.items ?? [],
    shares: new Map((shares.data?.items ?? []).map((s) => [s.share_id, s.name])),
    shareList: shares.data?.items ?? [],
  };
}

export function ReturnRuleRow({
  rule,
  n,
  first,
  last,
  checks,
  onChange,
  onMove,
  onRemove,
}: {
  rule: ReturnRule;
  n: number;
  first: boolean;
  last: boolean;
  checks: RuleCheck[];
  onChange: (r: ReturnRule) => void;
  onMove: (by: -1 | 1) => void;
  onRemove: () => void;
}) {
  const id = useId();
  const names = useNames();
  const c = rule.conditions;
  const place = placeOf(rule);
  const loc = get(rule, "place_at") as ReturnLocation | undefined;
  const conflict = (get(rule, "on_conflict") as Conflict | undefined) ?? "restored_name";
  const hold = (get(rule, "hold_days") as number | undefined) ?? 0;
  const notify = ((get(rule, "notify") as unknown[] | undefined) ?? []).includes("requester");
  const setC = (patch: Partial<ReturnRule["conditions"]>) => {
    const next = Object.fromEntries(
      Object.entries({ ...c, ...patch }).filter(([, v]) => (v as unknown) !== undefined),
    ) as ReturnRule["conditions"];
    onChange({ ...rule, conditions: next });
  };
  const setLoc = (l: ReturnLocation) => onChange(setAction(rule, "place_at", { place_at: l }));
  // A NAS file is replaced only inside the annex (annex.md 4.6).
  const canOverwrite = place === "share" || place === "origin" || place === "annex_folder";
  const onKey = (e: KeyboardEvent) => {
    if (!e.altKey) return;
    if (e.key === "ArrowUp" && !first) {
      e.preventDefault();
      onMove(-1);
    }
    if (e.key === "ArrowDown" && !last) {
      e.preventDefault();
      onMove(1);
    }
  };
  return (
    <fieldset className="card card-pad rule-row">
      <legend>Back rule {n}</legend>
      <div className="grid-2">
        <div className="field">
          <label className="field-label" htmlFor={`${id}-place`}>
            Place at
          </label>
          <select
            id={`${id}-place`}
            onKeyDown={onKey}
            value={place}
            onChange={(e) => {
              const p = e.target.value as Place;
              const l = locationFor(p, loc);
              let next = setAction(rule, "exclude", p === "nowhere" ? { exclude: true } : null);
              next = setAction(next, "place_at", l ? { place_at: l } : null);
              if (p === "nowhere") {
                for (const k of ["on_conflict", "hold_days", "let_requester_choose"])
                  next = setAction(next, k, null);
              } else if (!get(next, "on_conflict")) {
                next = setAction(next, "on_conflict", { on_conflict: "restored_name" });
              }
              if (p === "filer_folder" && conflict === "overwrite_keep_version")
                next = setAction(next, "on_conflict", { on_conflict: "restored_name" });
              onChange(next);
            }}
          >
            {(Object.keys(PLACE_LABEL) as Place[]).map((p) => (
              <option key={p} value={p}>
                {PLACE_LABEL[p]}
              </option>
            ))}
          </select>
        </div>
        {place === "filer_folder" ? (
          <div className="field">
            <label className="field-label" htmlFor={`${id}-filer`}>
              Filer
            </label>
            <select
              id={`${id}-filer`}
              onKeyDown={onKey}
              value={loc?.filer_id ?? ""}
              onChange={(e) =>
                setLoc({ kind: "nas_folder", filer_id: e.target.value, path: loc?.path ?? "" })
              }
            >
              <option value="">Pick a filer</option>
              {names.filerList.map((f) => (
                <option key={f.filer_id} value={f.filer_id}>
                  {f.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        {place === "share" ? (
          <div className="field">
            <label className="field-label" htmlFor={`${id}-share`}>
              Share
            </label>
            <select
              id={`${id}-share`}
              onKeyDown={onKey}
              value={loc?.share_id ?? ""}
              onChange={(e) =>
                setLoc({ kind: "share_folder", share_id: e.target.value, path: loc?.path ?? "" })
              }
            >
              <option value="">Pick a share</option>
              {names.shareList.map((s) => (
                <option key={s.share_id} value={s.share_id}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        {place === "annex_folder" || place === "filer_folder" || place === "share" ? (
          <div className="field">
            <label className="field-label" htmlFor={`${id}-folder`}>
              Folder
            </label>
            <input
              id={`${id}-folder`}
              onKeyDown={onKey}
              value={loc?.path ?? ""}
              placeholder="restored"
              onChange={(e) => loc && setLoc({ ...loc, path: e.target.value })}
            />
            <p className="field-hint">The file keeps its folders below the annex.</p>
          </div>
        ) : null}
        {place !== "nowhere" ? (
          <>
            <div className="field">
              <label className="field-label" htmlFor={`${id}-conflict`}>
                If the name is taken
              </label>
              <select
                id={`${id}-conflict`}
                onKeyDown={onKey}
                value={conflict}
                onChange={(e) =>
                  onChange(
                    setAction(rule, "on_conflict", { on_conflict: e.target.value as Conflict }),
                  )
                }
              >
                {(Object.keys(CONFLICT_LABEL) as Conflict[])
                  .filter((k) => k !== "overwrite_keep_version" || canOverwrite)
                  .map((k) => (
                    <option key={k} value={k}>
                      {CONFLICT_LABEL[k]}
                    </option>
                  ))}
              </select>
              {conflict === "overwrite_keep_version" && place !== "share" ? (
                <p className="field-hint" data-testid={`${id}-replaces`}>
                  {REPLACE_HINT}
                </p>
              ) : null}
            </div>
            <div className="field">
              <label className="field-label" htmlFor={`${id}-hold`}>
                Hold it there (days)
              </label>
              <input
                id={`${id}-hold`}
                onKeyDown={onKey}
                type="number"
                min={0}
                max={365}
                value={hold}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  onChange(setAction(rule, "hold_days", v > 0 ? { hold_days: v } : null));
                }}
              />
              <p className="field-hint">Outbound rules leave a placed file alone until then.</p>
            </div>
            <div className="field">
              <label className="checkbox">
                <input
                  type="checkbox"
                  onKeyDown={onKey}
                  checked={notify}
                  onChange={(e) =>
                    onChange(
                      setAction(
                        rule,
                        "notify",
                        e.target.checked ? { notify: ["requester"] } : null,
                      ),
                    )
                  }
                />{" "}
                Tell the requester in Teams
              </label>
            </div>
          </>
        ) : null}
      </div>
      <details open={returnChips(c).length > 0}>
        <summary>
          If ({returnChips(c).length ? returnChips(c).join(" · ") : "every recalled file"})
        </summary>
        <div className="grid-2">
          <div className="field">
            <label className="field-label" htmlFor={`${id}-req`}>
              Requester in (group SIDs, comma-separated)
            </label>
            <input
              id={`${id}-req`}
              onKeyDown={onKey}
              value={(c.requester_in ?? []).join(", ")}
              onChange={(e) => setC({ requester_in: list(e.target.value) })}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor={`${id}-path`}>
              Path (globs, comma-separated)
            </label>
            <input
              id={`${id}-path`}
              onKeyDown={onKey}
              value={(c.path ?? []).join(", ")}
              onChange={(e) => setC({ path: list(e.target.value) })}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor={`${id}-ext`}>
              Extensions
            </label>
            <input
              id={`${id}-ext`}
              onKeyDown={onKey}
              value={(c.extension ?? []).join(", ")}
              onChange={(e) => setC({ extension: list(e.target.value) })}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor={`${id}-size`}>
              At least (GiB)
            </label>
            <input
              id={`${id}-size`}
              onKeyDown={onKey}
              type="number"
              min={0}
              value={c.size_gte === undefined ? "" : c.size_gte / GIB}
              onChange={(e) =>
                setC({
                  size_gte:
                    e.target.value === "" ? undefined : Math.round(Number(e.target.value) * GIB),
                })
              }
            />
          </div>
        </div>
      </details>
      {checks.length ? (
        <ul className="callout callout-warn" aria-label={`Problems with back rule ${n}`}>
          {checks.map((k) => (
            <li key={k.check}>
              <strong>{CHECK_LABEL[k.check] ?? k.check}</strong>: {k.message}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="row-actions">
        <button
          type="button"
          className="btn btn-sm"
          disabled={first}
          onClick={() => onMove(-1)}
          aria-label={`Move back rule ${n} up`}
        >
          Move up
        </button>
        <button
          type="button"
          className="btn btn-sm"
          disabled={last}
          onClick={() => onMove(1)}
          aria-label={`Move back rule ${n} down`}
        >
          Move down
        </button>
        <button
          type="button"
          className="btn btn-sm btn-danger"
          onClick={onRemove}
          aria-label={`Remove back rule ${n}`}
        >
          Remove
        </button>
      </div>
    </fieldset>
  );
}

/** The Back diagram (annex.md 8.3): read right to left, from Azure to where a
 * recalled file lands, drawn from the ordered list, with the preview's counts. */
export function BackFlow({
  rules,
  preview,
}: {
  rules: ReturnRule[];
  preview: ReturnPreview | null;
}) {
  const names = useNames();
  const counts = new Map((preview?.rules ?? []).map((r) => [r.id, r]));
  if (!rules.length) {
    return (
      <p className="lede">
        No back rules: a recalled file comes back to Azure only, and is placed nowhere.
      </p>
    );
  }
  return (
    <ol className="rule-flow" aria-label="Back rule flow">
      {rules.map((r) => {
        const k = counts.get(r.id);
        const loc = get(r, "place_at") as ReturnLocation | undefined;
        const hold = get(r, "hold_days") as number | undefined;
        const conflict = get(r, "on_conflict") as string | undefined;
        const zero = preview !== null && k !== undefined && k.files === 0 && k.may_take === 0;
        const taken = (k?.targets ?? []).reduce((n, t) => n + t.names_taken, 0);
        const free = k?.targets[0]?.free_bytes;
        return (
          <li key={r.id} className={zero ? "muted" : undefined}>
            <span className="chips">
              {returnChips(r.conditions).length
                ? returnChips(r.conditions).join(" · ")
                : "everyone else"}
            </span>
            {" ◀ "}
            <span className={get(r, "exclude") ? "badge badge-outline" : "badge"}>
              {get(r, "exclude") ? "placed nowhere" : `placed at ${locationText(loc, names)}`}
            </span>
            {conflict ? (
              <span className="muted"> · on a clash: {CONFLICT_LABEL[conflict] ?? conflict}</span>
            ) : null}
            {hold ? <span className="muted"> · hold {hold} days</span> : null}
            {k ? (
              <span className="muted">
                {" "}
                · today: {zero ? "no files match" : `${k.files} files (${formatBytes(k.bytes)})`}
                {k.may_take ? `, ${k.may_take} more for the requesters it names` : ""}
                {taken ? `, ${taken} names taken` : ""}
                {free !== undefined && free !== null ? `, ${formatBytes(free)} free` : ""}
              </span>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

export function newReturnRule(): ReturnRule {
  return {
    id: `back-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
    trigger: RETURN_TRIGGER,
    conditions: {},
    actions: [{ place_at: ORIGIN }, { on_conflict: "restored_name" }, { hold_days: 14 }],
  };
}
