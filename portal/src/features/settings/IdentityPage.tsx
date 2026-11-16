import { Link } from "react-router";
import { useEdges } from "../../api/queries";
import { usePageTitle } from "../../app/usePageTitle";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { formatDateTime } from "../../lib/format";
import { useAzureSettings } from "./cloudQueries";
import { CheckList, Fact, StatusBadge, StepList, type StepItem } from "./common";
import { useIdentitySettings, useWhoAmI } from "./queries";

type IdentitySettings = NonNullable<ReturnType<typeof useIdentitySettings>["data"]>;

/**
 * What Entra ID and Active Directory each do for OmniFile, and a done / to-do
 * list built from what this page already reads (docs/design/cloud-connect.md 1.6).
 */
function SetupSteps({ s }: { s: IdentitySettings }) {
  const who = useWhoAmI();
  const azure = useAzureSettings();
  const edges = useEdges();
  const personRoles = s.roles.filter((r) => r.member_types.includes("user"));
  const roleless = personRoles.filter((r) => r.groups.length === 0).map((r) => r.role);
  const w = who.data;
  const tokenOk = !!w?.sid && w.group_count > 0 && w.checks.every((c) => c.status === "ok");
  const azureSteps = azure.data?.steps ?? [];
  const azureDone = azureSteps.filter((x) => x.status === "done").length;
  const active = (edges.data?.edges ?? []).filter((e) => e.state === "active");

  const items: StepItem[] = [
    {
      code: "app",
      title: "The tenant and the portal's app registration",
      status: s.tenant_id && s.client_id ? "done" : "todo",
      detail:
        s.tenant_id && s.client_id
          ? "The service knows its Entra tenant and the portal app people sign in to."
          : "Run deploy/entra/register.sh, then set the tenant and client id in the deployment.",
    },
    {
      code: "consent",
      title: "Admin consent for the portal app",
      status: s.consent.status === "granted" ? "done" : "todo",
      detail:
        s.consent.status === "granted"
          ? s.consent.detail
          : `${s.consent.detail} A tenant administrator grants it from the link below.`,
    },
    {
      code: "roles",
      title: "App roles given to AD groups",
      status: roleless.length === 0 ? "done" : "todo",
      detail:
        roleless.length === 0
          ? "Every role for people is held by at least one AD group synced to Entra."
          : `No group seen holding ${roleless.join(", ")}. Assign the roles to AD groups in Enterprise applications, Users and groups.`,
    },
    {
      code: "token",
      title: "Your token carries your AD identity",
      status: !w ? "unknown" : tokenOk ? "done" : "todo",
      detail: !w
        ? "Reading your sign-in…"
        : tokenOk
          ? `Your sign-in has your on-premises SID (onprem_sid) and ${w.group_count} group SIDs, so file access checks work for you.`
          : "Your sign-in lacks the onprem_sid claim or group SIDs. Add them to the app's token configuration, and check Entra Connect syncs your account.",
    },
    {
      code: "azure",
      title: "Azure connected",
      status: !azure.data
        ? "unknown"
        : azureSteps.length > 0 && azureDone === azureSteps.length
          ? "done"
          : "todo",
      detail: (
        <>
          {azure.data
            ? `${azureDone} of ${azureSteps.length} steps done. `
            : azure.isError
              ? "Couldn't read the Azure connection. "
              : "Reading the Azure connection… "}
          <Link to="/settings/azure">Settings › Azure</Link>
        </>
      ),
    },
    {
      code: "edge",
      title: "At least one edge joined to AD",
      status: !edges.data ? "unknown" : active.length > 0 ? "done" : "todo",
      detail: (
        <>
          {!edges.data
            ? "Reading the edges… "
            : active.length > 0
              ? `${active.length === 1 ? "1 edge is" : `${active.length} edges are`} enrolled; each joined AD with winbind during setup. `
              : "No edge is enrolled yet. Its setup wizard joins it to AD. "}
          <Link to="/edges">Edges</Link>
        </>
      ),
    },
  ];

  return (
    <div className="stack">
      <div className="grid-2">
        <div className="card card-pad stack">
          <h3>Entra ID: people and the cloud</h3>
          <ul className="stack" style={{ margin: 0, paddingLeft: 20 }}>
            <li>People sign in to the portal with Entra ID.</li>
            <li>
              The app roles (ArchiveAdmin, DataSteward, User) go to AD groups that Entra Connect
              syncs to Entra.
            </li>
            <li>Teams cards reach people through Entra.</li>
            <li>
              The service runs as its own managed identities in Azure, so it needs no keys or
              secrets.
            </li>
          </ul>
        </div>
        <div className="card card-pad stack">
          <h3>Active Directory: the edges</h3>
          <ul className="stack" style={{ margin: 0, paddingLeft: 20 }}>
            <li>Every edge joins AD with winbind.</li>
            <li>
              Each NFS and SMB access is checked against the person&apos;s AD groups and the
              folder&apos;s POSIX ACLs.
            </li>
            <li>
              The group SIDs in people&apos;s Entra tokens tie the two together: the portal and the
              shares agree on who may do what.
            </li>
          </ul>
        </div>
      </div>
      <h3>Setup</h3>
      <StepList steps={items} label="Identity setup" />
    </div>
  );
}

