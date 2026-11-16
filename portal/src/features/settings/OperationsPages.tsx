import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";
import { Link } from "react-router";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { formatDateTime } from "../../lib/format";
import {
  isSettled,
  useCloudTask,
  useDeployMover,
  useStorageMoverLive,
  type StorageMoverSettings,
} from "./cloudQueries";
import { CopyButton, Fact, Linkified, StatusBadge } from "./common";
import { sk, useAlerts } from "./queries";
import { PriceEditor, PriceVisibility } from "./Prices";
import "./prices.css";

const MOVER_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{2,62}$/;
const LOCATION = /^[a-z0-9]{3,30}$/;

/** Deploy Storage Mover (cloud-connect.md 3): the worker creates it with its granted role. */
function DeployMover({ m }: { m: StorageMoverSettings }) {
  const deploy = useDeployMover();
  const [taskId, setTaskId] = useState<string | null>(null);
  const task = useCloudTask(taskId);
  const [name, setName] = useState("omnifile-mover");
  const [project, setProject] = useState("migrations");
  const [location, setLocation] = useState("westeurope");
  const ids = { name: useId(), project: useId(), location: useId() };
  const can = m.can_deploy;
  const valid = MOVER_NAME.test(name) && MOVER_NAME.test(project) && LOCATION.test(location);
  const t = task.data;
  const running = !!taskId && (!t || !isSettled(t.state));
  const client = useQueryClient();
  const settled = !!t && isSettled(t.state);
  useEffect(() => {
    if (settled) void client.invalidateQueries({ queryKey: sk.mover });
  }, [settled, client]);

  if (!can.ok) {
    return (
      <Callout tone="warn" title="Storage Mover can't be deployed from here yet">
        <p>{can.reason ?? "Azure isn't connected."}</p>
        <p>
          <Link to="/settings/azure">Connect Azure</Link> and choose a resource group for Storage
          Mover, so the worker gets its Mover Deployer grant there.
        </p>
      </Callout>
    );
  }
  return (
    <form
      className="card card-pad stack"
      aria-labelledby={`${ids.name}-h`}
      onSubmit={(e) => {
        e.preventDefault();
        if (!valid) return;
        deploy.mutate(
          { name, project, location },
          { onSuccess: (started) => setTaskId(started.task_id) },
        );
      }}
    >
      <h3 id={`${ids.name}-h`}>Deploy Storage Mover</h3>
      <p className="muted">
        The worker creates the Storage Mover and its project in resource group{" "}
        <span className="mono">{can.resource_group}</span> (subscription{" "}
        <span className="mono">{can.subscription_id}</span>), with its own managed identity and the
        Mover Deployer role from Connect Azure.
      </p>
      <div className="grid-2">
        <div className="field">
          <label className="field-label" htmlFor={ids.name}>
            Storage Mover name
          </label>
          <input
            id={ids.name}
            type="text"
            autoComplete="off"
            value={name}
            aria-invalid={!MOVER_NAME.test(name)}
            onChange={(e) => setName(e.target.value.trim())}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.project}>
            Project
          </label>
          <input
            id={ids.project}
            type="text"
            autoComplete="off"
            value={project}
            aria-invalid={!MOVER_NAME.test(project)}
            onChange={(e) => setProject(e.target.value.trim())}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.location}>
            Location
          </label>
          <input
            id={ids.location}
            type="text"
            className="mono"
            autoComplete="off"
            value={location}
            aria-invalid={!LOCATION.test(location)}
            onChange={(e) => setLocation(e.target.value.trim())}
          />
        </div>
      </div>
      <div>
        <button
          type="submit"
          className="btn btn-primary"
          disabled={!valid || deploy.isPending || running}
        >
          {deploy.isPending || running ? "Deploying…" : m.deployment ? "Deploy again" : "Deploy"}
        </button>
      </div>
      {deploy.isError ? (
        <ErrorCallout error={deploy.error} title="The deployment didn't start" />
      ) : null}
      {running ? (
        <Callout role="status" title="Deploying…">
          The worker is creating the Storage Mover and its project, then reads them back.
        </Callout>
      ) : null}
    </form>
  );
}

