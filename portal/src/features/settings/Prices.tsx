import { useId, useState } from "react";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { useToast } from "../../components/Toasts";
import { formatDate, formatDateTime } from "../../lib/format";
import {
  usePriceDisplay,
  usePriceTable,
  useSavePriceTable,
  useSetPriceDisplay,
  type PriceRole,
  type PriceTable,
} from "./queries";

const ROLES: { key: PriceRole; label: string; hint: string }[] = [
  { key: "ArchiveAdmin", label: "ArchiveAdmins", hint: "Policies, tiers and these settings." },
  { key: "DataSteward", label: "DataStewards", hint: "Tiering of the folders they look after." },
  { key: "others", label: "Everyone else", hint: "Share Owners, Writers and Readers." },
];

const PRESETS: { key: string; label: string; roles: PriceRole[] }[] = [
  { key: "all", label: "Everyone", roles: ["ArchiveAdmin", "DataSteward", "others"] },
  { key: "admins", label: "Admins only", roles: ["ArchiveAdmin", "DataSteward"] },
  { key: "archive", label: "ArchiveAdmins only", roles: ["ArchiveAdmin"] },
  { key: "none", label: "Nobody", roles: [] },
];

const same = (a: readonly string[], b: readonly string[]) =>
  a.length === b.length && a.every((x) => b.includes(x));

/** Who sees prices (portal-api.md 5.24): a preset, or each role one click down. */
export function PriceVisibility() {
  const display = usePriceDisplay();
  const save = useSetPriceDisplay();
  const toast = useToast();
  const [draft, setDraft] = useState<PriceRole[] | null>(null);
  const name = useId();
  if (display.isPending) return <Loading />;
  if (display.isError) return <ErrorCallout error={display.error} />;
  const { display: d, etag } = display.data;
  const roles = draft ?? d.show_to;
  const preset = PRESETS.find((p) => same(p.roles, roles))?.key ?? "custom";
  const changed = !same(roles, d.show_to);
  const toggle = (k: PriceRole) =>
    setDraft(roles.includes(k) ? roles.filter((r) => r !== k) : [...roles, k]);
  return (
    <section className="card card-pad stack" aria-labelledby="price-vis-h">
      <h3 id="price-vis-h">Who sees prices</h3>
      <p className="muted">
        Costs show in the policy preview, recall choices, tier pins and checks. Hidden from a role,
        they leave the service&apos;s answers to that role too, not just the page. Your price editor
        below always shows the rates.
      </p>
      <fieldset className="options options-row">
        <legend className="field-label">Show prices to</legend>
        {PRESETS.map((p) => (
          <label key={p.key} className="option">
            <input
              type="radio"
              name={name}
              checked={preset === p.key}
              onChange={() => setDraft(p.roles)}
            />
            <span className="option-title">{p.label}</span>
          </label>
        ))}
      </fieldset>
      <fieldset className="role-checks">
        <legend className="field-label">Each role</legend>
        {ROLES.map((r) => (
          <label key={r.key} className="check-row">
            <input type="checkbox" checked={roles.includes(r.key)} onChange={() => toggle(r.key)} />
            <span>
              {r.label} <span className="muted">· {r.hint}</span>
            </span>
          </label>
        ))}
      </fieldset>
      {roles.length === 0 ? (
        <Callout tone="warn">
          Nobody sees costs, ArchiveAdmins included, outside this editor.
        </Callout>
      ) : null}
      {save.isError ? <ErrorCallout error={save.error} /> : null}
      <div className="row">
        <button
          type="button"
          className="btn btn-primary"
          disabled={!changed || save.isPending}
          onClick={() =>
            save.mutate(
              { show_to: roles, etag },
              {
                onSuccess: () => {
                  setDraft(null);
                  toast("Who sees prices is saved. The change is in the audit log.");
                },
              },
            )
          }
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {changed ? (
          <button type="button" className="btn btn-link" onClick={() => setDraft(null)}>
            Undo
          </button>
        ) : null}
        <span className="spacer" />
        <span className="muted micro">
          {d.set_by ? `Last changed by ${d.set_by}, ${formatDateTime(d.set_at)}` : "Never changed"}
        </span>
      </div>
    </section>
  );
}

/** The rates the portal prices with. */
const FIELDS = [
  { key: "gb_month", label: "Per GB-month" },
  { key: "write_per_10k", label: "Writes per 10,000" },
  { key: "read_per_10k", label: "Reads per 10,000" },
  { key: "retrieval_per_gb", label: "Retrieval per GB" },
] as const;
type Field = (typeof FIELDS)[number]["key"];
type Classes = PriceTable["classes"];

const DECIMAL = /^[0-9]+(\.[0-9]+)?$/;

/** Classes a table has no list price for yet but the backend uses (backends/sim/capabilities.py). */
const KNOWN_CLASSES: Record<string, string[]> = {
  s3: ["STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE", "INTELLIGENT_TIERING"],
};

/** A number in the table's own spelling, for comparing an edit with the list price. */
const norm = (v: string | undefined) => (v === undefined || v === "" ? "" : String(Number(v)));

