import { screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { browserNav } from "../../api/client";
import { cloudState, seedAzureConnected } from "../../mocks/cloud";
import { requestLog } from "../../mocks/handlers";
import { identityState } from "../../mocks/settings";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";
import { cloudPolling } from "./cloudQueries";

const WAIT = { timeout: 4000 };
const posts = (path: string) => requestLog.filter((r) => r.method === "POST" && r.path === path);
const SIGN_IN = "/v1/settings/azure/sign-in";

beforeEach(() => {
  cloudPolling.taskMs = 20;
  cloudPolling.moverMs = 20;
});
afterEach(() => {
  cloudPolling.taskMs = 1000;
  cloudPolling.moverMs = 15_000;
});

/** Step 1 and 2: sign in to list, then choose rg-storage and rg-network, Mover in rg-migration. */
async function listAndChoose(user: UserEvent) {
  await user.click(await screen.findByRole("button", { name: "Sign in to Azure" }));
  await waitFor(() => expect(browserNav.assign).toHaveBeenCalled());
  await user.click(await screen.findByRole("checkbox", { name: /rg-storage/ }));
  await user.click(screen.getByRole("checkbox", { name: /rg-network/ }));
  await user.selectOptions(
    screen.getByLabelText("Resource group for Storage Mover (optional)"),
    "rg-migration",
  );
  await user.click(screen.getByRole("button", { name: "Show the grants this needs" }));
  return screen.findByRole("list", { name: "Planned grants" });
}

describe("Connect Azure", () => {
  it("signs in through the navigation helper and lists only the deployment's tenant", async () => {
    const { user, router } = renderApp("/settings/azure");
    expect(await screen.findByText("OmniFile never keeps your Azure sign-in")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign in to Azure" }));
    await waitFor(() =>
      expect(browserNav.assign).toHaveBeenCalledWith(
        expect.stringMatching(/^\/settings\/azure\?op=\d+$/),
      ),
    );
    expect(posts(SIGN_IN)[0]?.body).toEqual({ purpose: "list" });
    expect(posts(SIGN_IN)[0]?.headers["idempotency-key"]).toBeTruthy();

    // Entra sends the admin back with ?op=: the outcome is shown.
    const url = (browserNav.assign as unknown as { mock: { calls: string[][] } }).mock
      .calls[0]?.[0];
    await router.navigate(url ?? "/settings/azure");
    expect(await screen.findByText("Listing what you can see in Azure: done")).toBeInTheDocument();
    const contoso = await screen.findByRole("radio", { name: /Contoso/ });
    expect(contoso).toBeChecked();
    const fabrikam = screen.getByRole("radio", { name: /Fabrikam/ });
    expect(fabrikam).toBeDisabled();
    expect(fabrikam).toHaveAccessibleDescription(/Azure Lighthouse/);
    expect(screen.getByLabelText("Subscription")).toHaveValue(
      "00000000-0000-4000-8000-0000000000a1",
    );
  });

  it("shows every grant with its actions before anything is granted", async () => {
    const { user, container } = renderApp("/settings/azure");
    const plan = await listAndChoose(user);
    const items = within(plan).getAllByRole("article");
    expect(items.map((a) => a.getAttribute("aria-labelledby") && a.textContent)).toHaveLength(4);
    expect(
      within(plan).getByRole("article", {
        name: "OmniFile Discovery Reader (rg-storage, 00000000)",
      }),
    ).toHaveTextContent(
      "/subscriptions/00000000-0000-4000-8000-0000000000a1/resourceGroups/rg-storage",
    );
    const mover = within(plan).getByRole("article", {
      name: "OmniFile Mover Deployer (rg-migration, 00000000)",
    });
    expect(mover).toHaveTextContent(/worker/);
    const actions = within(mover).getByRole("list", {
      name: "Actions in OmniFile Mover Deployer (rg-migration, 00000000)",
    });
    expect(
      within(actions).getByText("Microsoft.StorageMover/storageMovers/write"),
    ).toBeInTheDocument();
    expect(within(actions).getAllByRole("listitem")).toHaveLength(11);
    expect(within(plan).getAllByText("Planned")).toHaveLength(4);
    // The choice was sent; nothing was granted.
    const put = requestLog.find(
      (r) => r.method === "PUT" && r.path === "/v1/settings/azure/choice",
    );
    expect(put?.body).toEqual({
      subscription_id: "00000000-0000-4000-8000-0000000000a1",
      resource_groups: ["rg-storage", "rg-network"],
      mover_resource_group: "rg-migration",
    });
    expect(
      posts(SIGN_IN).filter((r) => (r.body as { purpose: string }).purpose === "grant"),
    ).toHaveLength(0);
    expect(screen.queryByRole("table", { name: "Grants" })).not.toBeInTheDocument();
    await expectNoAxeViolations(container);

    await user.click(screen.getByRole("button", { name: "Sign in and make these grants" }));
    await waitFor(() => expect(posts(SIGN_IN)).toHaveLength(2));
    expect(posts(SIGN_IN)[1]?.body).toEqual({ purpose: "grant" });
    const grants = await screen.findByRole("table", { name: "Grants" }, WAIT);
    expect(within(grants).getAllByRole("button", { name: /^Remove / })).toHaveLength(4);
  });

  it("asks before removing a grant, then signs in to remove it", async () => {
    const { user, db } = renderApp("/settings/azure");
    seedAzureConnected(db);
    const grants = await screen.findByRole("table", { name: "Grants" });
    const before = posts(SIGN_IN).length;
    await user.click(
      within(grants).getByRole("button", {
        name: "Remove OmniFile Discovery Reader (rg-network, 00000000)",
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "Remove this grant?" });
    expect(within(dialog).getByText(/can't read the storage and network/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(posts(SIGN_IN)).toHaveLength(before);

    await user.click(
      within(grants).getByRole("button", {
        name: "Remove OmniFile Discovery Reader (rg-network, 00000000)",
      }),
    );
    await user.click(
      within(screen.getByRole("dialog", { name: "Remove this grant?" })).getByRole("button", {
        name: "Sign in and remove",
      }),
    );
    await waitFor(() => expect(posts(SIGN_IN)).toHaveLength(before + 1));
    const sent = posts(SIGN_IN)[before]?.body as { purpose: string; grant_ids: string[] };
    expect(sent.purpose).toBe("remove");
    expect(sent.grant_ids).toHaveLength(1);
    expect(browserNav.assign).toHaveBeenCalledWith(expect.stringMatching(/\?op=\d+$/));
  });

  it("discovers, and lets only privately reachable storage accounts be ticked", async () => {
    const { user, container, db } = renderApp("/settings/azure");
    seedAzureConnected(db);
    await user.click(await screen.findByRole("button", { name: "Discover" }));
    const accounts = await screen.findByRole("table", { name: "Storage accounts" });
    expect(within(accounts).getByRole("row", { name: /omnilive01/ })).toHaveTextContent("Private");
    expect(within(accounts).getByRole("row", { name: /contososhared01/ })).toHaveTextContent(
      "Not private",
    );
    expect(screen.getByRole("table", { name: "Virtual networks" })).toHaveTextContent("vnet-hub");
    expect(screen.getByRole("table", { name: "Private endpoints" })).toHaveTextContent(
      "pe-omnilive01-blob",
    );
    expect(screen.getByRole("table", { name: "Storage movers" })).toHaveTextContent(
      "contoso-mover",
    );

    const shared = screen.getByRole("checkbox", { name: /contososhared01/ });
    expect(shared).toBeDisabled();
    expect(shared).toHaveAccessibleDescription(/not reachable privately/);
    const live = screen.getByRole("checkbox", { name: /^omnilive01/ });
    expect(live).toBeEnabled();
    await user.click(live);
    await user.click(screen.getByRole("checkbox", { name: /vnet-hub/ }));
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("Confirmed")).toBeInTheDocument();
    const [sent] = posts("/v1/settings/azure/confirm");
    expect(sent?.body).toMatchObject({
      storage_accounts: [expect.stringMatching(/storageAccounts\/omnilive01$/)],
      virtual_networks: [expect.stringMatching(/virtualNetworks\/vnet-hub$/)],
      private_endpoints: [],
    });
    await expectNoAxeViolations(container);
  });

  it("is ArchiveAdmin only", async () => {
    renderApp("/settings/azure", { persona: "steward" });
    expect(await screen.findByText("Settings are for ArchiveAdmins.")).toBeInTheDocument();
    expect(requestLog.some((r) => r.path.startsWith("/v1/settings/azure"))).toBe(false);
  });
});

describe("Identity", () => {
  it("starts with what Entra ID and AD are for, and a done / to-do list", async () => {
    const { container } = renderApp("/settings/identity");
    expect(
      await screen.findByRole("heading", { name: "Entra ID: people and the cloud" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Active Directory: the edges" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Every edge joins AD with winbind/)).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Identity setup" });
    const item = (name: RegExp) =>
      within(list)
        .getAllByRole("listitem")
        .find((li) => name.test(li.textContent));
    expect(item(/Admin consent for the portal app/)).toHaveTextContent("Done");
    expect(item(/App roles given to AD groups/)).toHaveTextContent(/To do.*User/);
    await waitFor(() =>
      expect(item(/Your token carries your AD identity/)).toHaveTextContent("Done"),
    );
    await waitFor(() => expect(item(/Azure connected/)).toHaveTextContent(/To do.*of 6 steps/));
    expect(within(list).getByRole("link", { name: "Settings › Azure" })).toHaveAttribute(
      "href",
      "/settings/azure",
    );
    await waitFor(() => expect(item(/At least one edge joined to AD/)).toHaveTextContent("Done"));
    // Everything that was there before is still there.
    expect(screen.getByRole("link", { name: "Open the admin consent page" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "App roles" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Microsoft Graph" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Who am I" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("with named people, lists them and calls every AD user a User", async () => {
    identityState.roleHolders = "people";
    try {
      const { container } = renderApp("/settings/identity");
      const list = await screen.findByRole("list", { name: "Identity setup" });
      const item = (name: RegExp) =>
        within(list)
          .getAllByRole("listitem")
          .find((li) => name.test(li.textContent));
      // No "give the roles to AD groups" to-do: nobody expects groups here.
      expect(item(/App roles given to AD groups/)).toBeUndefined();
      expect(item(/App roles given to named people/)).toHaveTextContent("Done");
      expect(
        screen.getByText(
          "ArchiveAdmin and DataSteward go to people named in Entra, and every AD user in the tenant is a User.",
        ),
      ).toBeInTheDocument();
      const table = screen.getByRole("table", { name: "App roles" });
      const row = (role: string) =>
        within(table).getByRole("row", { name: new RegExp(`^${role}`) });
      expect(row("ArchiveAdmin")).toHaveTextContent("Ann Admin");
      expect(row("DataSteward")).toHaveTextContent("Sam Steward");
      expect(row("User")).toHaveTextContent("Every AD user in the tenant");
      await expectNoAxeViolations(container);
    } finally {
      identityState.roleHolders = "groups";
    }
  });
});

describe("Connect AWS", () => {
  it("launches the stack with this deployment's values", async () => {
    const { user, container } = renderApp("/settings/aws");
    const launch = await screen.findByRole("link", { name: "Launch in AWS" });
    expect(launch).toHaveAttribute("target", "_blank");
    expect(launch).toHaveAttribute("rel", "noopener noreferrer");
    const [page, quick] = (launch.getAttribute("href") ?? "").split("#/stacks/quickcreate?");
    expect(new URL(page ?? "").hostname).toBe("us-east-1.console.aws.amazon.com");
    const href = { searchParams: new URLSearchParams(quick) };
    expect(href.searchParams.get("stackName")).toBe("omnifile-omnifile");
    expect(href.searchParams.get("param_TenantId")).toBe("8f3a2b1c-0d9e-4f8a-b7c6-d5e4f3a2b1c0");
    expect(href.searchParams.get("param_Audience")).toBe(
      "api://1d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    );
    expect(href.searchParams.get("templateURL")).toMatch(/^https:\/\/.+\.s3\.amazonaws\.com\//);
    expect(screen.getByRole("list", { name: "Connect AWS steps" })).toHaveTextContent(
      /EventBridge/,
    );
    // The old tool stays below, by hand.
    expect(screen.getByRole("heading", { name: "By hand" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Role ARN" })).toBeInTheDocument();

    const dep = screen.getByRole("textbox", { name: "Deployment id" });
    await user.clear(dep);
    await user.type(dep, "contoso1");
    await user.clear(screen.getByRole("textbox", { name: "AWS region" }));
    await user.type(screen.getByRole("textbox", { name: "AWS region" }), "eu-west-1");
    await user.click(screen.getByRole("button", { name: "Use these values" }));
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Launch in AWS" })).toHaveAttribute(
        "href",
        expect.stringContaining("param_DeploymentId=contoso1"),
      ),
    );
    expect(
      requestLog.some(
        (r) => r.path === "/v1/settings/aws/connect?region=eu-west-1&deployment_id=contoso1",
      ),
    ).toBe(true);
    await expectNoAxeViolations(container);
  });

  it("offers the template download when there is no quick-create link", async () => {
    const { db } = renderApp("/settings/aws");
    cloudState(db).templateUrl = null;
    expect(await screen.findByRole("link", { name: "Download template" })).toHaveAttribute(
      "href",
      "/v1/settings/aws/connect/template",
    );
    expect(screen.queryByRole("link", { name: "Launch in AWS" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open CloudFormation" })).toHaveAttribute(
      "href",
      "https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create",
    );
    const params = screen.getByRole("table", { name: "Stack parameters" });
    expect(within(params).getByRole("row", { name: /ServiceSubjects/ })).toHaveTextContent(
      "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    );
  });

  it("fetches the account, polls the task, and confirms the settings entry", async () => {
    const { user } = renderApp("/settings/aws");
    await user.type(await screen.findByRole("textbox", { name: "AWS account id" }), "210987654321");
    await user.type(
      screen.getByRole("textbox", { name: "S3 endpoint" }),
      "https://vpce-0abc.s3.us-east-1.vpce.amazonaws.com",
    );
    await user.click(screen.getByRole("button", { name: "Fetch" }));
    const buckets = await screen.findByRole("table", { name: "Buckets found" }, WAIT);
    expect(posts("/v1/settings/aws/connect/fetch")[0]?.body).toEqual({
      account_id: "210987654321",
      region: "us-east-1",
      deployment_id: "omnifile",
      endpoints: { s3: "https://vpce-0abc.s3.us-east-1.vpce.amazonaws.com" },
    });
    expect(
      requestLog.filter((r) => r.path.startsWith("/v1/settings/tasks/")).length,
    ).toBeGreaterThanOrEqual(2);
    expect(within(buckets).getByRole("row", { name: /acme-wells/ })).toHaveTextContent(
      /Off: turn it on/,
    );
    expect(within(buckets).getByRole("checkbox", { name: "Use acme-seismic" })).toBeChecked();
    expect(within(buckets).getByRole("checkbox", { name: "Use acme-logs" })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    const value = await screen.findByLabelText("OMNIFILE_AWS_ACCOUNTS value");
    expect(value.textContent).toContain('"210987654321"');
    expect(value.textContent).toContain("omnifile-service-omnifile");
    const [sent] = posts("/v1/settings/aws/connect/confirm");
    expect(sent?.body).toMatchObject({
      account_id: "210987654321",
      region: "us-east-1",
      deployment_id: "omnifile",
      buckets: ["acme-seismic", "acme-wells"],
      outputs: expect.objectContaining({
        RoleArn: "arn:aws:iam::210987654321:role/omnifile-service-omnifile",
      }) as unknown,
    });
    expect(screen.getByRole("button", { name: "Copy OMNIFILE_AWS_ACCOUNTS" })).toBeInTheDocument();
    expect(screen.getByText("Not deployed yet")).toBeInTheDocument();
    expect(
      await screen.findByRole("table", { name: "Connected AWS accounts" }, WAIT),
    ).toHaveTextContent("210987654321");
  });

  it("reads pasted outputs instead of calling AWS", async () => {
    const { user } = renderApp("/settings/aws");
    await user.click(await screen.findByText("Or paste the stack's outputs"));
    await user.type(
      screen.getByLabelText("The stack's outputs"),
      "AccountId=345678901234\nRegion=eu-west-1\nDeploymentId=omnifile\nSourceBuckets=acme-seismic",
    );
    await user.click(screen.getByRole("button", { name: "Read the outputs" }));
    expect(
      await screen.findByRole("heading", { name: "What the outputs say" }),
    ).toBeInTheDocument();
    expect(posts("/v1/settings/aws/connect/fetch")).toHaveLength(0);
    expect(screen.getByRole("checkbox", { name: "acme-seismic" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    const value = await screen.findByLabelText("OMNIFILE_AWS_ACCOUNTS value");
    expect(value.textContent).toContain('"345678901234"');
    expect(posts("/v1/settings/aws/connect/confirm")[0]?.body).toMatchObject({
      account_id: "345678901234",
      region: "eu-west-1",
      buckets: ["acme-seismic"],
    });
  });
});

describe("Storage Mover deployment", () => {
  it("says why it can't deploy until Azure is connected", async () => {
    renderApp("/settings/storage-mover");
    expect(
      await screen.findByText("Storage Mover can't be deployed from here yet"),
    ).toBeInTheDocument();
    expect(screen.getByText(/needs its Mover Deployer grant there/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect Azure" })).toHaveAttribute(
      "href",
      "/settings/azure",
    );
    expect(screen.queryByRole("button", { name: "Deploy" })).not.toBeInTheDocument();
  });

  it("deploys, shows the agent's steps, and follows the agent until it is Online", async () => {
    const { user, db, container } = renderApp("/settings/storage-mover");
    seedAzureConnected(db);
    await user.click(await screen.findByRole("button", { name: "Deploy" }));
    expect(posts("/v1/settings/storage-mover/deploy")[0]?.body).toEqual({
      name: "omnifile-mover",
      project: "migrations",
      location: "westeurope",
    });
    const steps = await screen.findByRole("list", { name: "Agent steps" }, WAIT);
    expect(within(steps).getByRole("link", { name: /learn.microsoft.com/ })).toHaveAttribute(
      "href",
      "https://learn.microsoft.com/azure/storage-mover/agent-deploy",
    );
    expect(screen.getByLabelText("Registration values")).toHaveTextContent("rg-migration");
    expect(screen.getByLabelText("OMNIFILE_STORAGE_MOVER value").textContent).toContain(
      '"storage_mover": "omnifile-mover"',
    );
    expect(
      screen.getByText(/copies migrated files from S3; the migration engine defines and runs/),
    ).toBeInTheDocument();
    const agents = screen.getByRole("table", { name: "Agents" });
    expect(await within(agents).findByText("Online", {}, WAIT)).toBeInTheDocument();
    expect(screen.getByText("An agent is Online.")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });
});
