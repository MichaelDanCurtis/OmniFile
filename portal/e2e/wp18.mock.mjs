// @ts-check
// WP18 in a browser, against the dev:mock API: the live Activity feed, the Add edge wizard,
// who sees prices and the rate editor, and the Filers flow. With WP18_SHOTS=<dir>, it also
// saves each one's screenshots, light and dark (docs/milestones/results/wp18/).
import { expect, test } from "@playwright/test";

const shots = process.env.WP18_SHOTS;

/** @param {import("@playwright/test").Page} page @param {string} name */
async function shot(page, name, { fullPage = true } = {}) {
  if (!shots) return;
  for (const scheme of /** @type {const} */ (["light", "dark"])) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await page.waitForTimeout(250);
    await page.screenshot({ path: `${shots}/wp18-${name}-${scheme}.png`, fullPage });
  }
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
}

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce" });
});

test("the live Activity feed streams, filters and pauses", async ({ page }) => {
  await page.goto("/activity");
  const feed = page.getByRole("list", { name: "Live events" });
  await expect(feed).toBeVisible();
  await expect(page.locator(".live-row[data-fresh]").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("list", { name: "Edges" })).toBeVisible();
  await shot(page, "activity-live", { fullPage: false });
  await page.getByRole("button", { name: "Heartbeat" }).click();
  await page.getByRole("button", { name: "Pause live feed" }).click();
  await expect(page.getByText("Paused")).toBeVisible();
  await shot(page, "activity-paused-filtered", { fullPage: false });
});

test("Add edge walks from a platform to the first heartbeat", async ({ page }) => {
  await page.goto("/edges");
  await page.getByRole("link", { name: "Add edge" }).click();
  await expect(page.getByRole("radio", { name: /AWS EC2/ })).toBeDisabled();
  await page.getByRole("radio", { name: /Hyper-V/ }).check();
  await shot(page, "edge-1-platform");
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByText("omnifile-edge_0.3.0.vhdx", { exact: true })).toBeVisible();
  await shot(page, "edge-2-image");
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByText("8 GiB, fixed (not dynamic)")).toBeVisible();
  await shot(page, "edge-3-requirements");
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Name").fill("edge-dallas-1");
  await page.getByRole("button", { name: "Create code" }).click();
  await expect(page.getByLabel("Enrolment code for edge-dallas-1")).toHaveValue(/^ofe1\./);
  await shot(page, "edge-4-enrol");
  await page.getByRole("button", { name: "I've given it the code" }).click();
  await expect(
    page.getByRole("heading", { name: "Waiting for edge-dallas-1 to enrol" }),
  ).toBeVisible();
  await shot(page, "edge-5-waiting");
  await expect(page.getByRole("heading", { name: "edge-dallas-1 is online" })).toBeVisible({
    timeout: 30_000,
  });
  await shot(page, "edge-5-online");
});

test("prices: who sees them, and the rates", async ({ page }) => {
  await page.goto("/settings/prices-alerts");
  await page.getByRole("radio", { name: "Admins only", exact: true }).check();
  const table = page.getByRole("table", { name: "Azure Blob rates" });
  await table.getByLabel("Hot per gb-month").fill("0.015");
  await table.getByLabel("Archive retrieval per gb").fill("0.02");
  await expect(table.getByText("list 0.0184")).toBeVisible();
  await shot(page, "prices-editing");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(/Who sees prices is saved/)).toBeVisible();
  await page.getByRole("button", { name: "Save rates" }).first().click();
  await expect(page.getByText("Your rates")).toBeVisible();
  await shot(page, "prices-saved");
  // As a Reader with prices hidden from "everyone else": no costs on the files' recall choices.
  // Back to the defaults for the next run.
  await page.getByRole("radio", { name: "Everyone" }).check();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(/Who sees prices is saved/).first()).toBeVisible();
});

test("a DataSteward with prices hidden sees no costs", async ({ page }) => {
  await page.goto("/settings/prices-alerts");
  await page.getByRole("radio", { name: "ArchiveAdmins only" }).check();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(/Who sees prices is saved/)).toBeVisible();
  await page.evaluate(() => localStorage.setItem("omnifile.mockPersona", "steward"));
  await page.goto("/policies/1");
  await expect(page.getByText("An ArchiveAdmin has hidden prices from your role.")).toBeVisible();
  await shot(page, "prices-hidden-steward");
  await page.evaluate(() => {
    localStorage.setItem("omnifile.mockPersona", "admin");
    localStorage.removeItem("omnifile.mockPrices");
  });
});

test("Filers draws filer, edge, annex and Azure", async ({ page }) => {
  await page.goto("/settings/filers");
  await expect(page.getByRole("list", { name: "ontap01: what it needs" })).toBeVisible();
  await expect(page.getByRole("list", { name: "isilon02: what it needs" })).toBeVisible();
  await shot(page, "filers-flow");
  // With no edge online (the mock's dev knob): the steps lock and the page says to deploy one.
  await page.evaluate(() => localStorage.setItem("omnifile.mockNoEdges", "1"));
  await page.reload();
  await expect(page.getByText("Deploy an edge first.")).toBeVisible();
  await shot(page, "filers-no-edge");
  await page.evaluate(() => localStorage.removeItem("omnifile.mockNoEdges"));
});
