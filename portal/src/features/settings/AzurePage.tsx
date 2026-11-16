import { useId, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { CheckIcon, XIcon } from "../../components/icons";
import { Modal } from "../../components/Modal";
import { formatDateTime, pluralize } from "../../lib/format";
import { Fact, StatusBadge } from "./common";
import {
  useAzureChoice,
  useAzureConfirm,
  useAzureDiscover,
  useAzureSettings,
  useAzureSignIn,
  type AzureDiscovery,
  type AzureGrant,
  type AzureGrantPlan,
  type AzureSettings,
  type ConnectStep,
} from "./cloudQueries";
import { notPrivateWhy } from "./privateWhy";

/**
 * Settings › Azure: Connect Azure (docs/design/cloud-connect.md 1). The admin
 * signs in to Azure, picks where, checks every grant before it is made, then
 * the service discovers storage and network with its own identity and the
 * admin confirms what OmniFile uses. No Azure token is ever kept.
 */

const IDENTITY: Record<string, string> = {
  web: "web (the portal and its checks)",
  worker: "worker (runs migrations and Storage Mover)",
};
const identityLabel = (p: string) => IDENTITY[p] ?? p;

const PURPOSE: Record<string, string> = {
  list: "Listing what you can see in Azure",
  grant: "Making the grants",
  remove: "Removing grants",
};

const lastSegment = (id: string | null) => (id ? (id.split("/").pop() ?? id) : "—");

function Step({
  n,
  title,
  why,
  children,
}: {
  n: number;
  title: string;
  why: ReactNode;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section className="card card-pad stack" aria-labelledby={id}>
      <h3 id={id}>
        <span className="muted">{n}.</span> {title}
      </h3>
      <p className="muted">{why}</p>
      {children}
    </section>
  );
}

/** The service's own steps, as a stepper: done, the one to do now, then the rest. */
function Progress({ steps }: { steps: ConnectStep[] }) {
  const current = steps.findIndex((s) => s.status !== "done");
  return (
    <div className="card card-pad stack">
      <ol
        className="steps"
        aria-label="Connect Azure progress"
        style={{ gridTemplateColumns: `repeat(${steps.length}, minmax(0, 1fr))` }}
      >
        {steps.map((s, i) => {
          const status =
            s.status === "done"
              ? "done"
              : s.status === "attention"
                ? "failed"
                : i === current
                  ? "current"
                  : "pending";
          return (
            <li
              key={s.code}
              className="step"
              data-status={status}
              aria-current={status === "current" ? "step" : undefined}
            >
              <span className="step-dot" aria-hidden="true">
                {status === "done" ? <CheckIcon /> : status === "failed" ? <XIcon /> : i + 1}
              </span>
              <span>
                <span className="step-name">{s.title}</span>
                <span className="sr-only">
                  {" "}
                  —{" "}
                  {s.status === "done"
                    ? "done"
                    : s.status === "attention"
                      ? "needs attention"
                      : "to do"}
                </span>
              </span>
            </li>
          );
        })}
      </ol>
      {steps
        .filter((s) => s.status === "attention")
        .map((s) => (
          <Callout key={s.code} tone="warn" title={s.title}>
            {s.detail}
          </Callout>
        ))}
    </div>
  );
}

/** What the last sign-in did, shown when Entra sends the admin back here (?op=). */
function LastOp({ last, onDismiss }: { last: AzureSettings["last_op"]; onDismiss: () => void }) {
  if (!last) return null;
  const what = PURPOSE[last.purpose] ?? last.purpose;
  const tone =
    last.state === "done"
      ? "ok"
      : last.state === "failed"
        ? "danger"
        : last.state === "expired"
          ? "warn"
          : "info";
  const title =
    last.state === "done"
      ? `${what}: done`
      : last.state === "failed"
        ? `${what}: failed`
        : last.state === "expired"
          ? `${what}: the sign-in expired`
          : `${what}: still finishing`;
  return (
    <Callout tone={tone} role="status" title={title}>
      <p>
        {last.detail ??
          (last.state === "expired"
            ? "A sign-in has 10 minutes to come back. Start it again."
            : last.state === "started"
              ? "The service hasn't finished this sign-in yet. Reload in a moment."
              : "")}
      </p>
      {last.finished_at ? <p className="muted">At {formatDateTime(last.finished_at)}</p> : null}
      <button type="button" className="btn btn-sm" onClick={onDismiss} style={{ marginTop: 8 }}>
        Dismiss
      </button>
    </Callout>
  );
}

function SignInStep({ s }: { s: AzureSettings }) {
  const signIn = useAzureSignIn();
  const l = s.listing;
  return (
    <Step
      n={1}
      title="Sign in to Azure"
      why="So the service can list the tenants, subscriptions and resource groups you can see. You sign in with your own account, in Microsoft's page; the service uses that sign-in for this one listing and then drops it."
    >
      {l ? (
        <p>
          Listed {formatDateTime(l.listed_at)}
          {l.listed_by ? ` by ${l.listed_by}` : ""}: {pluralize(l.tenants.length, "tenant")},{" "}
          {pluralize(l.subscriptions.length, "subscription")} and{" "}
          {pluralize(l.resource_groups.length, "resource group")}.
          {l.truncated ? " The list was cut short; only the first ones are shown." : ""}
        </p>
      ) : null}
      <div>
        <button
          type="button"
          className={l ? "btn" : "btn btn-primary"}
          disabled={signIn.isPending}
          onClick={() => signIn.mutate({ purpose: "list" })}
        >
          {signIn.isPending
            ? "Opening Microsoft sign-in…"
            : l
              ? "Sign in again to refresh the list"
              : "Sign in to Azure"}
        </button>
      </div>
      {signIn.isError ? (
        <ErrorCallout error={signIn.error} title="The sign-in didn't start" />
      ) : null}
    </Step>
  );
}

function ChooseStep({ s }: { s: AzureSettings }) {
  const listing = s.listing;
  const choice = s.choice;
  const save = useAzureChoice();
  const ids = { tenant: useId(), sub: useId(), mover: useId(), moverHint: useId(), rgs: useId() };
  const firstTenant = listing?.tenants.find((t) => t.selectable)?.tenant_id ?? "";
  const subTenant = (subId: string) =>
    listing?.subscriptions.find((x) => x.subscription_id === subId)?.tenant_id ?? null;
  const [tenant, setTenant] = useState(
    (choice && subTenant(choice.subscription_id)) || firstTenant,
  );
  const [sub, setSub] = useState(choice?.subscription_id ?? "");
  const [rgs, setRgs] = useState<string[]>(choice?.resource_groups ?? []);
  const [mover, setMover] = useState(choice?.mover_resource_group ?? "");
  if (!listing) return null;

  const subs = listing.subscriptions.filter((x) => !x.tenant_id || x.tenant_id === tenant);
  const subId = subs.some((x) => x.subscription_id === sub)
    ? sub
    : subs.length === 1
      ? (subs[0]?.subscription_id ?? "")
      : "";
  const groups = listing.resource_groups.filter((g) => g.subscription_id === subId);
  const valid = !!subId && rgs.length > 0;

  return (
    <Step
      n={2}
      title="Choose where"
      why="Pick the subscription, the resource groups that hold your storage accounts and network, and, if you want the portal to set up Azure Storage Mover, one resource group for it. Nothing is granted yet."
    >
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (!valid) return;
          save.mutate({
            subscription_id: subId,
            resource_groups: rgs.filter((r) => groups.some((g) => g.name === r)),
            mover_resource_group: mover || null,
          });
        }}
      >
        <fieldset className="options">
          <legend className="field-label">Tenant</legend>
          {listing.tenants.map((t) => {
            const hint = `${ids.tenant}-${t.tenant_id}`;
            return (
              <label key={t.tenant_id} className="option">
                <input
                  type="radio"
                  name={ids.tenant}
                  checked={tenant === t.tenant_id}
                  disabled={!t.selectable}
                  aria-describedby={hint}
                  onChange={() => {
                    setTenant(t.tenant_id);
                    setSub("");
                    setRgs([]);
                    setMover("");
                  }}
                />
                <span className="option-title">
                  {t.display_name ?? t.tenant_id} <span className="mono muted">{t.tenant_id}</span>
                </span>
                <span id={hint} className="option-body">
                  {t.selectable
                    ? "OmniFile's own tenant: its managed identities live here."
                    : (t.reason ?? "Can't be chosen.")}
                </span>
              </label>
            );
          })}
        </fieldset>
        <div className="grid-2">
          <div className="field">
            <label className="field-label" htmlFor={ids.sub}>
              Subscription
            </label>
            <select
              id={ids.sub}
              value={subId}
              onChange={(e) => {
                setSub(e.target.value);
                setRgs([]);
                setMover("");
              }}
            >
              <option value="">Choose a subscription</option>
              {subs.map((x) => (
                <option key={x.subscription_id} value={x.subscription_id}>
                  {x.display_name ?? x.subscription_id} ({x.subscription_id})
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor={ids.mover}>
              Resource group for Storage Mover (optional)
            </label>
            <select
              id={ids.mover}
              value={mover}
              disabled={!subId}
              aria-describedby={ids.moverHint}
              onChange={(e) => setMover(e.target.value)}
            >
              <option value="">None: I set Storage Mover up myself</option>
              {groups.map((g) => (
                <option key={g.name} value={g.name}>
                  {g.name}
                </option>
              ))}
            </select>
            <span id={ids.moverHint} className="field-hint">
              The worker may then create a Storage Mover and its project there, and run its jobs.
            </span>
          </div>
        </div>
        <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="field-label">Resource groups with your storage and network</legend>
          {!subId ? (
            <p className="muted">Choose the subscription first.</p>
          ) : groups.length === 0 ? (
            <p className="muted">You can see no resource groups in this subscription.</p>
          ) : (
            groups.map((g) => (
              <label key={g.name} className="check-row">
                <input
                  type="checkbox"
                  checked={rgs.includes(g.name)}
                  onChange={(e) =>
                    setRgs((prev) =>
                      e.target.checked ? [...prev, g.name] : prev.filter((x) => x !== g.name),
                    )
                  }
                />
                <span>
                  {g.name} {g.location ? <span className="muted">({g.location})</span> : null}
                </span>
              </label>
            ))
          )}
        </fieldset>
        <div>
          <button type="submit" className="btn btn-primary" disabled={!valid || save.isPending}>
            {save.isPending ? "Saving…" : "Show the grants this needs"}
          </button>
        </div>
        {choice ? (
          <p className="muted">
            Chosen {formatDateTime(choice.chosen_at)}
            {choice.chosen_by ? ` by ${choice.chosen_by}` : ""}.
          </p>
        ) : null}
        {save.isError ? <ErrorCallout error={save.error} title="The choice wasn't saved" /> : null}
      </form>
    </Step>
  );
}

function PlanItem({ item }: { item: AzureGrantPlan }) {
  const id = useId();
  return (
    <article className="card card-pad stack" aria-labelledby={id}>
      <div className="row" style={{ gap: 8 }}>
        <StatusBadge status={item.state} />
        <strong id={id}>{item.role_name}</strong>
      </div>
      <dl className="kv">
        <Fact label="Scope">
          <span className="mono">{item.scope}</span>
        </Fact>
        <Fact label="Given to">
          {identityLabel(item.principal)}
          {item.principal_id ? (
            <>
              {", object id "}
              <span className="mono">{item.principal_id}</span>
            </>
          ) : (
            ", whose object id the deployment doesn't name"
          )}
        </Fact>
      </dl>
      <details className="more">
        <summary>Every action in this role ({item.actions.length})</summary>
        <ul className="more-body mono" aria-label={`Actions in ${item.role_name}`}>
          {item.actions.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
      </details>
    </article>
  );
}

function PlanStep({ s }: { s: AzureSettings }) {
  const signIn = useAzureSignIn();
  const planned = s.plan.filter((i) => i.state !== "active");
  const missing = s.plan.some((i) => i.state === "missing_identity");
  return (
    <Step
      n={3}
      title="Check the grants"
      why="Every role the service's own identities would get, where, and every action in it. Nothing is made until you sign in again and press the button below."
    >
      <Callout title="What is never granted">
        No data actions (no file is read or written through these roles), no keys, no deletes, and
        no permission to grant roles. So no service identity can give itself more.
      </Callout>
      <ul
        className="stack"
        aria-label="Planned grants"
        style={{ listStyle: "none", padding: 0, margin: 0 }}
      >
        {s.plan.map((item) => (
          <li key={item.key}>
            <PlanItem item={item} />
          </li>
        ))}
      </ul>
      {missing ? (
        <Callout tone="danger" title="The service's identities aren't known">
          The deployment doesn't set OMNIFILE_SERVICE_PRINCIPAL_IDS (web=…,worker=…), so these
          grants can&apos;t be made. Re-deploy with the managed identities&apos; object ids.
        </Callout>
      ) : null}
      {planned.length === 0 ? (
        <Callout tone="ok" title="Every grant is made" />
      ) : (
        <>
          <p>
            You need Owner or User Access Administrator on each resource group. The service checks
            that first and makes nothing if you don&apos;t have it. The sign-in counts as fresh: it
            has to be within the last 10 minutes.
          </p>
          <div>
            <button
              type="button"
              className="btn btn-primary"
              disabled={missing || signIn.isPending}
              onClick={() => signIn.mutate({ purpose: "grant" })}
            >
              {signIn.isPending
                ? "Opening Microsoft sign-in…"
                : `Sign in and make ${planned.length === 1 ? "this grant" : "these grants"}`}
            </button>
          </div>
        </>
      )}
      {signIn.isError ? (
        <ErrorCallout error={signIn.error} title="The sign-in didn't start" />
      ) : null}
    </Step>
  );
}

function GrantsStep({ grants }: { grants: AzureGrant[] }) {
  const signIn = useAzureSignIn();
  const [removing, setRemoving] = useState<AzureGrant | null>(null);
  return (
    <Step
      n={4}
      title="Grants made"
      why="Each grant is audited. Remove takes it away again, with its own sign-in."
    >
      <div className="table-wrap">
        <table className="table" aria-label="Grants">
          <thead>
            <tr>
              <th scope="col">Role</th>
              <th scope="col">Scope</th>
              <th scope="col">Given to</th>
              <th scope="col">Made</th>
              <th scope="col">State</th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {grants.map((g) => (
              <tr key={g.grant_id}>
                <th scope="row">{g.role_name}</th>
                <td className="mono">{g.scope}</td>
                <td>{g.principal}</td>
                <td>
                  {formatDateTime(g.made_at)}
                  {g.made_by ? ` by ${g.made_by}` : ""}
                </td>
                <td>
                  <StatusBadge status={g.state} />
                  {g.removed_at ? (
                    <span className="muted">
                      {" "}
                      {formatDateTime(g.removed_at)}
                      {g.removed_by ? ` by ${g.removed_by}` : ""}
                    </span>
                  ) : null}
                </td>
                <td>
                  {g.state === "active" ? (
                    <button
                      type="button"
                      className="btn btn-sm"
                      aria-label={`Remove ${g.role_name}`}
                      onClick={() => setRemoving(g)}
                    >
                      Remove
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {signIn.isError ? (
        <ErrorCallout error={signIn.error} title="The sign-in didn't start" />
      ) : null}
      <Modal
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title="Remove this grant?"
        description={removing ? removing.role_name : undefined}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setRemoving(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={signIn.isPending}
              onClick={() => {
                if (!removing) return;
                signIn.mutate({ purpose: "remove", grant_ids: [removing.grant_id] });
                setRemoving(null);
              }}
            >
              Sign in and remove
            </button>
          </>
        }
      >
        <p>
          You sign in to Azure again. The service deletes the role assignment, and the custom role
          when no other grant uses it. What needed it stops working there:{" "}
          {removing?.purpose === "storage_mover"
            ? "the worker can't create Storage Mover or run its jobs in this resource group."
            : "the portal can't read the storage and network in this resource group."}
        </p>
      </Modal>
    </Step>
  );
}

function DiscoveryTables({ d }: { d: AzureDiscovery }) {
  return (
    <>
      {d.errors.length ? (
        <Callout tone="warn" title="Some resource groups couldn't be read">
          <ul>
            {d.errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </Callout>
      ) : null}
      <div className="table-wrap">
        <table className="table" aria-label="Storage accounts">
          <thead>
            <tr>
              <th scope="col">Storage account</th>
              <th scope="col">Resource group</th>
              <th scope="col">Location</th>
              <th scope="col">Redundancy</th>
              <th scope="col">Blob private endpoint</th>
              <th scope="col">Public access</th>
              <th scope="col">DNS zone linked</th>
              <th scope="col">Reachable privately</th>
            </tr>
          </thead>
          <tbody>
            {d.storage_accounts.length === 0 ? (
              <tr>
                <td colSpan={8}>No storage accounts.</td>
              </tr>
            ) : (
              d.storage_accounts.map((a) => (
                <tr key={a.id}>
                  <th scope="row">{a.name}</th>
                  <td>{a.resource_group}</td>
                  <td>{a.location ?? "—"}</td>
                  <td>
                    {a.sku ?? "—"} {a.zrs ? <span className="badge badge-info">ZRS</span> : null}
                  </td>
                  <td>{a.blob_private_endpoint ? "Yes, approved" : "None"}</td>
                  <td>{a.public_network_access ?? "—"}</td>
                  <td>{a.dns_linked ? "Yes" : "No"}</td>
                  <td>
                    {a.private ? (
                      <span className="badge badge-ok">Private</span>
                    ) : (
                      <span className="badge badge-warn">Not private</span>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table className="table" aria-label="Virtual networks">
          <thead>
            <tr>
              <th scope="col">VNet</th>
              <th scope="col">Resource group</th>
              <th scope="col">Address space</th>
              <th scope="col">Subnets</th>
            </tr>
          </thead>
          <tbody>
            {d.virtual_networks.length === 0 ? (
              <tr>
                <td colSpan={4}>No VNets.</td>
              </tr>
            ) : (
              d.virtual_networks.map((v) => (
                <tr key={v.id}>
                  <th scope="row">{v.name}</th>
                  <td>{v.resource_group}</td>
                  <td className="mono">{v.address_prefixes.join(", ") || "—"}</td>
                  <td className="mono">{v.subnets.join(", ") || "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table className="table" aria-label="Private endpoints">
          <thead>
            <tr>
              <th scope="col">Private endpoint</th>
              <th scope="col">Resource group</th>
              <th scope="col">Connects to</th>
              <th scope="col">For</th>
              <th scope="col">Subnet</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {d.private_endpoints.length === 0 ? (
              <tr>
                <td colSpan={6}>No private endpoints.</td>
              </tr>
            ) : (
              d.private_endpoints.map((p) => (
                <tr key={p.id}>
                  <th scope="row">{p.name}</th>
                  <td>{p.resource_group}</td>
                  <td>{lastSegment(p.target_id)}</td>
                  <td>{p.group_ids.join(", ") || "—"}</td>
                  <td>{lastSegment(p.subnet_id)}</td>
                  <td>{p.status ?? "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table className="table" aria-label="Storage movers">
          <thead>
            <tr>
              <th scope="col">Storage mover</th>
              <th scope="col">Resource group</th>
              <th scope="col">Location</th>
            </tr>
          </thead>
          <tbody>
            {d.storage_movers.length === 0 ? (
              <tr>
                <td colSpan={3}>None yet. Settings › Storage Mover can deploy one.</td>
              </tr>
            ) : (
              d.storage_movers.map((m) => (
                <tr key={m.id}>
                  <th scope="row">{m.name}</th>
                  <td>{m.resource_group}</td>
                  <td>{m.location ?? "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}

function DiscoverStep({ s }: { s: AzureSettings }) {
  const discover = useAzureDiscover();
  const d = s.discovered;
  return (
    <Step
      n={5}
      title="Discover storage and network"
      why="The service reads, with its own identity and the Discovery Reader roles just granted, the storage accounts, VNets, private endpoints and storage movers in those resource groups. It reads only; it changes nothing."
    >
      <div className="row">
        <button
          type="button"
          className={d ? "btn" : "btn btn-primary"}
          disabled={discover.isPending}
          onClick={() => discover.mutate()}
        >
          {discover.isPending ? "Discovering…" : d ? "Discover again" : "Discover"}
        </button>
        {d ? <span className="muted">Last read {formatDateTime(d.discovered_at)}</span> : null}
      </div>
      {discover.isError ? <ErrorCallout error={discover.error} title="Discovery failed" /> : null}
      {d ? <DiscoveryTables d={d} /> : null}
    </Step>
  );
}

function ConfirmStep({ s }: { s: AzureSettings }) {
  const d = s.discovered;
  const confirm = useAzureConfirm();
  const base = useId();
  const [accounts, setAccounts] = useState<string[]>(s.confirmed?.storage_accounts ?? []);
  const [vnets, setVnets] = useState<string[]>(s.confirmed?.virtual_networks ?? []);
  const [endpoints, setEndpoints] = useState<string[]>(s.confirmed?.private_endpoints ?? []);
  if (!d) return null;
  const toggle =
    (set: (fn: (prev: string[]) => string[]) => void, id: string) =>
    (e: { target: { checked: boolean } }) =>
      set((prev) => (e.target.checked ? [...prev, id] : prev.filter((x) => x !== id)));

  return (
    <Step
      n={6}
      title="Confirm what OmniFile uses"
      why="Tick what OmniFile should use. Only storage accounts reachable privately can be ticked (decision 22). The confirmation is audited; the Storage page then offers the ticked accounts when you add a backend."
    >
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          confirm.mutate({
            storage_accounts: accounts,
            virtual_networks: vnets,
            private_endpoints: endpoints,
          });
        }}
      >
        <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="field-label">Storage accounts</legend>
          {d.storage_accounts.map((a) => {
            const hint = `${base}-sa-${a.name}`;
            return (
              <label
                key={a.id}
                className="check-row"
                style={a.private ? undefined : { cursor: "not-allowed" }}
              >
                <input
                  type="checkbox"
                  checked={accounts.includes(a.id)}
                  disabled={!a.private}
                  aria-describedby={hint}
                  onChange={toggle(setAccounts, a.id)}
                />
                <span>
                  {a.name} <span className="muted">({a.resource_group})</span>
                  <span id={hint} className="field-hint">
                    {a.private
                      ? `Reachable privately: an approved blob private endpoint${a.zrs ? ", zone-redundant" : ""}.`
                      : `Can't be used: not reachable privately. ${notPrivateWhy(a)}`}
                  </span>
                </span>
              </label>
            );
          })}
        </fieldset>
        {d.virtual_networks.length ? (
          <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="field-label">Virtual networks</legend>
            {d.virtual_networks.map((v) => (
              <label key={v.id} className="check-row">
                <input
                  type="checkbox"
                  checked={vnets.includes(v.id)}
                  onChange={toggle(setVnets, v.id)}
                />
                <span>
                  {v.name} <span className="muted mono">{v.address_prefixes.join(", ")}</span>
                </span>
              </label>
            ))}
          </fieldset>
        ) : null}
        {d.private_endpoints.length ? (
          <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="field-label">Private endpoints</legend>
            {d.private_endpoints.map((p) => (
              <label key={p.id} className="check-row">
                <input
                  type="checkbox"
                  checked={endpoints.includes(p.id)}
                  onChange={toggle(setEndpoints, p.id)}
                />
                <span>
                  {p.name}{" "}
                  <span className="muted">
                    ({p.group_ids.join(", ")} for {lastSegment(p.target_id)})
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
        ) : null}
        <div>
          <button type="submit" className="btn btn-primary" disabled={confirm.isPending}>
            {confirm.isPending ? "Confirming…" : "Confirm"}
          </button>
        </div>
        {confirm.isError ? (
          <ErrorCallout error={confirm.error} title="The confirmation wasn't recorded" />
        ) : null}
        {s.confirmed ? (
          <Callout tone="ok" role="status" title="Confirmed">
            <p>
              {pluralize(s.confirmed.storage_accounts.length, "storage account")},{" "}
              {pluralize(s.confirmed.virtual_networks.length, "VNet")} and{" "}
              {pluralize(s.confirmed.private_endpoints.length, "private endpoint")}, on{" "}
              {formatDateTime(s.confirmed.confirmed_at)}
              {s.confirmed.confirmed_by ? ` by ${s.confirmed.confirmed_by}` : ""}.
            </p>
            <p>
              Register an account as a backend on the <Link to="/settings/storage">Storage</Link>{" "}
              page.
            </p>
          </Callout>
        ) : null}
      </form>
    </Step>
  );
}

export function AzurePage() {
  usePageTitle("Settings: Azure");
  const az = useAzureSettings();
  const [params, setParams] = useSearchParams();
  const op = params.get("op");
  if (az.isPending) return <Loading />;
  if (az.isError) return <ErrorCallout error={az.error} />;
  const s = az.data;
  const active = s.grants.some((g) => g.state === "active");
  // Keyed by the server's state, so a new listing or choice resets the forms.
  const formKey = `${s.listing?.listed_at ?? ""}|${s.choice?.chosen_at ?? ""}`;

  return (
    <section className="stack" aria-labelledby="azure-h">
      <h2 id="azure-h">Azure</h2>
      <p className="lede">
        Connect Azure finds your storage accounts, networks and Storage Mover, and gives the
        service&apos;s own managed identities a few read-mostly roles where you choose.
      </p>
      <Callout title="OmniFile never keeps your Azure sign-in">
        Each step that needs your Azure rights asks you to sign in, in Microsoft&apos;s own page.
        The service uses that sign-in for the one request it started and then drops it: no token,
        refresh token or key is stored. Everything after that runs as the service&apos;s own managed
        identities.
      </Callout>
      {op ? (
        <LastOp
          last={s.last_op}
          onDismiss={() => {
            const next = new URLSearchParams(params);
            next.delete("op");
            setParams(next, { replace: true });
          }}
        />
      ) : null}
      <Progress steps={s.steps} />
      <dl className="kv card card-pad">
        <Fact label="Tenant">
          <span className="mono">{s.tenant_id}</span>
        </Fact>
        {s.identities.map((i) => (
          <Fact key={i.name} label={`Identity: ${i.name}`}>
            {i.object_id ? (
              <span className="mono">{i.object_id}</span>
            ) : (
              "not named by the deployment"
            )}
          </Fact>
        ))}
      </dl>
      <SignInStep s={s} />
      {s.listing ? <ChooseStep key={formKey} s={s} /> : null}
      {s.plan.length ? <PlanStep s={s} /> : null}
      {s.grants.length ? <GrantsStep grants={s.grants} /> : null}
      {active || s.discovered ? <DiscoverStep s={s} /> : null}
      {s.discovered ? (
        <ConfirmStep
          key={`${s.discovered.discovered_at}|${s.confirmed?.confirmed_at ?? ""}`}
          s={s}
        />
      ) : null}
    </section>
  );
}