/** One provider's rates: edit any, put one class or the whole table back to its list price. */
export function PriceEditor({ kind, label }: { kind: "azure_blob" | "s3"; label: string }) {
  const table = usePriceTable(kind);
  const save = useSavePriceTable(kind);
  const toast = useToast();
  const [edits, setEdits] = useState<Record<string, Partial<Record<Field, string>>>>({});
  if (table.isPending) return <Loading />;
  if (table.isError) {
    return (
      <div className="card card-pad stack">
        <h3>{label}</h3>
        <ErrorCallout error={table.error} />
      </div>
    );
  }
  const t = table.data;
  const list: Classes = t.list?.classes ?? {};
  const value = (cls: string, f: Field) => edits[cls]?.[f] ?? t.classes[cls]?.[f] ?? "";
  const listValue = (cls: string, f: Field) =>
    (list[cls] as Record<string, string> | undefined)?.[f];
  const bad = Object.entries(edits).some(([, row]) =>
    Object.values(row).some((v) => v !== "" && !DECIMAL.test(v)),
  );
  const dirty = Object.keys(edits).length > 0;
  const classes = [
    ...new Set([...Object.keys(t.classes), ...Object.keys(list), ...(KNOWN_CLASSES[kind] ?? [])]),
  ];
  const build = (rows: Classes): PriceTable => ({ ...t, classes: rows });
  const merged = (): Classes => {
    const out: Classes = {};
    for (const cls of classes) {
      const was: Record<string, unknown> = { ...(t.classes[cls] ?? list[cls] ?? {}) };
      const row = Object.fromEntries(
        Object.entries(was)
          .map(([k, v]): [string, unknown] => [k, edits[cls]?.[k as Field] ?? v])
          .filter(([, v]) => v !== ""),
      );
      for (const f of FIELDS) {
        const v = edits[cls]?.[f.key];
        if (v && !(f.key in row)) row[f.key] = v;
      }
      // A class with no rate at all is left out, as a table without it.
      if (FIELDS.some((f) => f.key in row)) out[cls] = row;
    }
    return out;
  };
  const resetClass = (cls: string) => {
    const row: Partial<Record<Field, string>> = {};
    for (const f of FIELDS) row[f.key] = listValue(cls, f.key) ?? "";
    setEdits((e) => ({ ...e, [cls]: row }));
  };
  return (
    <div className="card card-pad stack">
      <div className="row">
        <h3>{label}</h3>
        <span className="spacer" />
        {t.overridden ? (
          <span className="badge badge-warn">Your rates</span>
        ) : (
          <span className="badge badge-outline">List prices</span>
        )}
        {t.verify ? <span className="badge badge-warn">Some prices unchecked</span> : null}
      </div>
      <p className="muted">
        As of {formatDate(t.as_of)}, {t.region || "all regions"}, in {t.currency}. List prices as of{" "}
        {formatDate(t.list?.as_of)}. Storage is per GB-month and operations per 10,000, as the
        provider states them.
      </p>
      <div className="table-wrap">
        <table className="table price-edit" aria-label={`${label} rates`}>
          <thead>
            <tr>
              <th scope="col">Class</th>
              {FIELDS.map((f) => (
                <th key={f.key} scope="col" className="col-num">
                  {f.label}
                </th>
              ))}
              <th scope="col">
                <span className="sr-only">Reset</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {classes.map((cls) => {
              const differs = FIELDS.some(
                (f) => norm(value(cls, f.key)) !== norm(listValue(cls, f.key)),
              );
              return (
                <tr key={cls}>
                  <th scope="row">{cls}</th>
                  {FIELDS.map((f) => {
                    const v = value(cls, f.key);
                    const lp = listValue(cls, f.key);
                    const off = norm(v) !== norm(lp);
                    return (
                      <td key={f.key} className="col-num">
                        <input
                          className="price-input mono"
                          inputMode="decimal"
                          aria-label={`${cls} ${f.label.toLowerCase()}`}
                          aria-invalid={v !== "" && !DECIMAL.test(v)}
                          value={v}
                          onChange={(e) =>
                            setEdits((cur) => ({
                              ...cur,
                              [cls]: { ...cur[cls], [f.key]: e.target.value.trim() },
                            }))
                          }
                        />
                        {off ? <span className="price-list micro">list {lp ?? "none"}</span> : null}
                      </td>
                    );
                  })}
                  <td>
                    <button
                      type="button"
                      className="btn btn-link"
                      disabled={!differs}
                      aria-label={`Reset ${cls} to list price`}
                      onClick={() => resetClass(cls)}
                    >
                      Reset
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {bad ? <p className="field-error">Each rate is a number, like 0.0184.</p> : null}
      {save.isError ? <ErrorCallout error={save.error} /> : null}
      <div className="row">
        <button
          type="button"
          className="btn btn-primary"
          disabled={!dirty || bad || save.isPending}
          onClick={() =>
            save.mutate(
              { table: build(merged()) },
              {
                onSuccess: () => {
                  setEdits({});
                  toast(`${label} rates saved. Previews use them now.`);
                },
              },
            )
          }
        >
          Save rates
        </button>
        {dirty ? (
          <button type="button" className="btn btn-link" onClick={() => setEdits({})}>
            Undo edits
          </button>
        ) : null}
        <span className="spacer" />
        <button
          type="button"
          className="btn"
          disabled={!t.overridden || save.isPending}
          onClick={() =>
            save.mutate(
              { table: t, reset: true },
              {
                onSuccess: () => {
                  setEdits({});
                  toast(`${label} is back on list prices.`);
                },
              },
            )
          }
        >
          Reset all to list price
        </button>
      </div>
    </div>
  );
}