function WhoAmI() {
  const who = useWhoAmI();
  if (who.isPending) return <Loading />;
  if (who.isError) return <ErrorCallout error={who.error} />;
  const w = who.data;
  return (
    <div className="card card-pad stack" aria-labelledby="whoami-h">
      <h3 id="whoami-h">Who am I</h3>
      <dl className="kv">
        <Fact label="Name">{w.display_name}</Fact>
        <Fact label="Sign-in name">{w.upn}</Fact>
        <Fact label="Entra object id">
          <span className="mono">{w.object_id}</span>
        </Fact>
        <Fact label="AD SID">{w.sid ? <span className="mono">{w.sid}</span> : "none"}</Fact>
        <Fact label="App roles">{w.roles.join(", ") || "none"}</Fact>
        <Fact label="AD groups">
          {w.group_count} (from {w.groups_source})
        </Fact>
        <Fact label="Owner of shares">{w.owner_of.length ? w.owner_of.join(", ") : "none"}</Fact>
        <Fact label="Signed in">{formatDateTime(w.session.auth_time)}</Fact>
        <Fact label="Session ends">{formatDateTime(w.session.expires_at)}</Fact>
      </dl>
      <CheckList checks={w.checks} label="Checks for you" />
    </div>
  );
}

export function IdentityPage() {
  usePageTitle("Settings: identity");
  const id = useIdentitySettings();
  if (id.isPending) return <Loading />;
  if (id.isError) return <ErrorCallout error={id.error} />;
  const s = id.data;
  return (
    <section className="stack" aria-labelledby="identity-h">
      <h2 id="identity-h">Identity</h2>
      <p className="lede">
        OmniFile uses two directories: Entra ID for people and the cloud, and your on-premises
        Active Directory for the edges and their shares.
      </p>
      <SetupSteps s={s} />
      <h3>Details</h3>
      <dl className="kv card card-pad">
        <Fact label="Tenant">
          <span className="mono">{s.tenant_id}</span>
        </Fact>
        <Fact label="Portal app (client id)">
          <span className="mono">{s.client_id}</span>
        </Fact>
        <Fact label="API audiences">
          <span className="mono">{s.api_audiences.join(", ") || "none"}</span>
        </Fact>
      </dl>
      <div className="card card-pad stack">
        <h3>Admin consent</h3>
        <p>
          <StatusBadge status={s.consent.status} /> {s.consent.detail}
        </p>
        <p>
          Only a tenant administrator can grant consent; OmniFile can&apos;t grant it to itself.{" "}
          <a href={s.consent.url} target="_blank" rel="noreferrer noopener">
            Open the admin consent page
          </a>
        </p>
      </div>
      <div className="card card-pad stack">
        <h3>Microsoft Graph</h3>
        <CheckList checks={[s.graph]} label="Graph" />
      </div>
      <div className="table-wrap card">
        <table className="table" aria-label="App roles">
          <thead>
            <tr>
              <th scope="col">Role</th>
              <th scope="col">For</th>
              <th scope="col">AD groups that hold it</th>
              <th scope="col" className="col-num">
                People seen (30 days)
              </th>
              <th scope="col">From</th>
            </tr>
          </thead>
          <tbody>
            {s.roles.map((r) => (
              <tr key={r.role}>
                <th scope="row">{r.role}</th>
                <td>
                  {r.member_types.map((t) => (t === "user" ? "people" : "API clients")).join(", ")}
                </td>
                <td>
                  {r.groups.length
                    ? r.groups.map((g) => g.display_name ?? g.sid ?? g.object_id ?? "?").join(", ")
                    : "—"}
                </td>
                <td className="num">{r.people_seen}</td>
                <td>
                  {r.source === "graph" ? "Graph" : r.source === "sign_ins" ? "Sign-ins" : r.source}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <WhoAmI />
    </section>
  );
}
