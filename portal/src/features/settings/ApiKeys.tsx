import { useId, useState } from "react";
import { Callout, ErrorCallout } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toasts";
import { formatDateTime } from "../../lib/format";
import { CopyButton, StatusBadge } from "./common";
import {
  useCreateKey,
  useRevokeKey,
  useRotateKey,
  type ApiClient,
  type ApiKey,
  type ApiKeyCreate,
} from "./queries";

type Scope = NonNullable<ApiKeyCreate["scopes"]>[number];

export const KEYS_RECOMMENDATION =
  "Entra app credentials are the recommended way: nothing is stored or shared. Use an OmniFile API key only when the client can't hold an Entra credential.";

/**
 * A key, shown once. It lives only in this component's parent state while the
 * dialog is open; never in the query cache, the URL, storage or a toast.
 */
interface Shown {
  key: string;
  keyId: string;
  previous: ApiKey | null;
}

function KeyShownDialog({ shown, onClose }: { shown: Shown | null; onClose: () => void }) {
  const keyId = useId();
  return (
    <Modal
      open={shown !== null}
      onClose={onClose}
      wide
      title="Your new API key"
      footer={
        <button type="button" className="btn btn-primary" onClick={onClose}>
          Done
        </button>
      }
    >
      {shown ? (
        <div className="stack">
          <Callout tone="warn">
            Copy this key now. OmniFile keeps only a hash and can&apos;t show it again. Send it as{" "}
            <code>Authorization: Bearer &lt;key&gt;</code>, never in a URL.
          </Callout>
          <div className="field">
            <label className="field-label" htmlFor={keyId}>
              API key {shown.keyId}
            </label>
            <textarea
              id={keyId}
              className="mono"
              readOnly
              rows={3}
              value={shown.key}
              autoComplete="off"
              spellCheck={false}
              onFocus={(e) => e.currentTarget.select()}
            />
          </div>
          <div>
            <CopyButton text={shown.key} label="Copy key" />
          </div>
          {shown.previous ? (
            <p>
              The previous key, <span className="mono">{shown.previous.key_id}</span>, keeps working
              until {formatDateTime(shown.previous.expires_at)}. Move the client to the new key
              before then.
            </p>
          ) : null}
        </div>
      ) : null}
    </Modal>
  );
}

function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  hint,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  hint: string;
}) {
  const id = useId();
  const hintId = useId();
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        step={1}
        value={value}
        aria-describedby={hintId}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <p id={hintId} className="field-hint">
        {hint}
      </p>
    </div>
  );
}

const inRange = (v: number, min: number, max: number) =>
  Number.isInteger(v) && v >= min && v <= max;

function CreateKeyDialog({
  client,
  open,
  onClose,
  onIssued,
}: {
  client: ApiClient;
  open: boolean;
  onClose: () => void;
  onIssued: (s: Shown) => void;
}) {
  const create = useCreateKey(client.api_client_id);
  const [scopes, setScopes] = useState<Scope[]>(client.scopes);
  const [days, setDays] = useState(90);
  const valid = scopes.length > 0 && inRange(days, 1, 365);
  function close() {
    create.reset();
    setScopes(client.scopes);
    setDays(90);
    onClose();
  }
  return (
    <Modal
      open={open}
      onClose={close}
      title={`Create a key for ${client.display_name}`}
      description={KEYS_RECOMMENDATION}
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!valid || create.isPending}
            onClick={() =>
              create.mutate(
                { scopes, expires_in_days: days },
                {
                  onSuccess: (r) => {
                    const shown = { key: r.key, keyId: r.api_key.key_id, previous: null };
                    close();
                    onIssued(shown);
                  },
                },
              )
            }
          >
            {create.isPending ? "Creating…" : "Create key"}
          </button>
        </>
      }
    >
      <div className="stack">
        <fieldset className="field">
          <legend className="field-label">Scopes (within the client&apos;s)</legend>
          {client.scopes.map((s) => (
            <label key={s} className="row">
              <input
                type="checkbox"
                checked={scopes.includes(s)}
                onChange={(e) =>
                  setScopes(e.target.checked ? [...scopes, s] : scopes.filter((x) => x !== s))
                }
              />
              <span className="mono">{s}</span>
            </label>
          ))}
        </fieldset>
        <NumberField
          label="Expires after (days)"
          value={days}
          onChange={setDays}
          min={1}
          max={365}
          hint="1 to 365 days. A key never lasts longer; rotate it before then."
        />
        {create.isError ? <ErrorCallout error={create.error} title="No key was made" /> : null}
      </div>
    </Modal>
  );
}

