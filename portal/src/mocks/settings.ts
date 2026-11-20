import { http, HttpResponse, type JsonBodyType } from "msw";
import type { components } from "../api/schema.gen";
import type { MockDb } from "./db";
import { example } from "./examples.gen";

/**
 * MSW handlers for Settings (portal-api.md 5.20) and API clients (5.21), and
 * the price tables they show. The catch-all in handlers.ts runs first, so
 * CSRF and Idempotency-Key are checked there. State lives per mock database,
 * so `resetDb()` resets it too.
 */

type S = components["schemas"];
type Persona = MockDb["persona"];

interface SettingsState {
  backends: S["StorageBackend"][];
  clients: S["ApiClient"][];
  tests: Map<string, { test: S["SettingsTest"]; polls: number; pass: boolean }>;
  nextId: number;
}

const states = new WeakMap<MockDb, SettingsState>();
const iso = (ms: number) => new Date(ms).toISOString();
const json = (body: unknown, init?: ResponseInit) => HttpResponse.json(body as JsonBodyType, init);
const TENANT = "8f3a2b1c-0d9e-4f8a-b7c6-d5e4f3a2b1c0";
const PORTAL_APP = "1d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const AUDIENCE = `api://${PORTAL_APP}`;

/** Who holds the portal's roles in the mock deployment (decision 89); tests switch it. */
export const identityState: { roleHolders: "groups" | "people" } = { roleHolders: "groups" };

/** Settings, Identity when named people hold the roles, read from Graph. */
function peopleIdentity() {
  const person = (
    role: string,
    people: { object_id: string; display_name: string }[],
    seen: number,
  ) => ({
    role,
    member_types: ["user"],
    groups: [],
    people,
    people_seen: seen,
    source: "graph",
  });
  return {
    role_holders: "people",
    tenant_id: TENANT,
    client_id: PORTAL_APP,
    api_audiences: [AUDIENCE, PORTAL_APP],
    consent: {
      status: "granted",
      detail: "Tenant-wide consent for openid, profile.",
      url: `https://login.microsoftonline.com/${TENANT}/adminconsent?client_id=${PORTAL_APP}`,
    },
    graph: {
      code: "graph",
      status: "ok",
      title: "Reading Entra from Graph",
      message: "Roles, groups and consent read from Graph now.",
      fix: null,
    },
    roles: [
      person("ArchiveAdmin", [{ object_id: "u-ann", display_name: "Ann Admin" }], 1),
      person("DataSteward", [{ object_id: "u-sam", display_name: "Sam Steward" }], 1),
      person("User", [], 37),
    ],
  };
}