function Deployment({ m }: { m: StorageMoverSettings }) {
  const d = m.deployment;
  if (!d) return null;
  const online = m.agents.some((a) => a.status === "Online");
  return (
    <div className="card card-pad stack" aria-labelledby="mover-dep-h">
      <h3 id="mover-dep-h">The deployment</h3>
      <div className="row" style={{ gap: 8 }}>
        <StatusBadge status={d.state} />
        <span>
          {d.name}, project {d.project}, in {d.location}
        </span>
      </div>
      {d.detail ? <p>{d.detail}</p> : null}
      <dl className="kv">
        <Fact label="Resource group">{d.resource_group}</Fact>
        <Fact label="Subscription">
          <span className="mono">{d.subscription_id}</span>
        </Fact>
        <Fact label="Started">{formatDateTime(d.created_at)}</Fact>
        <Fact label="Finished">{d.finished_at ? formatDateTime(d.finished_at) : "not yet"}</Fact>
      </dl>
      {d.state === "done" ? (
        <>
          <h4>The on-premises agent</h4>
          <p className="muted">
            A Storage Mover agent is a VM you run near the data. It copies; the migration engine
            defines and runs its jobs.
          </p>
          <ol aria-label="Agent steps">
            {m.agent_steps.map((step) => (
              <li key={step}>
                <Linkified text={step} />
              </li>
            ))}
          </ol>
          <h4>Values to give the agent when it registers</h4>
          <dl className="kv" aria-label="Registration values">
            <Fact label="Tenant">
              <span className="mono">{d.registration.tenant_id}</span>
            </Fact>
            <Fact label="Subscription">
              <span className="mono">{d.registration.subscription_id}</span>
            </Fact>
            <Fact label="Resource group">{d.registration.resource_group}</Fact>
            <Fact label="Storage Mover">{d.registration.storage_mover}</Fact>
          </dl>
          <p role="status">
            {online
              ? "An agent is Online."
              : "Waiting for an agent to register. This page checks every 15 seconds."}
          </p>
          <h4>The setting to deploy</h4>
          <p>
            Set <span className="mono">OMNIFILE_STORAGE_MOVER</span> (the deployment&apos;s{" "}
            <span className="mono">storageMoverConfig</span>) to this value, once the source and
            target endpoints are filled in (setup guide, step 10).
          </p>
          <pre className="mono" aria-label="OMNIFILE_STORAGE_MOVER value">
            {d.config_value}
          </pre>
          <div>
            <CopyButton text={d.config_value} label="Copy the value" />
          </div>
        </>
      ) : null}
    </div>
  );
}

