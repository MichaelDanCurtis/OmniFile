import { screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { http } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { server } from "../../mocks/server";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";
import { polling, scanPolling } from "./api";

const posts = (path: string) => requestLog.filter((r) => r.method === "POST" && r.path === path);
const WAIT = { timeout: 4000 };

beforeEach(() => {
  polling.ms = 20;
  scanPolling.ms = 20;
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(() => {
  polling.ms = 3000;
  scanPolling.ms = 1000;
});

async function pickGroup(user: UserEvent, role: string, search: string) {
  await user.type(screen.getByLabelText(`${role} group`), search);
  const results = await screen.findByRole("list", { name: `Groups matching ${search}` });
  await user.click(await within(results).findByRole("button", { name: new RegExp(search, "i") }));
}

const next = (user: UserEvent) => user.click(screen.getByRole("button", { name: "Next" }));

/** Source: aws-us-east-1, bucket acme-wells, prefix wells/. */
async function chooseSource(user: UserEvent) {
  await screen.findByRole("heading", { name: "Where is the data now?" });
  await user.selectOptions(screen.getByLabelText("S3 backend"), "7");
  await user.selectOptions(screen.getByLabelText("Bucket"), "acme-wells");
  await user.type(screen.getByLabelText("Prefix (optional)"), "wells/");
  await next(user);
}

async function chooseAccess(user: UserEvent) {
  await screen.findByRole("heading", { name: "Who may use the migrated files?" });
  expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  await pickGroup(user, "Owners", "GG-Seismic-Owners");
  await pickGroup(user, "Writers", "GG-Seismic-Writers");
  await pickGroup(user, "Readers", "GG-Geoscience-All");
  await next(user);
}

describe("new migration", () => {
  it("lists the kinds of source, Annex handing over to Create an Annex", async () => {
    const { user, container } = renderApp("/migrations/new");
    await screen.findByRole("heading", { name: "Where is the data now?" });
    expect(screen.getByRole("radio", { name: /An Amazon S3 bucket/ })).toBeChecked();
    const annex = screen.getByRole("radio", { name: /A NAS share \(Annex\)/ });
    expect(annex).toBeEnabled();
    expect(screen.queryByText("Coming soon")).toBeNull();
    // A backend that takes any bucket asks for its name.
    await user.selectOptions(screen.getByLabelText("S3 backend"), "8");
    expect(screen.getByRole("textbox", { name: "Bucket" })).toBeInTheDocument();
    // The inventory is one click down.
    expect(screen.getByText("How OmniFile finds the objects")).not.toBeVisible();
    await expectNoAxeViolations(container);
  });

  it("hands an Annex over to Create an Annex with the filer and folder", async () => {
    const { user, router } = renderApp("/migrations/new");
    await screen.findByRole("heading", { name: "Where is the data now?" });
    await user.click(screen.getByRole("radio", { name: /A NAS share \(Annex\)/ }));
    await screen.findByRole("option", { name: /ontap01/ });
    await user.selectOptions(screen.getByLabelText("NAS filer"), "41");
    await user.type(screen.getByLabelText("Folder on the share"), "long-term-storage");
    await user.click(screen.getByRole("link", { name: "Continue in Create an Annex" }));
    expect(router.state.location.pathname).toBe("/annexes/new");
    expect(router.state.location.search).toBe("?filer=41&path=long-term-storage");
    expect(await screen.findByRole("heading", { name: "Create an Annex" })).toBeInTheDocument();
  });

  it("registers the source, waits for its catalogue, then starts the migration", async () => {
    const { user, router, container } = renderApp("/migrations/new");
    await chooseSource(user);

    await screen.findByRole("heading", { name: "Which dataset does it go into?" });
    await user.selectOptions(screen.getByLabelText("Dataset"), "4");
    await next(user);
    await chooseAccess(user);

    await screen.findByRole("heading", { name: "How should it move?" });
    expect(screen.getByRole("radio", { name: /Azure Storage Mover/ })).toBeChecked();
    expect(screen.getByText("Nothing switches until every file is verified")).toBeInTheDocument();
    expect(screen.getByText("New writes go to Azure from the start")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /When I say/ }));
    await next(user);

    await screen.findByRole("heading", { name: "Check, then start" });
    expect(screen.getByText("AWS bills some of this")).toBeInTheDocument();
    expect(screen.getByText(/s3:\/\/acme-wells\/wells\//)).toBeInTheDocument();
    await expectNoAxeViolations(container);
    await user.click(screen.getByRole("button", { name: "Start migration" }));

    // The source stays in the URL, so the page resumes from it.
    await waitFor(() => expect(router.state.location.search).toMatch(/^\?source=\d+$/), WAIT);

    const [source] = posts("/v1/sources");
    expect(source?.body).toEqual({
      dataset_id: "4",
      backend_id: "7",
      container: "acme-wells",
      prefix: "wells/",
      path: "",
      inventory: null,
      groups: {
        owners: "S-1-5-21-3623811015-3361044348-30300820-1201",
        writers: "S-1-5-21-3623811015-3361044348-30300820-1202",
        readers: "S-1-5-21-3623811015-3361044348-30300820-1203",
      },
    });
    // Still being catalogued: the start goes to the worker's plan scheduler, one wave due
    // now, so it starts even if the page is closed (the browser starts nothing itself).
    expect(await screen.findByText("It starts by itself", {}, WAIT)).toBeInTheDocument();
    const [plan] = posts("/v1/migration-plans");
    const body = plan?.body as { dataset_id: string; waves: Record<string, unknown>[] };
    expect(body.dataset_id).toBe("4");
    expect(body.waves).toHaveLength(1);
    expect(body.waves[0]).toMatchObject({
      prefixes: ["wells/"],
      cutover: "manual",
      copier: "auto",
    });
    expect(posts("/v1/migrations")).toHaveLength(0);
    // Each call carries its own Idempotency-Key (portal-api.md 4.2).
    const keys = [source, plan].map((r) => r?.headers["idempotency-key"]);
    expect(keys.every((k) => typeof k === "string" && k.length >= 16)).toBe(true);
    expect(new Set(keys).size).toBe(2);
    // Once catalogued, the scheduler (played by the mock) starts the wave's migration.
    expect(await screen.findByText("Objects found", {}, WAIT)).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Follow it in its plan" }));
    const waves = await screen.findByRole("table", { name: "Waves" }, WAIT);
    expect(await within(waves).findByRole("link", { name: "wells/" }, WAIT)).toBeInTheDocument();
    expect(within(waves).getByText("Running")).toBeInTheDocument();
  });

  it("makes a new dataset first, and reuses it when the source is refused", async () => {
    const { user } = renderApp("/migrations/new");
    await screen.findByRole("heading", { name: "Where is the data now?" });
    await user.selectOptions(screen.getByLabelText("S3 backend"), "7");
    // Overlaps source 9 (raw/2021/) of the same bucket.
    await user.selectOptions(screen.getByLabelText("Bucket"), "acme-seismic");
    await user.type(screen.getByLabelText("Prefix (optional)"), "raw/");
    await next(user);
    await user.click(await screen.findByRole("radio", { name: /A new dataset/ }));
    await user.type(screen.getByLabelText("Dataset name"), "seismic-legacy");
    await screen.findByRole("option", { name: "edge-houston-1" });
    await user.selectOptions(screen.getByLabelText("Which edge serves it?"), "1");
    await user.selectOptions(screen.getByLabelText("Azure storage"), "1");
    await next(user);
    await chooseAccess(user);
    await next(user);
    expect(await screen.findByText(/The new dataset has no share yet/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start migration" }));

    expect(await screen.findByText("The source wasn't registered")).toBeInTheDocument();
    expect(screen.getByText(/overlaps source 9/)).toBeInTheDocument();
    expect(screen.getByText(/seismic-legacy was created and is kept/)).toBeInTheDocument();
    expect(posts("/v1/datasets")).toHaveLength(1);

    // Fix the prefix and start again: the dataset is used, not made twice.
    for (let i = 0; i < 4; i++) await user.click(screen.getByRole("button", { name: "Back" }));
    const prefix = await screen.findByLabelText("Prefix (optional)");
    await user.clear(prefix);
    await user.type(prefix, "cooked/");
    for (let i = 0; i < 4; i++) await next(user);
    await user.click(await screen.findByRole("button", { name: "Start migration" }));
    await waitFor(() => expect(posts("/v1/sources")).toHaveLength(2));
    expect(posts("/v1/datasets")).toHaveLength(1);
    const datasetId = (posts("/v1/sources")[1]?.body as { dataset_id: string }).dataset_id;
    expect(datasetId).toBe((posts("/v1/sources")[0]?.body as { dataset_id: string }).dataset_id);
  });

  it("keeps the source when the migration doesn't start, and tries again", async () => {
    let refused = 0;
    server.use(
      http.post("*/v1/migrations", () => {
        if (refused++ > 0) return undefined;
        return Response.json(
          {
            type: "urn:omnifile:error:unavailable",
            title: "unavailable",
            status: 503,
            code: "unavailable",
            detail: "The worker queue is busy.",
            retryable: true,
            retry_after_s: null,
            request_id: "t",
          },
          { status: 503, headers: { "Content-Type": "application/problem+json" } },
        );
      }),
    );
    // No plan can be made: the page starts it itself once catalogued.
    server.use(
      http.post("*/v1/migration-plans", () =>
        Response.json(
          {
            type: "x",
            title: "unavailable",
            status: 503,
            code: "unavailable",
            detail: "busy",
            retryable: true,
            retry_after_s: null,
            request_id: "t",
          },
          { status: 503, headers: { "Content-Type": "application/problem+json" } },
        ),
      ),
    );
    const { user, router } = renderApp("/migrations/new");
    await chooseSource(user);
    await user.selectOptions(await screen.findByLabelText("Dataset"), "4");
    await next(user);
    await chooseAccess(user);
    await next(user);
    await user.click(await screen.findByRole("button", { name: "Start migration" }));

    expect(await screen.findByText("The migration didn't start", {}, WAIT)).toBeInTheDocument();
    expect(
      screen.getByText(/The source acme-wells\/wells\/ is registered and kept/),
    ).toBeInTheDocument();
    expect(router.state.location.search).toMatch(/^\?source=/);
    expect(posts("/v1/sources")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(
      () => expect(router.state.location.pathname).toMatch(/^\/migrations\/\d+$/),
      WAIT,
    );
    expect(posts("/v1/sources")).toHaveLength(1);
    // The refusal above answered before the mock's request log; the retry reached it.
    expect(refused).toBe(2);
    expect(posts("/v1/migrations")).toHaveLength(1);
  });

  it("resumes from a catalogued source on the Migrations page", async () => {
    const { user, router } = renderApp("/migrations");
    const sources = await screen.findByRole("table", { name: "Sources" });
    await user.click(
      within(sources).getByRole("link", { name: "Start the migration of acme-seismic/raw/2021/" }),
    );
    expect(await screen.findAllByText("4,200", {}, WAIT)).toHaveLength(2);
    // Nothing starts until the admin says so.
    expect(posts("/v1/migrations")).toHaveLength(0);
    await user.click(screen.getByRole("radio", { name: /An external copier, through the API/ }));
    await user.click(screen.getByRole("button", { name: "Start migration" }));
    await waitFor(
      () => expect(router.state.location.pathname).toMatch(/^\/migrations\/\d+$/),
      WAIT,
    );
    expect(posts("/v1/migrations")[0]?.body).toEqual({
      dataset_id: "1",
      prefix: "raw/2021/",
      cutover: "auto",
      copier: "external",
    });
  });

  it("offers to retry a source whose cataloguing failed", async () => {
    const { user } = renderApp("/migrations/new?source=4");
    expect(await screen.findByText("Cataloguing stopped")).toBeInTheDocument();
    expect(screen.getByText(/older than the bucket's event queue keeps/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start once catalogued/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Try cataloguing again" }));
    await waitFor(() => expect(posts("/v1/sources/4/retry")).toHaveLength(1));
    expect(await screen.findByText("Objects found", {}, WAIT)).toBeInTheDocument();
    expect(screen.queryByText("Cataloguing stopped")).not.toBeInTheDocument();
  });

  it("says when no S3 backend is set up, and never offers to make one", async () => {
    server.use(http.get("*/v1/source-backends", () => Response.json({ backends: [] })));
    renderApp("/migrations/new");
    expect(await screen.findByText("No S3 backend is set up")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("scans the source read-only, shows what it found, and fills the prefix", async () => {
    const { user, container } = renderApp("/migrations/new");
    await screen.findByRole("heading", { name: "Where is the data now?" });
    expect(screen.getByRole("button", { name: "Scan" })).toBeDisabled();
    expect(screen.getByText(/it never writes to the source, or anywhere else/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("S3 backend"), "7");
    await user.selectOptions(screen.getByLabelText("Bucket"), "acme-wells");
    await user.type(screen.getByLabelText("Prefix (optional)"), "wells/");
    await user.click(screen.getByRole("button", { name: "Scan" }));
    expect(posts("/v1/source-scans")[0]?.body).toEqual({
      backend_id: "7",
      bucket: "acme-wells",
      prefix: "wells/",
    });
    // Progress while the worker lists, then what it found.
    expect(await screen.findByText(/It stops at 2,000,000 objects/)).toBeInTheDocument();
    const prefixes = await screen.findByRole("table", { name: "Prefixes found" }, WAIT);
    expect(
      requestLog.filter((r) => r.path.startsWith("/v1/source-scans/")).length,
    ).toBeGreaterThanOrEqual(3);
    expect(within(prefixes).getByRole("row", { name: /wells\/2019\// })).toBeInTheDocument();
    const classes = screen.getByRole("table", { name: "Storage classes" });
    expect(within(classes).getByRole("row", { name: /DEEP_ARCHIVE/ })).toHaveTextContent(
      "Restore needed",
    );
    expect(within(classes).getByRole("row", { name: /^STANDARD / })).toHaveTextContent(
      "Copied as it is",
    );
    const estimate = screen.getByLabelText("Restore estimate");
    expect(estimate).toHaveTextContent("2 waves");
    expect(estimate).toHaveTextContent("Up to 48 h");
    expect(estimate).toHaveTextContent("$2,386.47");
    expect(screen.getByText("3 objects over 10 GiB")).toBeInTheDocument();
    expect(screen.getByText(/promises no restore or copy time/)).toBeInTheDocument();
    const waves = screen.getByRole("table", { name: "Suggested waves" });
    expect(within(waves).getAllByRole("row")).toHaveLength(4);
    expect(within(waves).getByText("Copy only")).toBeInTheDocument();
    await expectNoAxeViolations(container);

    await user.click(screen.getByRole("button", { name: "Use the prefix wells/2020/" }));
    expect(screen.getByLabelText("Prefix (optional)")).toHaveValue("wells/2020/");
    // The scan's summary comes with the review.
    await next(user);
    await user.selectOptions(await screen.findByLabelText("Dataset"), "4");
    await next(user);
    await chooseAccess(user);
    await next(user);
    await screen.findByRole("heading", { name: "Check, then start" });
    expect(screen.getByText(/need a restore, in 2 waves of up to 48 h/)).toBeInTheDocument();
  });

  it("scans the S3 Inventory the wizard names", async () => {
    const { user } = renderApp("/migrations/new");
    await screen.findByRole("heading", { name: "Where is the data now?" });
    await user.selectOptions(screen.getByLabelText("S3 backend"), "7");
    await user.selectOptions(screen.getByLabelText("Bucket"), "acme-seismic");
    await user.click(screen.getByText("More settings"));
    await user.click(screen.getByRole("radio", { name: /From an S3 Inventory report/ }));
    await user.type(screen.getByLabelText("Inventory bucket"), "acme-inventory");
    await user.type(screen.getByLabelText("Manifest key"), "acme-seismic/daily/manifest.json");
    await user.click(screen.getByRole("button", { name: "Scan" }));
    await waitFor(() => expect(posts("/v1/source-scans")).toHaveLength(1));
    expect(posts("/v1/source-scans")[0]?.body).toEqual({
      backend_id: "7",
      bucket: "acme-seismic",
      prefix: "",
      inventory: {
        kind: "s3_inventory",
        bucket: "acme-inventory",
        manifest_key: "acme-seismic/daily/manifest.json",
      },
    });
    expect(await screen.findByText(/from its S3 Inventory/, {}, WAIT)).toBeInTheDocument();
  });
});