function problem(status: number, code: string, detail: string, extra: object = {}) {
  return HttpResponse.json(
    {
      type: `urn:omnifile:error:${code}`,
      title: code.replace(/_/g, " "),
      status,
      code,
      detail,
      retryable: false,
      retry_after_s: null,
      request_id: "mock-settings",
      ...extra,
    } as JsonBodyType,
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

function owner(db: MockDb, who: Persona): S["ApiClientOwner"] {
  const u = db.users[who];
  return {
    identity_id: u.identity_id,
    display_name: u.display_name ?? null,
    upn: u.upn ?? null,
    roles: u.roles,
    roles_seen_at: iso(Date.now() - 3 * 3600_000),
  };
}

function state(db: MockDb): SettingsState {
  let s = states.get(db);
  if (s) return s;
  const now = Date.now();
  const admin = db.users.admin;
  s = {
    nextId: 100,
    tests: new Map(),
    backends: [
      { ...example.StorageBackend, name: "azure-weu" },
      {
        backend_id: "2",
        name: "azure-worm",
        kind: "azure_blob",
        private_network: true,
        can_be_home: true,
        where: "account omniworm01, WORM, westeurope",
        config: {
          layout: "single_account",
          primary_account: "omniworm01",
          container: "omniworm01/data",
          worm: true,
          region: "westeurope",
        },
        uses: { datasets: 1, sources: 0, migrations: 0 },
        created_at: iso(now - 90 * 86_400_000),
      },
      {
        backend_id: "7",
        name: "s3-123456789012",
        kind: "s3",
        private_network: true,
        can_be_home: false,
        where: "account 123456789012, eu-west-1, buckets seismic-raw",
        config: { account_id: "123456789012", region: "eu-west-1", buckets: ["seismic-raw"] },
        uses: { datasets: 1, sources: 1, migrations: 2 },
        created_at: iso(now - 40 * 86_400_000),
      },
    ],
    clients: [
      {
        ...example.ApiClient,
        app_id: "5c7e1b2a-4d3c-4b2a-9e8f-7a6b5c4d3e2f",
        owner: owner(db, "steward"),
        created_by: {
          kind: "identity",
          display_name: admin.display_name ?? null,
          app_id: null,
          identity_id: admin.identity_id,
        },
        last_used_at: iso(now - 20 * 60_000),
        keys: [
          {
            key_id: "3f9c2a7b1d0e4c58",
            scopes: ["Archive.Read"],
            state: "active",
            created_at: iso(now - 30 * 86_400_000),
            created_by: {
              kind: "identity",
              display_name: admin.display_name ?? null,
              app_id: null,
              identity_id: admin.identity_id,
            },
            expires_at: iso(now + 60 * 86_400_000),
            revoked_at: null,
            rotated_from: null,
            last_used_at: iso(now - 2 * 3600_000),
            last_used_addr: "10.30.1.44",
          },
        ],
      },
    ],
  };
  states.set(db, s);
  return s;
}

function newTest(s: SettingsState, kind: string, pass: boolean): S["SettingsTest"] {
  const test: S["SettingsTest"] = {
    test_id: String(++s.nextId),
    kind,
    state: "pending",
    detail: null,
    result: {},
    created_at: iso(Date.now()),
    finished_at: null,
  };
  s.tests.set(test.test_id, { test, polls: 0, pass });
  return test;
}

const DETAIL: Record<string, string> = {
  teams_card: "Card sent to your Teams chat.",
  email: "Email sent to avery.chen@contoso-energy.com from omnifile-notify@contoso-energy.com.",
  aws_assume_role: "The service assumed the role with its web identity.",
};

const PEOPLE: Persona[] = ["admin", "steward", "reader"];

/** The handlers, given a way to read the current mock database. */
export function settingsHandlers(getDb: () => MockDb) {
  const admin = () => getDb().users[getDb().persona].roles.includes("ArchiveAdmin");
  const refuse = () => problem(403, "role_required", "needs the app role ArchiveAdmin");
  const st = () => state(getDb());
  const client = (id: string | readonly string[] | undefined) =>
    st().clients.find((c) => c.api_client_id === String(id));
  const bump = (c: S["ApiClient"], change: Partial<S["ApiClient"]>) => {
    Object.assign(c, change, {
      updated_at: iso(Date.now()),
      etag: `"${Number(c.etag.replace(/"/g, "")) + 1}"`,
    });
    return c;
  };

  return [
    http.get("*/v1/settings/storage", () => {
      if (!admin()) return refuse();
      return json({
        backends: st().backends,
        private_network: true,
        aws_configured: true,
        dev_mode: false,
      });
    }),
    http.post("*/v1/settings/storage", async ({ request }) => {
      if (!admin()) return refuse();
      const d = (await request.json()) as S["BackendDraft"];
      if (d.kind === "azure" && !d.account && (!d.live || !d.archive)) {
        return problem(
          400,
          "invalid_request",
          "the two-account layout needs both --live and --archive",
          { errors: [{ field: "archive", message: "the archive account is required" }] },
        );
      }
      const s = st();
      const name =
        d.name ??
        (d.kind === "s3" ? `s3-${d.account ?? ""}` : d.account ? `azure-${d.account}` : "azure");
      const existing = s.backends.find((b) => b.name === name);
      if (existing) return json({ outcome: "unchanged", backend: existing, changes: [] });
      const backend: S["StorageBackend"] = {
        backend_id: String(++s.nextId),
        name,
        kind: d.kind === "s3" ? "s3" : "azure_blob",
        private_network: true,
        can_be_home: d.kind !== "s3",
        where:
          d.kind === "s3"
            ? `account ${d.account}, ${d.region}, buckets ${(d.buckets ?? []).join(", ")}`
            : d.account
              ? `account ${d.account}${d.worm ? ", WORM" : ""}`
              : `live ${d.live}, archive ${d.archive}, Smart`,
        config:
          d.kind === "s3"
            ? { account_id: d.account, region: d.region, buckets: d.buckets }
            : d.account
              ? { layout: "single_account", primary_account: d.account, worm: !!d.worm }
              : { layout: "two_account", primary_account: d.live, archive_account: d.archive },
        uses: { datasets: 0, sources: 0, migrations: 0 },
        created_at: iso(Date.now()),
      };
      s.backends.push(backend);
      return json({ outcome: "added", backend, changes: [] }, { status: 201 });
    }),
    http.put("*/v1/settings/storage/:id", async ({ params, request }) => {
      if (!admin()) return refuse();
      const b = st().backends.find((x) => x.backend_id === String(params.id));
      if (!b) return problem(404, "not_found", "no such backend");
      const d = (await request.json()) as S["BackendDraft"];
      if (b.kind === "s3" && d.buckets) {
        const before = (b.config.buckets as string[] | undefined) ?? [];
        const gone = before.filter((x) => !(d.buckets ?? []).includes(x));
        if (gone.length && b.uses.sources) {
          return problem(400, "invalid_request", `buckets ${gone.join(", ")} would be dropped`);
        }
        b.config = { ...b.config, buckets: d.buckets };
        b.where = `account ${String(b.config.account_id)}, ${String(b.config.region)}, buckets ${d.buckets.join(", ")}`;
        return json({
          outcome: "updated",
          backend: b,
          changes: [`buckets: ${before.join(",")} -> ${d.buckets.join(",")}`],
        });
      }
      return json({ outcome: "unchanged", backend: b, changes: [] });
    }),
    http.get("*/v1/settings/storage/:id/checks", ({ params }) => {
      if (!admin()) return refuse();
      const b = st().backends.find((x) => x.backend_id === String(params.id));
      if (!b) return problem(404, "not_found", "no such backend");
      const checks: S["SettingCheck"][] =
        b.kind === "s3"
          ? [
              {
                code: "assume_role",
                status: "ok",
                title: "The service's role is assumable",
                message:
                  "AssumeRoleWithWebIdentity answered for arn:aws:iam::123456789012:role/omnifile-service.",
                fix: null,
              },
              {
                code: "private_dns",
                status: "ok",
                title: "S3 is reached through PrivateLink",
                message: "The S3 interface endpoint resolves to 10.40.2.15.",
                fix: null,
              },
            ]
          : [
              {
                code: "private_dns",
                status: "ok",
                title: "Private endpoint DNS",
                message:
                  "omnilive01.blob.core.windows.net resolves to 10.20.4.5, a private address.",
                fix: null,
              },
              {
                code: "rbac_role",
                status: b.backend_id === "2" ? "warning" : "ok",
                title: "The service's RBAC role",
                message:
                  b.backend_id === "2"
                    ? "The worker's identity has Storage Blob Data Reader on omniworm01, not Contributor."
                    : "The worker's identity has Storage Blob Data Contributor on both accounts.",
                fix:
                  b.backend_id === "2"
                    ? "Assign Storage Blob Data Contributor to the worker's managed identity on omniworm01."
                    : null,
              },
              {
                code: "archive_pair",
                status: "ok",
                title: "Live and archive accounts",
                message: "Two accounts: the live one is ZRS with Smart, the archive one LRS.",
                fix: null,
              },
              {
                code: "change_feed",
                status: "ok",
                title: "Change feed",
                message: "The change feed is on for the live account.",
                fix: null,
              },
            ];
      return json({ backend_id: b.backend_id, checked_at: iso(Date.now()), checks });
    }),

    http.get("*/v1/settings/aws", () => {
      if (!admin()) return refuse();
      return json({
        configured: true,
        private_network: true,
        audience: AUDIENCE,
        tenant_id: TENANT,
        issuer: `https://sts.windows.net/${TENANT}/`,
        subjects: ["7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"],
        accounts: [
          {
            account_id: "123456789012",
            region: "eu-west-1",
            role_arn: "arn:aws:iam::123456789012:role/omnifile-service",
            sts_endpoint: "https://vpce-0abc.sts.eu-west-1.vpce.amazonaws.com",
          },
        ],
      });
    }),
    http.get("*/v1/settings/aws/trust-policy", ({ request }) => {
      if (!admin()) return refuse();
      const arn = new URL(request.url).searchParams.get("role_arn") ?? "";
      const account = arn.split(":")[4] ?? "";
      const host = `sts.windows.net/${TENANT}/`;
      return json({
        role_arn: arn,
        account_id: account,
        configured_account: account === "123456789012",
        oidc_provider: {
          url: `https://${host}`,
          arn: `arn:aws:iam::${account}:oidc-provider/${host}`,
          audience: AUDIENCE,
        },
        trust_policy: {
          Version: "2012-10-17",
          Statement: [
            {
              Effect: "Allow",
              Principal: { Federated: `arn:aws:iam::${account}:oidc-provider/${host}` },
              Action: "sts:AssumeRoleWithWebIdentity",
              Condition: {
                StringEquals: {
                  [`${host}:aud`]: AUDIENCE,
                  [`${host}:sub`]: ["7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"],
                },
              },
            },
          ],
        },
        steps: [
          `In IAM, add an OpenID Connect identity provider for https://${host} with the audience ${AUDIENCE}.`,
          "Set the role's trust policy to the one shown.",
          "Give the role the permissions in deploy/aws/iam.",
          "Press Test.",
        ],
      });
    }),
    http.post("*/v1/settings/aws/test", () => {
      if (!admin()) return refuse();
      return json(newTest(st(), "aws_assume_role", true), { status: 202 });
    }),
    http.post("*/v1/settings/teams/test-card", () => {
      if (!admin()) return refuse();
      return json(newTest(st(), "teams_card", true), { status: 202 });
    }),
    http.post("*/v1/settings/email/test", () => {
      if (!admin()) return refuse();
      return json(newTest(st(), "email", true), { status: 202 });
    }),
    http.get("*/v1/settings/tests/:id", ({ params }) => {
      if (!admin()) return refuse();
      const t = st().tests.get(String(params.id));
      if (!t) return problem(404, "not_found", "no such test");
      t.polls += 1;
      if (t.polls >= 2 && t.test.state === "pending") {
        t.test = {
          ...t.test,
          state: t.pass ? "passed" : "failed",
          detail: DETAIL[t.test.kind] ?? "Done.",
          result:
            t.test.kind === "aws_assume_role"
              ? {
                  assumed_role_arn:
                    "arn:aws:sts::123456789012:assumed-role/omnifile-service/omnifile-settings-test",
                }
              : {},
          finished_at: iso(Date.now()),
        };
      }
      return json(t.test);
    }),

    http.get("*/v1/settings/identity", () => {
      if (!admin()) return refuse();
      if (identityState.roleHolders === "people") return json(peopleIdentity());
      return json({
        role_holders: "groups",
        tenant_id: TENANT,
        client_id: PORTAL_APP,
        api_audiences: [AUDIENCE, PORTAL_APP],
        consent: {
          status: "granted",
          detail: "People have signed in to the portal, so its delegated sign-in is consented.",
          url: `https://login.microsoftonline.com/${TENANT}/adminconsent?client_id=${PORTAL_APP}`,
        },
        graph: {
          code: "graph_roles",
          status: "warning",
          title: "Role assignments from Graph",
          message:
            "Graph refused the role assignments (needs Application.Read.All), so the groups below come from sign-ins.",
          fix: "Grant the web identity Application.Read.All, or check Enterprise applications, Users and groups.",
        },
        roles: [
          {
            role: "ArchiveAdmin",
            member_types: ["user"],
            groups: [
              {
                object_id: null,
                display_name: "OmniFile Admins",
                sid: "S-1-5-21-1000-2000-3000-2201",
              },
            ],
            people: [],
            people_seen: 2,
            source: "sign_ins",
          },
          {
            role: "DataSteward",
            member_types: ["user"],
            groups: [
              {
                object_id: null,
                display_name: "Data Stewards",
                sid: "S-1-5-21-1000-2000-3000-2202",
              },
            ],
            people: [],
            people_seen: 3,
            source: "sign_ins",
          },
          {
            role: "User",
            member_types: ["user"],
            groups: [],
            people: [],
            people_seen: 41,
            source: "sign_ins",
          },
          {
            role: "Policy.Propose",
            member_types: ["application"],
            groups: [],
            people: [],
            people_seen: 0,
            source: "sign_ins",
          },
        ],
      });
    }),
    http.get("*/v1/settings/identity/whoami", () => {
      if (!admin()) return refuse();
      const db = getDb();
      const u = db.users[db.persona];
      return json({
        object_id: u.object_id,
        identity_id: u.identity_id,
        display_name: u.display_name,
        upn: u.upn,
        sid: u.sid,
        roles: u.roles,
        groups_source: u.groups_source,
        group_count: 6,
        owner_of: u.owner_of,
        session: {
          auth_time: iso(db.authTime),
          expires_at: iso(Date.now() + 9 * 3600_000),
          recently_authenticated: Date.now() - db.authTime < 600_000,
        },
        checks: [
          {
            code: "sid",
            status: "ok",
            title: "AD identity",
            message: "Your sign-in carries your on-premises SID, so file access checks work.",
            fix: null,
          },
          {
            code: "groups",
            status: "ok",
            title: "AD groups",
            message: "6 groups, from your token.",
            fix: null,
          },
        ],
      });
    }),

    http.get("*/v1/settings/teams", () => {
      if (!admin()) return refuse();
      return json({
        configured: true,
        teams_app_id: "c0ffee00-1111-4222-8333-444455556666",
        bot_app_id: "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        health: {
          status: "ok",
          cards_sent_24h: 14,
          cards_failed_24h: 0,
          last_sent_at: iso(Date.now() - 40 * 60_000),
          last_error: null,
          conversations_blocked: 0,
          message: "Cards are reaching people.",
        },
        package: {
          available: false,
          file_name: null,
          instructions: [
            "Build the package: deploy/teams/build.sh (it writes deploy/teams/out/omnifile-teams-app.zip).",
            "Upload it in the Teams admin centre, or run deploy/entra/register.sh --teams-app-zip.",
          ],
        },
      });
    }),
    http.get("*/v1/settings/teams/package", () =>
      problem(404, "not_found", "the Teams app package isn't reachable from the service"),
    ),
    http.get("*/v1/settings/email", () => {
      if (!admin()) return refuse();
      return json({
        configured: true,
        sender: "omnifile-notify@contoso-energy.com",
        my_mail: "avery.chen@contoso-energy.com",
        health: {
          sent_24h: 2,
          failed_24h: 0,
          last_sent_at: iso(Date.now() - 5 * 3600_000),
          last_error: null,
        },
        instructions: [],
      });
    }),
    http.get("*/v1/settings/alerts", () => {
      if (!admin()) return refuse();
      return json({
        where:
          "The rules live in Azure Monitor (deploy/azure/modules/app-metrics.bicep) and go to your action group. This page shows what the catalog says now.",
        alerts: [
          {
            name: "Edge silent",
            condition: "No heartbeat for 15 minutes",
            status: "ok",
            detail: "Every edge reported in the last minute.",
          },
          {
            name: "Migration failed",
            condition: "A migration prefix in failed",
            status: "firing",
            detail: "1 migration failed: raw/2019/.",
          },
          {
            name: "Queue backlog",
            condition: "A queue's oldest message older than 15 minutes",
            status: "unknown",
            detail: "See Azure Monitor.",
          },
        ],
      });
    }),
    http.get("*/v1/price-tables/:kind", ({ params }) => {
      const kind = String(params.kind);
      if (kind === "s3") {
        return json({
          backend_kind: "s3",
          region: "eu-west-1",
          currency: "USD",
          as_of: "2026-09-26",
          verify: true,
          overridden: false,
          etag: '"0"',
          classes: {
            STANDARD: { gb_month: "0.023" },
            GLACIER: { gb_month: "0.0036", retrieval_per_gb: "0.01", minimum_days: 90 },
          },
        });
      }
      return json({
        backend_kind: "azure_blob",
        region: "westeurope",
        currency: "USD",
        as_of: "2026-09-26",
        verify: false,
        overridden: false,
        etag: '"0"',
        classes: {
          Hot: { gb_month: "0.0184" },
          Cool: { gb_month: "0.01", minimum_days: 30 },
          Cold: { gb_month: "0.0036", minimum_days: 90 },
          Archive: { gb_month: "0.00099", retrieval_per_gb: "0.022", minimum_days: 180 },
        },
      });
    }),

    http.get("*/v1/settings/people", ({ request }) => {
      if (!admin()) return refuse();
      const q = (new URL(request.url).searchParams.get("q") ?? "").toLowerCase();
      const db = getDb();
      const items = PEOPLE.map((p) => owner(db, p)).filter(
        (o) =>
          (o.display_name ?? "").toLowerCase().startsWith(q) ||
          (o.upn ?? "").toLowerCase().startsWith(q),
      );
      return json({ items });
    }),
    http.get("*/v1/settings/api-clients", () => {
      if (!admin()) return refuse();
      return json({
        items: [...st().clients].sort((a, b) => a.display_name.localeCompare(b.display_name)),
        setup: {
          tenant_id: TENANT,
          token_endpoint: `https://login.microsoftonline.com/${TENANT}/oauth2/v2.0/token`,
          scope: `${AUDIENCE}/.default`,
          audiences: [AUDIENCE, PORTAL_APP],
          app_roles: ["Policy.Propose", "Recall.Request", "Archive.Read", "Archive.Adopt"],
          steps: [
            "In Entra, the client's own app registration gets a certificate (preferred) or uses workload identity federation. OmniFile never sees it.",
            "An admin assigns the client the app roles it needs on the OmniFile portal app (Enterprise applications, the client's permissions), and grants admin consent.",
            `The client requests a token with the client credentials grant for the scope ${AUDIENCE}/.default.`,
            "Register the client here with the same scopes, and name its owner.",
            "Only if the client can't hold an Entra credential: create an OmniFile API key on its page and send it as Authorization: Bearer <key>.",
          ],
        },
      });
    }),
    http.post("*/v1/settings/api-clients", async ({ request }) => {
      if (!admin()) return refuse();
      const b = (await request.json()) as S["ApiClientCreate"];
      const s = st();
      if (s.clients.some((c) => c.app_id === b.app_id)) {
        return problem(409, "name_taken", "that application is already registered");
      }
      const db = getDb();
      const who = PEOPLE.find((p) => db.users[p].identity_id === b.owner_identity_id);
      if (!who) return problem(400, "invalid_request", "owner_identity_id: no such person");
      const me = db.users[db.persona];
      const c: S["ApiClient"] = {
        api_client_id: String(++s.nextId),
        app_id: b.app_id,
        display_name: b.display_name,
        scopes: b.scopes,
        owner: owner(db, who),
        state: "enabled",
        created_by: {
          kind: "identity",
          display_name: me.display_name ?? null,
          app_id: null,
          identity_id: me.identity_id,
        },
        created_at: iso(Date.now()),
        updated_at: iso(Date.now()),
        disabled_at: null,
        revoked_at: null,
        last_used_at: null,
        etag: '"1"',
      };
      s.clients.push(c);
      return json(c, { status: 201 });
    }),
    http.get("*/v1/settings/api-clients/:id", ({ params }) => {
      if (!admin()) return refuse();
      const c = client(params.id);
      return c ? json(c) : problem(404, "not_found", "no such API client");
    }),
    http.patch("*/v1/settings/api-clients/:id", async ({ params, request }) => {
      if (!admin()) return refuse();
      const c = client(params.id);
      if (!c) return problem(404, "not_found", "no such API client");
      const ifMatch = request.headers.get("If-Match");
      if (!ifMatch) return problem(428, "precondition_required", "If-Match is required");
      if (ifMatch !== c.etag) return problem(412, "precondition_failed", "it changed");
      if (c.state === "revoked") return problem(409, "client_disabled", "the client is revoked");
      const b = (await request.json()) as S["ApiClientPatch"];
      const change: Partial<S["ApiClient"]> = {};
      if (b.display_name !== undefined) change.display_name = b.display_name;
      if (b.scopes !== undefined) change.scopes = b.scopes;
      if (b.owner_identity_id !== undefined) {
        const db = getDb();
        const who = PEOPLE.find((p) => db.users[p].identity_id === b.owner_identity_id);
        if (!who) return problem(400, "invalid_request", "owner_identity_id: no such person");
        change.owner = owner(db, who);
      }
      if (b.enabled === false) {
        change.state = "disabled";
        change.disabled_at = iso(Date.now());
      } else if (b.enabled === true) {
        change.state = "enabled";
        change.disabled_at = null;
      }
      return json(bump(c, change));
    }),
    http.post("*/v1/settings/api-clients/:id/revoke", ({ params }) => {
      if (!admin()) return refuse();
      const c = client(params.id);
      if (!c) return problem(404, "not_found", "no such API client");
      if (c.state === "revoked") return json(c);
      const at = iso(Date.now());
      const keys = (c.keys ?? []).map((k) =>
        k.state === "active" ? { ...k, state: "revoked", revoked_at: at } : k,
      );
      return json(bump(c, { state: "revoked", revoked_at: at, keys }));
    }),

    // OmniFile-issued API keys (owner decision 2026-11-04): the key is in the
    // 201 answer only; the client's key list never holds it.
    http.post("*/v1/settings/api-clients/:id/keys", async ({ params, request }) => {
      if (!admin()) return refuse();
      const c = client(params.id);
      if (!c) return problem(404, "not_found", "no such API client");
      if (c.state !== "enabled") return problem(409, "client_disabled", "the client isn't enabled");
      const b = (await request.json()) as S["ApiKeyCreate"];
      const scopes = b.scopes ?? c.scopes;
      const outside = scopes.filter((s) => !c.scopes.includes(s));
      if (outside.length) {
        return problem(400, "invalid_request", `scopes: ${outside.join(", ")} isn't the client's`);
      }
      const { key, apiKey } = issue(getDb(), scopes, b.expires_in_days ?? 90, null);
      c.keys = [...(c.keys ?? []), apiKey];
      return json({ key, api_key: apiKey }, { status: 201 });
    }),
    http.post("*/v1/settings/api-clients/:id/keys/:keyId/rotate", async ({ params, request }) => {
      if (!admin()) return refuse();
      const c = client(params.id);
      const old = c?.keys?.find((k) => k.key_id === String(params.keyId));
      if (!c || !old) return problem(404, "not_found", "no such key");
      if (old.state !== "active")
        return problem(409, "client_disabled", "only an active key rotates");
      const b = (await request.json()) as S["ApiKeyRotate"];
      const until = Date.now() + (b.overlap_hours ?? 168) * 3600_000;
      if (until < Date.parse(old.expires_at)) old.expires_at = iso(until);
      const { key, apiKey } = issue(getDb(), old.scopes, b.expires_in_days ?? 90, old.key_id);
      c.keys = [...(c.keys ?? []), apiKey];
      return json({ key, api_key: apiKey, previous: old }, { status: 201 });
    }),
    http.post("*/v1/settings/api-clients/:id/keys/:keyId/revoke", ({ params }) => {
      if (!admin()) return refuse();
      const c = client(params.id);
      const k = c?.keys?.find((x) => x.key_id === String(params.keyId));
      if (!k) return problem(404, "not_found", "no such key");
      if (k.state !== "revoked")
        Object.assign(k, { state: "revoked", revoked_at: iso(Date.now()) });
      return json(k);
    }),
  ];
}

const B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

function randomString(n: number, alphabet: string): string {
  const bytes = crypto.getRandomValues(new Uint8Array(n));
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

/** A key of the real shape, ^ofk1\.[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$, and its row. */
function issue(
  db: MockDb,
  scopes: S["ApiKey"]["scopes"],
  days: number,
  rotatedFrom: string | null,
): { key: string; apiKey: S["ApiKey"] } {
  const keyId = randomString(16, "0123456789abcdef");
  const me = db.users[db.persona];
  const now = Date.now();
  return {
    key: `ofk1.${keyId}.${randomString(43, B64URL)}`,
    apiKey: {
      key_id: keyId,
      scopes,
      state: "active",
      created_at: iso(now),
      created_by: {
        kind: "identity",
        display_name: me.display_name ?? null,
        app_id: null,
        identity_id: me.identity_id,
      },
      expires_at: iso(now + days * 86_400_000),
      revoked_at: null,
      rotated_from: rotatedFrom,
      last_used_at: null,
      last_used_addr: null,
    },
  };
}
