import { useId, useState } from "react";
import { Link, useParams } from "react-router";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toasts";
import { formatDateTime, formatRelative } from "../../lib/format";
import { useDebounced } from "../../lib/useDebounced";
import { KeysSection } from "./ApiKeys";
import { Fact, StatusBadge } from "./common";
import {
  useApiClient,
  useApiClients,
  useEditClient,
  usePeople,
  useRegisterClient,
  useRevokeClient,
  type ApiClient,
  type ApiClientOwner,
  type ApiClientCreate,
} from "./queries";

export const SCOPES = [
  { value: "Policy.Propose", label: "Propose policies (an ArchiveAdmin approves them)" },
  { value: "Recall.Request", label: "Request recalls" },
  { value: "Archive.Read", label: "Read the catalog, events and usage" },
  { value: "Archive.Adopt", label: "Register sources and run migrations" },
] as const;
type Scope = ApiClientCreate["scopes"][number];

const GUID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

const OWNER_RULE =
  "The owner is the person answerable for this client. A proposal from it is refused unless the owner can see the dataset it targets (ArchiveAdmin, DataSteward, or Owner of one of its shares).";

function ownerName(o: ApiClientOwner): string {
  return o.display_name ?? o.upn ?? `identity ${o.identity_id}`;
}

function Setup() {
  const clients = useApiClients();
  const s = clients.data?.setup;
  if (!s) return null;
  return (
    <div className="card card-pad stack" aria-labelledby="setup-h">
      <h3 id="setup-h">How a client signs in</h3>
      <Callout title="Entra app credentials are the recommended way">
        A client authenticates with its own Entra app credential, preferably a certificate: nothing
        is stored or shared. Registering a client says which app may call, with which scopes, and
        who answers for it.
      </Callout>
      <p>
        <strong>The alternative: an OmniFile API key.</strong> Only for a client that can&apos;t
        hold an Entra credential. Create one on the client&apos;s page; OmniFile keeps only its
        hash, shows it once, and the client sends it as{" "}
        <code>Authorization: Bearer &lt;key&gt;</code>, never in a URL. Keys expire, and can be
        rotated and revoked.
      </p>
      <dl className="kv">
        <Fact label="Token endpoint">
          <span className="mono">{s.token_endpoint}</span>
        </Fact>
        <Fact label="Scope to request">
          <span className="mono">{s.scope ?? "not set (no API audience configured)"}</span>
        </Fact>
        <Fact label="Token audiences">
          <span className="mono">{s.audiences.join(", ") || "none"}</span>
        </Fact>
        <Fact label="App roles">{s.app_roles.join(", ")}</Fact>
      </dl>
      <ol>
        {s.steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
    </div>
  );
}

export function OwnerPicker({
  value,
  onChange,
}: {
  value: ApiClientOwner | null;
  onChange: (o: ApiClientOwner | null) => void;
}) {
  const [q, setQ] = useState("");
  const debounced = useDebounced(q, 250);
  const people = usePeople(debounced);
  const id = useId();
  const hintId = useId();
  if (value) {
    return (
      <div className="field">
        <span className="field-label">Owner</span>
        <p className="row">
          <strong>{ownerName(value)}</strong>
          {value.upn ? <span className="muted">{value.upn}</span> : null}
          <span className="muted">{value.roles.join(", ") || "no app role seen"}</span>
          <button type="button" className="btn btn-link" onClick={() => onChange(null)}>
            Change owner
          </button>
        </p>
      </div>
    );
  }
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        Owner
      </label>
      <input
        id={id}
        type="search"
        autoComplete="off"
        value={q}
        aria-describedby={hintId}
        onChange={(e) => setQ(e.target.value)}
      />
      <p id={hintId} className="field-hint">
        Type a name or sign-in name. People appear once they have signed in to the portal or the
        directory sync has seen them.
      </p>
      {people.isError ? <ErrorCallout error={people.error} /> : null}
      {people.data ? (
        people.data.items.length === 0 ? (
          <p className="muted">Nobody matches.</p>
        ) : (
          <ul aria-label="People" style={{ listStyle: "none", padding: 0 }}>
            {people.data.items.map((p) => (
              <li key={p.identity_id}>
                <button type="button" className="btn btn-link" onClick={() => onChange(p)}>
                  {ownerName(p)}
                  {p.upn ? ` (${p.upn})` : ""}
                </button>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </div>
  );
}

function ScopeBoxes({ value, onChange }: { value: Scope[]; onChange: (v: Scope[]) => void }) {
  return (
    <fieldset className="field">
      <legend className="field-label">Scopes</legend>
      {SCOPES.map((s) => (
        <label key={s.value} className="row">
          <input
            type="checkbox"
            checked={value.includes(s.value)}
            onChange={(e) =>
              onChange(e.target.checked ? [...value, s.value] : value.filter((v) => v !== s.value))
            }
          />
          <span>
            <span className="mono">{s.value}</span>: {s.label}
          </span>
        </label>
      ))}
    </fieldset>
  );
}

export function RegisterClientDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const register = useRegisterClient();
  const toast = useToast();
  const [appId, setAppId] = useState("");
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<Scope[]>([]);
  const [owner, setOwner] = useState<ApiClientOwner | null>(null);
  const appIdId = useId();
  const nameId = useId();
  const valid = GUID.test(appId.trim()) && name.trim().length > 0 && owner !== null;

  function close() {
    register.reset();
    setAppId("");
    setName("");
    setScopes([]);
    setOwner(null);
    onClose();
  }

  return (
    <Modal
      open={open}
      onClose={close}
      wide
      title="Register an API client"
      description="An application that calls the OmniFile API with its own Entra token."
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!valid || register.isPending}
            onClick={() =>
              owner &&
              register.mutate(
                {
                  app_id: appId.trim().toLowerCase(),
                  display_name: name.trim(),
                  scopes,
                  owner_identity_id: owner.identity_id,
                },
                {
                  onSuccess: (c) => {
                    toast(`${c.display_name} is registered.`);
                    close();
                  },
                },
              )
            }
          >
            {register.isPending ? "Registering…" : "Register client"}
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="field">
          <label className="field-label" htmlFor={appIdId}>
            Application (client) id
          </label>
          <input
            id={appIdId}
            type="text"
            className="mono"
            autoComplete="off"
            spellCheck={false}
            value={appId}
            aria-invalid={appId.length > 0 && !GUID.test(appId.trim())}
            onChange={(e) => setAppId(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={nameId}>
            Display name
          </label>
          <input
            id={nameId}
            type="text"
            maxLength={80}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <ScopeBoxes value={scopes} onChange={setScopes} />
        <OwnerPicker value={owner} onChange={setOwner} />
        <p className="muted">{OWNER_RULE}</p>
        {register.isError ? (
          <ErrorCallout error={register.error} title="The client wasn't registered" />
        ) : null}
      </div>
    </Modal>
  );
}

export function ApiClientsPage() {
  usePageTitle("Settings: API clients");
  const clients = useApiClients();
  const [adding, setAdding] = useState(false);
  return (
    <section className="stack" aria-labelledby="clients-h">
      <div className="page-head">
        <div>
          <h2 id="clients-h">API clients</h2>
          <p className="lede">
            Applications that may call the API. A token is accepted only from a client registered
            here and enabled, and only for the scopes ticked here.
          </p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}>
          Register client
        </button>
      </div>
      <RegisterClientDialog open={adding} onClose={() => setAdding(false)} />
      {clients.isPending ? (
        <Loading />
      ) : clients.isError ? (
        <ErrorCallout error={clients.error} />
      ) : clients.data.items.length === 0 ? (
        <p className="card card-pad">No API clients yet. Every API call is refused until one is.</p>
      ) : (
        <div className="table-wrap card">
          <table className="table" aria-label="API clients">
            <thead>
              <tr>
                <th scope="col">Client</th>
                <th scope="col">State</th>
                <th scope="col">Scopes</th>
                <th scope="col">Owner</th>
                <th scope="col">Last used</th>
              </tr>
            </thead>
            <tbody>
              {clients.data.items.map((c) => (
                <tr key={c.api_client_id}>
                  <th scope="row">
                    <Link to={`/settings/api-clients/${c.api_client_id}`}>{c.display_name}</Link>
                    <div className="muted mono">{c.app_id}</div>
                  </th>
                  <td>
                    <StatusBadge status={c.state} />
                  </td>
                  <td className="mono">{c.scopes.join(", ") || "none"}</td>
                  <td>{ownerName(c.owner)}</td>
                  <td>{c.last_used_at ? formatRelative(c.last_used_at) : "never"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Setup />
    </section>
  );
}

function EditClientDialog({ client, onClose }: { client: ApiClient | null; onClose: () => void }) {
  const edit = useEditClient();
  const toast = useToast();
  const [name, setName] = useState(client?.display_name ?? "");
  const [scopes, setScopes] = useState<Scope[]>(client?.scopes ?? []);
  const [owner, setOwner] = useState<ApiClientOwner | null>(client?.owner ?? null);
  const [shownFor, setShownFor] = useState(client);
  if (shownFor !== client) {
    setShownFor(client);
    setName(client?.display_name ?? "");
    setScopes(client?.scopes ?? []);
    setOwner(client?.owner ?? null);
  }
  const nameId = useId();
  function close() {
    edit.reset();
    onClose();
  }
  return (
    <Modal
      open={client !== null}
      onClose={close}
      wide
      title={`Edit ${client?.display_name ?? ""}`}
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!client || !owner || !name.trim() || edit.isPending}
            onClick={() =>
              client &&
              owner &&
              edit.mutate(
                {
                  id: client.api_client_id,
                  etag: client.etag,
                  body: {
                    display_name: name.trim(),
                    scopes,
                    owner_identity_id: owner.identity_id,
                  },
                },
                {
                  onSuccess: () => {
                    toast("Saved.");
                    close();
                  },
                },
              )
            }
          >
            {edit.isPending ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <div className="stack">
        <div className="field">
          <label className="field-label" htmlFor={nameId}>
            Display name
          </label>
          <input
            id={nameId}
            type="text"
            maxLength={80}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <ScopeBoxes value={scopes} onChange={setScopes} />
        <OwnerPicker value={owner} onChange={setOwner} />
        <p className="muted">{OWNER_RULE}</p>
        {edit.isError ? <ErrorCallout error={edit.error} title="Nothing was saved" /> : null}
      </div>
    </Modal>
  );
}

function RevokeClientDialog({
  client,
  onClose,
}: {
  client: ApiClient | null;
  onClose: () => void;
}) {
  const revoke = useRevokeClient();
  const toast = useToast();
  function close() {
    revoke.reset();
    onClose();
  }
  return (
    <Modal
      open={client !== null}
      onClose={close}
      title={`Revoke ${client?.display_name ?? ""}?`}
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={revoke.isPending}
            onClick={() =>
              client &&
              revoke.mutate(client.api_client_id, {
                onSuccess: () => {
                  toast(`${client.display_name} was revoked.`);
                  close();
                },
              })
            }
          >
            {revoke.isPending ? "Revoking…" : "Revoke client"}
          </button>
        </>
      }
    >
      <div className="stack">
        <p>
          Its next call is refused, and it can&apos;t be enabled again. Its proposals and audit
          records stay. To stop it for now instead, disable it.
        </p>
        {revoke.isError ? <ErrorCallout error={revoke.error} title="It wasn't revoked" /> : null}
      </div>
    </Modal>
  );
}

export function ApiClientPage() {
  const { apiClientId } = useParams();
  const client = useApiClient(apiClientId);
  const edit = useEditClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [revoking, setRevoking] = useState(false);
  usePageTitle(client.data ? `API client ${client.data.display_name}` : "API client");
  if (client.isPending) return <Loading />;
  if (client.isError) return <ErrorCallout error={client.error} />;
  const c = client.data;
  const revoked = c.state === "revoked";
  return (
    <section className="stack" aria-labelledby="client-h">
      <p>
        <Link to="/settings/api-clients">All API clients</Link>
      </p>
      <div className="page-head">
        <div>
          <h2 id="client-h">{c.display_name}</h2>
          <p>
            <StatusBadge status={c.state} />
          </p>
        </div>
        {revoked ? null : (
          <div className="row">
            <button
              type="button"
              className="btn"
              disabled={edit.isPending}
              onClick={() =>
                edit.mutate(
                  {
                    id: c.api_client_id,
                    etag: c.etag,
                    body: { enabled: c.state !== "enabled" },
                  },
                  {
                    onSuccess: (n) =>
                      toast(
                        n.state === "enabled"
                          ? `${n.display_name} is enabled.`
                          : `${n.display_name} is disabled. Its next call is refused.`,
                      ),
                  },
                )
              }
            >
              {c.state === "enabled" ? "Disable" : "Enable"}
            </button>
            <button type="button" className="btn" onClick={() => setEditing(true)}>
              Edit
            </button>
            <button type="button" className="btn btn-danger" onClick={() => setRevoking(true)}>
              Revoke…
            </button>
          </div>
        )}
      </div>
      {edit.isError ? <ErrorCallout error={edit.error} /> : null}
      <dl className="kv card card-pad">
        <Fact label="Application (client) id">
          <span className="mono">{c.app_id}</span>
        </Fact>
        <Fact label="Scopes">
          <span className="mono">{c.scopes.join(", ") || "none"}</span>
        </Fact>
        <Fact label="Owner">
          {ownerName(c.owner)}
          {c.owner.upn ? ` (${c.owner.upn})` : ""}
        </Fact>
        <Fact label="Owner's app roles">
          {c.owner.roles.join(", ") || "none seen"}
          {c.owner.roles_seen_at ? `, seen ${formatRelative(c.owner.roles_seen_at)}` : ""}
        </Fact>
        <Fact label="Registered">
          {formatDateTime(c.created_at)} by {c.created_by.display_name ?? "an admin"}
        </Fact>
        <Fact label="Last used">{c.last_used_at ? formatDateTime(c.last_used_at) : "never"}</Fact>
        {c.disabled_at ? <Fact label="Disabled">{formatDateTime(c.disabled_at)}</Fact> : null}
        {c.revoked_at ? <Fact label="Revoked">{formatDateTime(c.revoked_at)}</Fact> : null}
      </dl>
      <p className="muted">{OWNER_RULE}</p>
      <KeysSection client={c} />
      <EditClientDialog client={editing ? c : null} onClose={() => setEditing(false)} />
      <RevokeClientDialog client={revoking ? c : null} onClose={() => setRevoking(false)} />
    </section>
  );
}