export function StorageMoverPage() {
  usePageTitle("Settings: Storage Mover");
  const mover = useStorageMoverLive();
  if (mover.isPending) return <Loading />;
  if (mover.isError) return <ErrorCallout error={mover.error} />;
  const m = mover.data;
  const endpoints = [
    ...Object.entries(m.endpoints.source).map(([k, v]) => ["Source", k, v] as const),
    ...Object.entries(m.endpoints.target).map(([k, v]) => ["Target", k, v] as const),
  ];
  return (
    <section className="stack" aria-labelledby="mover-h">
      <h2 id="mover-h">Storage Mover</h2>
      <p className="lede">
        Azure Storage Mover copies migrated files from S3; the migration engine defines and runs its
        jobs. This page reads it with the service&apos;s identity, and can deploy it for you.
      </p>
      <DeployMover m={m} />
      <Deployment m={m} />
      {!m.configured ? (
        <Callout tone="warn" title="Not configured">
          The service has no Storage Mover settings, so migrations can&apos;t start copies.
        </Callout>
      ) : null}
      {m.error ? (
        <Callout tone="danger" title="Storage Mover couldn't be read">
          {m.error}
        </Callout>
      ) : null}
      {m.storage_mover ? (
        <dl className="kv card card-pad">
          <Fact label="Storage Mover">{m.storage_mover.name}</Fact>
          <Fact label="Resource group">{m.storage_mover.resource_group}</Fact>
          <Fact label="Subscription">
            <span className="mono">{m.storage_mover.subscription_id}</span>
          </Fact>
          <Fact label="State">{m.storage_mover.provisioning_state ?? "—"}</Fact>
        </dl>
      ) : null}
      <div className="table-wrap card">
        <table className="table" aria-label="Agents">
          <thead>
            <tr>
              <th scope="col">Agent</th>
              <th scope="col">Status</th>
              <th scope="col">Last update</th>
              <th scope="col">Version</th>
            </tr>
          </thead>
          <tbody>
            {m.agents.length === 0 ? (
              <tr>
                <td colSpan={4}>No agents. Cloud-to-cloud copies need none.</td>
              </tr>
            ) : (
              m.agents.map((a) => (
                <tr key={a.name}>
                  <th scope="row">{a.name}</th>
                  <td>{a.status ?? "—"}</td>
                  <td>{a.last_status_update ?? "—"}</td>
                  <td>{a.version ?? "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div className="table-wrap card">
        <table className="table" aria-label="Projects">
          <thead>
            <tr>
              <th scope="col">Project</th>
              <th scope="col">State</th>
              <th scope="col">Description</th>
            </tr>
          </thead>
          <tbody>
            {m.projects.length === 0 ? (
              <tr>
                <td colSpan={3}>No projects.</td>
              </tr>
            ) : (
              m.projects.map((p) => (
                <tr key={p.name}>
                  <th scope="row">
                    {p.name} {p.in_use ? <span className="badge badge-info">In use</span> : null}
                  </th>
                  <td>{p.provisioning_state ?? "—"}</td>
                  <td>{p.description ?? "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {endpoints.length ? (
        <div className="table-wrap card">
          <table className="table" aria-label="Endpoints">
            <thead>
              <tr>
                <th scope="col">Side</th>
                <th scope="col">Bucket or container</th>
                <th scope="col">Endpoint</th>
              </tr>
            </thead>
            <tbody>
              {endpoints.map(([side, k, v]) => (
                <tr key={`${side}-${k}`}>
                  <td>{side}</td>
                  <th scope="row" className="mono">
                    {k}
                  </th>
                  <td className="mono">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

export function PricesAlertsPage() {
  usePageTitle("Settings: prices and alerts");
  const alerts = useAlerts();
  return (
    <section className="stack" aria-labelledby="prices-h">
      <h2 id="prices-h">Prices and alerts</h2>
      <p className="lede">
        Who sees costs, and the rates every cost in the portal is worked out from: your negotiated
        rates, or the provider&apos;s list prices.
      </p>
      <PriceVisibility />
      <PriceEditor kind="azure_blob" label="Azure Blob" />
      <PriceEditor kind="s3" label="Amazon S3" />
      <h3>Alerts</h3>
      {alerts.isPending ? (
        <Loading />
      ) : alerts.isError ? (
        <ErrorCallout error={alerts.error} />
      ) : (
        <>
          <p>{alerts.data.where}</p>
          <div className="table-wrap card">
            <table className="table" aria-label="Alerts">
              <thead>
                <tr>
                  <th scope="col">Alert</th>
                  <th scope="col">Fires when</th>
                  <th scope="col">Now</th>
                  <th scope="col">Detail</th>
                </tr>
              </thead>
              <tbody>
                {alerts.data.alerts.map((a) => (
                  <tr key={a.name}>
                    <th scope="row">{a.name}</th>
                    <td>{a.condition}</td>
                    <td>
                      <StatusBadge status={a.status} />
                    </td>
                    <td>{a.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
