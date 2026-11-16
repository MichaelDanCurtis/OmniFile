import { useId, useState } from "react";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { formatDateTime } from "../../lib/format";
import { CopyButton, Fact, StatusBadge } from "./common";
import {
  fetchResultOf,
  isSettled,
  useAwsConfirm,
  useAwsConnect,
  useAwsFetch,
  useAwsOutputs,
  useCloudTask,
  type AwsConnect,
  type AwsConnection,
  type AwsFetchResult,
} from "./cloudQueries";

/**
 * Connect AWS (docs/design/cloud-connect.md 2): launch OmniFile's CloudFormation
 * stack in the admin's own AWS sign-in, then fetch (or paste) what it made and
 * confirm the account's settings entry. No AWS key is asked for or stored.
 */

const REGION = /^[a-z]{2}(-[a-z]+)+-[0-9]$/;
const DEPLOYMENT = /^[a-z0-9][a-z0-9-]{1,19}$/;
const ACCOUNT = /^[0-9]{12}$/;
const BUCKET = /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/;

/** What a fetch or pasted outputs found, ready to confirm. */
interface Found {
  from: "fetch" | "paste";
  account_id: string | null;
  region: string | null;
  deployment_id: string | null;
  outputs: Record<string, string>;
  entry: Record<string, unknown> | null;
  buckets: string[];
  problems: string[];
  fetched: AwsFetchResult | null;
}