function RotateKeyDialog({
  client,
  apiKey,
  onClose,
  onIssued,
}: {
  client: ApiClient;
  apiKey: ApiKey | null;
  onClose: () => void;
  onIssued: (s: Shown) => void;
}) {
  const rotate = useRotateKey(client.api_client_id);
  const [days, setDays] = useState(90);
  const [overlap, setOverlap] = useState(168);
  const valid = inRange(days, 1, 365) && inRange(overlap, 0, 720);
  function close() {
    rotate.reset();
    setDays(90);
    setOverlap(168);
    onClose();
  }
  return (
    <Modal
      open={apiKey !== null}
      onClose={close}
      title={`Rotate key ${apiKey?.key_id ?? ""}`}
      description="A new key with the same scopes. The old one keeps working for the overlap, then stops."
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!valid || rotate.isPending}
            onClick={() =>
              apiKey &&
              rotate.mutate(
                { keyId: apiKey.key_id, body: { expires_in_days: days, overlap_hours: overlap } },
                {
                  onSuccess: (r) => {
                    const shown = {
                      key: r.key,
                      keyId: r.api_key.key_id,
                      previous: r.previous ?? null,
                    };
                    close();
                    onIssued(shown);
                  },
                },
              )
            }
          >
            {rotate.isPending ? "Rotating…" : "Rotate key"}
          </button>
        </>
      }
    >
      <div className="stack">
        <NumberField
          label="New key expires after (days)"
          value={days}
          onChange={setDays}
          min={1}
          max={365}
          hint="1 to 365 days."
        />
        <NumberField
          label="Old key keeps working for (hours)"
          value={overlap}
          onChange={setOverlap}
          min={0}
          max={720}
          hint="0 to 720 hours; 168 is a week. 0 stops it now."
        />
        {rotate.isError ? (
          <ErrorCallout error={rotate.error} title="The key wasn't rotated" />
        ) : null}
      </div>
    </Modal>
  );
}

function RevokeKeyDialog({
  client,
  apiKey,
  onClose,
}: {
  client: ApiClient;
  apiKey: ApiKey | null;
  onClose: () => void;
}) {
  const revoke = useRevokeKey(client.api_client_id);
  const toast = useToast();
  function close() {
    revoke.reset();
    onClose();
  }
  return (
    <Modal
      open={apiKey !== null}
      onClose={close}
      title={`Revoke key ${apiKey?.key_id ?? ""}?`}
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
              apiKey &&
              revoke.mutate(apiKey.key_id, {
                onSuccess: () => {
                  toast(`Key ${apiKey.key_id} was revoked.`);
                  close();
                },
              })
            }
          >
            {revoke.isPending ? "Revoking…" : "Revoke key"}
          </button>
        </>
      }
    >
      <div className="stack">
        <p>Its next call is refused. A revoked key can&apos;t be used again.</p>
        {revoke.isError ? <ErrorCallout error={revoke.error} title="It wasn't revoked" /> : null}
      </div>
    </Modal>
  );
}

export function KeysSection({ client }: { client: ApiClient }) {
  const [creating, setCreating] = useState(false);
  const [rotating, setRotating] = useState<ApiKey | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [shown, setShown] = useState<Shown | null>(null);
  const keys = client.keys ?? [];
  const active = client.state === "enabled";
  return (
    <div className="card card-pad stack" aria-labelledby="keys-h">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 id="keys-h">Keys</h3>
        {client.state === "revoked" ? null : (
          <button
            type="button"
            className="btn"
            disabled={!active || client.scopes.length === 0}
            onClick={() => setCreating(true)}
          >
            Create key
          </button>
        )}
      </div>
      <Callout>{KEYS_RECOMMENDATION}</Callout>
      {keys.length === 0 ? (
        <p className="muted">No OmniFile API keys.</p>
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label="API keys">
            <thead>
              <tr>
                <th scope="col">Key</th>
                <th scope="col">Scopes</th>
                <th scope="col">State</th>
                <th scope="col">Expires</th>
                <th scope="col">Last used</th>
                <th scope="col">Rotated from</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.key_id}>
                  <th scope="row" className="mono">
                    {k.key_id}
                  </th>
                  <td className="mono">{k.scopes.join(", ")}</td>
                  <td>
                    <StatusBadge status={k.state === "active" ? "enabled" : k.state} />
                  </td>
                  <td>{formatDateTime(k.expires_at)}</td>
                  <td>
                    {k.last_used_at ? formatDateTime(k.last_used_at) : "never"}
                    {k.last_used_addr ? <div className="muted mono">{k.last_used_addr}</div> : null}
                  </td>
                  <td className="mono">{k.rotated_from ?? "—"}</td>
                  <td>
                    {k.state === "active" ? (
                      <span className="row">
                        <button
                          type="button"
                          className="btn btn-link"
                          aria-label={`Rotate key ${k.key_id}`}
                          onClick={() => setRotating(k)}
                        >
                          Rotate
                        </button>
                        <button
                          type="button"
                          className="btn btn-link"
                          aria-label={`Revoke key ${k.key_id}`}
                          onClick={() => setRevoking(k)}
                        >
                          Revoke…
                        </button>
                      </span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <CreateKeyDialog
        client={client}
        open={creating}
        onClose={() => setCreating(false)}
        onIssued={setShown}
      />
      <RotateKeyDialog
        client={client}
        apiKey={rotating}
        onClose={() => setRotating(null)}
        onIssued={setShown}
      />
      <RevokeKeyDialog client={client} apiKey={revoking} onClose={() => setRevoking(null)} />
      <KeyShownDialog shown={shown} onClose={() => setShown(null)} />
    </div>
  );
}