function Launch({ c }: { c: AwsConnect }) {
  const params = Object.entries(c.parameters);
  const table = (
    <div className="table-wrap">
      <table className="table" aria-label="Stack parameters">
        <thead>
          <tr>
            <th scope="col">Parameter</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {params.map(([k, v]) => (
            <tr key={k}>
              <th scope="row" className="mono">
                {k}
              </th>
              <td className="mono">
                {v || (
                  <span className="muted">you fill this in (for example the bucket names)</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
  return (
    <div className="stack">
      {c.quick_create_url ? (
        <>
          <div className="row">
            <a
              className="btn btn-primary"
              href={c.quick_create_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              Launch in AWS
            </a>
            <span className="muted">
              Opens CloudFormation with stack <span className="mono">{c.stack_name}</span> and these
              values filled in.
            </span>
          </div>
          <details className="more">
            <summary>The values it fills in</summary>
            <div className="more-body">{table}</div>
          </details>
        </>
      ) : (
        <>
          <Callout title="No one-click link here">
            A one-click link needs the template published in S3 (OMNIFILE_AWS_CONNECT_TEMPLATE_URL).
            Download the template, open CloudFormation&apos;s Create stack page, choose Upload a
            template file, and type the values below.
          </Callout>
          <div className="row">
            <a className="btn" href={c.template_path} download="omnifile-connect.yaml">
              Download template
            </a>
            <a
              className="btn"
              href={c.console_create_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              Open CloudFormation
            </a>
          </div>
          {table}
        </>
      )}
    </div>
  );
}

function FetchForm({
  c,
  taskId,
  onStarted,
}: {
  c: AwsConnect;
  taskId: string | null;
  onStarted: (taskId: string, deploymentId: string) => void;
}) {
  const fetch = useAwsFetch();
  const task = useCloudTask(taskId);
  const [account, setAccount] = useState("");
  const [region, setRegion] = useState(c.region);
  const [dep, setDep] = useState(c.deployment_id);
  const [eps, setEps] = useState({ s3: "", sts: "", sqs: "" });
  const ids = {
    account: useId(),
    region: useId(),
    dep: useId(),
    s3: useId(),
    sts: useId(),
    sqs: useId(),
    epHint: useId(),
  };
  const valid = ACCOUNT.test(account) && REGION.test(region) && DEPLOYMENT.test(dep);
  const t = task.data;

  return (
    <form
      className="stack"
      onSubmit={(e) => {
        e.preventDefault();
        if (!valid) return;
        const endpoints = Object.fromEntries(
          Object.entries(eps).filter(([, v]) => v.trim() !== ""),
        );
        fetch.mutate(
          {
            account_id: account,
            region,
            deployment_id: dep,
            ...(c.private_network && Object.keys(endpoints).length ? { endpoints } : {}),
          },
          { onSuccess: (started) => onStarted(started.task_id, dep) },
        );
      }}
    >
      <div className="grid-2">
        <div className="field">
          <label className="field-label" htmlFor={ids.account}>
            AWS account id
          </label>
          <input
            id={ids.account}
            type="text"
            inputMode="numeric"
            className="mono"
            autoComplete="off"
            placeholder="123456789012"
            value={account}
            aria-invalid={account !== "" && !ACCOUNT.test(account)}
            onChange={(e) => setAccount(e.target.value.trim())}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.region}>
            Stack region
          </label>
          <input
            id={ids.region}
            type="text"
            className="mono"
            autoComplete="off"
            value={region}
            aria-invalid={!REGION.test(region)}
            onChange={(e) => setRegion(e.target.value.trim())}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.dep}>
            Stack deployment id
          </label>
          <input
            id={ids.dep}
            type="text"
            className="mono"
            autoComplete="off"
            value={dep}
            aria-invalid={!DEPLOYMENT.test(dep)}
            onChange={(e) => setDep(e.target.value.trim())}
          />
        </div>
      </div>
      {c.private_network ? (
        <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="field-label">Interface endpoints (private networking is on)</legend>
          <p id={ids.epHint} className="field-hint">
            From the stack&apos;s outputs (S3EndpointDns, StsEndpointDns, SqsEndpointDns), as
            https://&lt;dns&gt;. The fetch needs at least S3 and STS.
          </p>
          <div className="grid-2">
            {(
              [
                ["s3", "S3 endpoint", ids.s3],
                ["sts", "STS endpoint", ids.sts],
                ["sqs", "SQS endpoint", ids.sqs],
              ] as const
            ).map(([k, label, id]) => (
              <div key={k} className="field">
                <label className="field-label" htmlFor={id}>
                  {label}
                </label>
                <input
                  id={id}
                  type="url"
                  className="mono"
                  autoComplete="off"
                  maxLength={300}
                  placeholder={`https://vpce-0abc.${k}.${region || "us-east-1"}.vpce.amazonaws.com`}
                  aria-describedby={ids.epHint}
                  value={eps[k]}
                  onChange={(e) => setEps((prev) => ({ ...prev, [k]: e.target.value.trim() }))}
                />
              </div>
            ))}
          </div>
        </fieldset>
      ) : null}
      <div>
        <button
          type="submit"
          className="btn btn-primary"
          disabled={!valid || fetch.isPending || (!!t && !isSettled(t.state))}
        >
          {fetch.isPending ? "Starting…" : "Fetch"}
        </button>
      </div>
      {fetch.isError ? <ErrorCallout error={fetch.error} title="The fetch didn't start" /> : null}
      {task.isError ? <ErrorCallout error={task.error} title="The fetch couldn't be read" /> : null}
      {taskId && (!t || !isSettled(t.state)) ? (
        <Callout role="status" title="Fetching…">
          The worker assumes the new role with its web identity and reads the buckets and the event
          queue. It reads only, and keeps no credentials.
        </Callout>
      ) : null}
      {t && t.state === "failed" ? (
        <Callout tone="danger" role="status" title="The fetch failed">
          {t.detail ?? "The worker couldn't read the account."}
        </Callout>
      ) : null}
    </form>
  );
}

function PasteForm({ onFound }: { onFound: (found: Found) => void }) {
  const parse = useAwsOutputs();
  const [text, setText] = useState("");
  const id = useId();
  const hint = useId();
  return (
    <form
      className="stack"
      onSubmit={(e) => {
        e.preventDefault();
        parse.mutate(text, {
          onSuccess: (r) =>
            onFound({
              from: "paste",
              account_id: r.account_id,
              region: r.region,
              deployment_id: r.deployment_id,
              outputs: r.outputs,
              entry: r.entry,
              buckets: r.buckets,
              problems: r.problems,
              fetched: null,
            }),
        });
      }}
    >
      <div className="field">
        <label className="field-label" htmlFor={id}>
          The stack&apos;s outputs
        </label>
        <textarea
          id={id}
          className="mono"
          rows={6}
          maxLength={20000}
          spellCheck={false}
          aria-describedby={hint}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <span id={hint} className="field-hint">
          Copy the Outputs tab from the CloudFormation console, the JSON of aws cloudformation
          describe-stacks, or Key=Value lines. Nothing is called or stored.
        </span>
      </div>
      <div>
        <button type="submit" className="btn" disabled={!text.trim() || parse.isPending}>
          {parse.isPending ? "Reading…" : "Read the outputs"}
        </button>
      </div>
      {parse.isError ? (
        <ErrorCallout error={parse.error} title="The outputs couldn't be read" />
      ) : null}
    </form>
  );
}

function ConnectionView({ conn }: { conn: AwsConnection }) {
  return (
    <div className="stack">
      <dl className="kv">
        <Fact label="Account">
          <span className="mono">{conn.account_id}</span>
        </Fact>
        <Fact label="Region">{conn.region}</Fact>
        <Fact label="Buckets">{conn.buckets.join(", ") || "none"}</Fact>
        <Fact label="Configured">
          {conn.configured ? (
            <span className="badge badge-ok">In the deployment</span>
          ) : (
            <span className="badge badge-warn">Not deployed yet</span>
          )}
        </Fact>
        <Fact label="Confirmed">
          {formatDateTime(conn.confirmed_at)}
          {conn.confirmed_by ? ` by ${conn.confirmed_by}` : ""}
        </Fact>
      </dl>
      <p>
        Set <span className="mono">{conn.env_name}</span> (the deployment&apos;s{" "}
        <span className="mono">{conn.parameter_name}</span> parameter) to this value. The account
        isn&apos;t used until the deployment has it: requests and identities stay operator settings,
        never something the portal can change.
      </p>
      <pre className="mono" aria-label={`${conn.env_name} value`}>
        {conn.settings_json}
      </pre>
      <div>
        <CopyButton text={conn.settings_json} label={`Copy ${conn.env_name}`} />
      </div>
      <ol>
        {conn.steps.map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ol>
    </div>
  );
}

function ConfirmFound({ found, deploymentId }: { found: Found; deploymentId: string }) {
  const confirm = useAwsConfirm();
  const base = useId();
  const inStack = new Set(found.fetched?.buckets.filter((b) => b.in_stack).map((b) => b.name));
  const [chosen, setChosen] = useState<string[]>(() =>
    found.fetched ? found.buckets.filter((b) => inStack.has(b)) : found.buckets,
  );
  const account = found.account_id ?? "";
  const region = found.region ?? "";
  const dep = found.deployment_id ?? deploymentId;
  const ok =
    ACCOUNT.test(account) &&
    REGION.test(region) &&
    DEPLOYMENT.test(dep) &&
    !!found.entry &&
    found.problems.length === 0;
  const f = found.fetched;

  return (
    <div className="stack">
      <h4>{found.from === "fetch" ? "What the fetch found" : "What the outputs say"}</h4>
      {found.problems.length ? (
        <Callout tone="warn" title="Fix these before confirming">
          <ul>
            {found.problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </Callout>
      ) : null}
      <dl className="kv">
        <Fact label="Account">
          {account ? <span className="mono">{account}</span> : "not found"}
        </Fact>
        <Fact label="Region">{region || "not found"}</Fact>
        {f ? (
          <>
            <Fact label="Role">
              <span className="mono">{f.role_arn}</span>
            </Fact>
            <Fact label="Event queue">
              <span className="mono">{f.queue.url}</span>{" "}
              {f.queue.ok ? (
                <span className="badge badge-ok">Readable</span>
              ) : (
                <span className="badge badge-danger">Not readable</span>
              )}
            </Fact>
          </>
        ) : null}
      </dl>
      {f ? (
        <div className="table-wrap">
          <table className="table" aria-label="Buckets found">
            <thead>
              <tr>
                <th scope="col">Use</th>
                <th scope="col">Bucket</th>
                <th scope="col">Region</th>
                <th scope="col">Versioning</th>
                <th scope="col">EventBridge</th>
                <th scope="col">In the stack</th>
              </tr>
            </thead>
            <tbody>
              {f.buckets.map((b) => {
                const hint = `${base}-${b.name}`;
                return (
                  <tr key={b.name}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Use ${b.name}`}
                        aria-describedby={hint}
                        checked={chosen.includes(b.name)}
                        onChange={(e) =>
                          setChosen((prev) =>
                            e.target.checked ? [...prev, b.name] : prev.filter((x) => x !== b.name),
                          )
                        }
                      />
                    </td>
                    <th scope="row" className="mono">
                      {b.name}
                    </th>
                    <td>{b.region ?? "—"}</td>
                    <td>{b.versioning ?? "—"}</td>
                    <td id={hint}>
                      {b.eventbridge === true ? (
                        <span className="badge badge-ok">On</span>
                      ) : b.eventbridge === false ? (
                        <span className="badge badge-warn">
                          Off: turn it on in the bucket&apos;s Properties
                        </span>
                      ) : (
                        <span className="badge badge-outline">Couldn&apos;t be read</span>
                      )}
                    </td>
                    <td>{b.in_stack ? "Yes" : "No: the role can't read it"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : found.buckets.length ? (
        <fieldset className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="field-label">Source buckets</legend>
          {found.buckets.map((b) => (
            <label key={b} className="check-row">
              <input
                type="checkbox"
                checked={chosen.includes(b)}
                onChange={(e) =>
                  setChosen((prev) =>
                    e.target.checked ? [...prev, b] : prev.filter((x) => x !== b),
                  )
                }
              />
              <span className="mono">{b}</span>
            </label>
          ))}
        </fieldset>
      ) : null}
      {found.entry ? (
        <details className="more">
          <summary>The account&apos;s settings entry</summary>
          <pre className="mono more-body" aria-label="Settings entry">
            {JSON.stringify(found.entry, null, 2)}
          </pre>
        </details>
      ) : null}
      <div>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!ok || confirm.isPending}
          onClick={() =>
            confirm.mutate({
              account_id: account,
              region,
              deployment_id: dep,
              outputs: found.outputs,
              buckets: chosen.filter((b) => BUCKET.test(b)),
            })
          }
        >
          {confirm.isPending ? "Confirming…" : "Confirm"}
        </button>
      </div>
      {confirm.isError ? (
        <ErrorCallout error={confirm.error} title="The account wasn't confirmed" />
      ) : null}
      {confirm.data ? (
        <Callout tone="ok" role="status" title="Recorded">
          The account&apos;s entry is recorded and audited. One more step: put it in the deployment.
        </Callout>
      ) : null}
      {confirm.data ? <ConnectionView conn={confirm.data} /> : null}
    </div>
  );
}

export function AwsConnectSection() {
  const [applied, setApplied] = useState({ region: "", dep: "" });
  const [typed, setTyped] = useState<{ region: string; dep: string } | null>(null);
  const connect = useAwsConnect(applied.region, applied.dep);
  const [fetchRun, setFetchRun] = useState<{ taskId: string; dep: string } | null>(null);
  const [pasted, setPasted] = useState<Found | null>(null);
  const [use, setUse] = useState<"fetch" | "paste">("fetch");
  const task = useCloudTask(fetchRun?.taskId ?? null);
  const ids = { region: useId(), dep: useId(), hint: useId() };
  const result = fetchResultOf(task.data);
  const fetched: Found | null =
    result && fetchRun
      ? {
          from: "fetch",
          account_id: result.account_id,
          region: result.region,
          deployment_id: fetchRun.dep,
          outputs: result.outputs,
          entry: result.entry,
          buckets: result.buckets.map((b) => b.name),
          problems: result.problems,
          fetched: result,
        }
      : null;
  const found = use === "paste" ? pasted : fetched;

  if (connect.isPending) return <Loading />;
  if (connect.isError) return <ErrorCallout error={connect.error} />;
  const c = connect.data;
  const shown = typed ?? { region: c.region, dep: c.deployment_id };
  const validTyped = REGION.test(shown.region) && DEPLOYMENT.test(shown.dep);

  return (
    <section className="stack" aria-labelledby="aws-connect-h">
      <h3 id="aws-connect-h">Connect AWS</h3>
      <p>
        One CloudFormation stack in your AWS account makes everything OmniFile needs: a role that
        trusts only this deployment&apos;s identities, the event queue, and the Batch Operations
        role. You create it in your own AWS sign-in; OmniFile never sees it. The role may read and
        restore your source buckets and is denied every write to them.
      </p>
      <form
        className="card card-pad stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (validTyped) setApplied({ region: shown.region, dep: shown.dep });
        }}
      >
        <div className="grid-2">
          <div className="field">
            <label className="field-label" htmlFor={ids.region}>
              AWS region
            </label>
            <input
              id={ids.region}
              type="text"
              className="mono"
              autoComplete="off"
              value={shown.region}
              aria-invalid={!REGION.test(shown.region)}
              onChange={(e) => setTyped({ ...shown, region: e.target.value.trim() })}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor={ids.dep}>
              Deployment id
            </label>
            <input
              id={ids.dep}
              type="text"
              className="mono"
              autoComplete="off"
              aria-describedby={ids.hint}
              value={shown.dep}
              aria-invalid={!DEPLOYMENT.test(shown.dep)}
              onChange={(e) => setTyped({ ...shown, dep: e.target.value.trim() })}
            />
            <span id={ids.hint} className="field-hint">
              A short name in every resource the stack makes (lowercase, digits and -).
            </span>
          </div>
        </div>
        <div>
          <button
            type="submit"
            className="btn"
            disabled={!validTyped || (shown.region === c.region && shown.dep === c.deployment_id)}
          >
            Use these values
          </button>
        </div>
      </form>

      <div className="card card-pad stack">
        <h4>1. Create the stack</h4>
        {!c.ready ? (
          <Callout tone="warn" title="The template can't be filled in yet">
            <ul>
              {c.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </Callout>
        ) : (
          <Launch c={c} />
        )}
        <ol aria-label="Connect AWS steps">
          {c.steps.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ol>
      </div>

      <div className="card card-pad stack">
        <h4>2. Fetch what the stack made</h4>
        <p className="muted">
          Once the stack shows CREATE_COMPLETE. The role&apos;s name is{" "}
          <span className="mono">{c.role_arn_pattern}</span>.
        </p>
        <FetchForm
          key={`${c.region}|${c.deployment_id}`}
          c={c}
          taskId={fetchRun?.taskId ?? null}
          onStarted={(taskId, dep) => {
            setFetchRun({ taskId, dep });
            setUse("fetch");
          }}
        />
        <details className="more">
          <summary>Or paste the stack&apos;s outputs</summary>
          <div className="more-body">
            <PasteForm
              onFound={(f) => {
                setPasted(f);
                setUse("paste");
              }}
            />
          </div>
        </details>
      </div>

      {found ? (
        <div className="card card-pad stack">
          <h4>3. Confirm</h4>
          <ConfirmFound
            key={`${found.from}|${fetchRun?.taskId ?? ""}|${JSON.stringify(found.outputs)}`}
            found={found}
            deploymentId={c.deployment_id}
          />
        </div>
      ) : null}

      {c.connections.length ? (
        <div className="card card-pad stack">
          <h4>Accounts connected</h4>
          <div className="table-wrap">
            <table className="table" aria-label="Connected AWS accounts">
              <thead>
                <tr>
                  <th scope="col">Account</th>
                  <th scope="col">Region</th>
                  <th scope="col">Buckets</th>
                  <th scope="col">Deployed</th>
                  <th scope="col">Confirmed</th>
                </tr>
              </thead>
              <tbody>
                {c.connections.map((x) => (
                  <tr key={x.account_id}>
                    <th scope="row" className="mono">
                      {x.account_id}
                    </th>
                    <td>{x.region}</td>
                    <td className="mono">{x.buckets.join(", ") || "—"}</td>
                    <td>
                      <StatusBadge status={x.configured ? "done" : "todo"} />
                    </td>
                    <td>
                      {formatDateTime(x.confirmed_at)}
                      {x.confirmed_by ? ` by ${x.confirmed_by}` : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </section>
  );
}
