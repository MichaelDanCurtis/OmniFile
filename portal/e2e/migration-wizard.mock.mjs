// @ts-check
// The New migration wizard end to end in a browser, against the dev:mock API
// (portal-api.md 5.10, 5.17): an ArchiveAdmin picks an S3 bucket, a dataset, the three AD
// groups and a manual switch-over, starts it, watches the source catalogue itself and the
// migration walk its lifecycle, and cuts it over once every file is verified.
import { expect, test } from "@playwright/test";

/** @param {import("@playwright/test").Page} page @param {string} role @param {string} name */
async function pickGroup(page, role, name) {
  await page.getByLabel(`${role} group`).fill(name);
  await page
    .getByRole("list", { name: `Groups matching ${name}` })
    .getByRole("button", { name: new RegExp(name, "i") })
    .click();
}

test("an ArchiveAdmin starts a migration and cuts it over", async ({ page }) => {
  const posts = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && new URL(r.url()).pathname.startsWith("/v1/")) {
      posts.push({ path: new URL(r.url()).pathname, body: r.postDataJSON() });
    }
  });
  await page.goto("/migrations");
  await expect(page.getByRole("heading", { level: 1, name: "Migrations" })).toBeVisible();
  await page.getByRole("link", { name: "New migration" }).click();

  // 1. Source: Annex is offered too (it hands over to Create an Annex, Annex A1).
  await expect(page.getByRole("heading", { name: "Where is the data now?" })).toBeVisible();
  await expect(page.getByRole("radio", { name: /\(Annex\)/ })).toBeEnabled();
  await page.getByLabel("S3 backend", { exact: true }).selectOption("7");
  await page.getByLabel("Bucket", { exact: true }).selectOption("acme-wells");
  await page.getByLabel("Prefix (optional)", { exact: true }).fill("wells/");
  await page.getByRole("button", { name: "Next" }).click();

  // 2. Destination.
  await page.getByLabel("Dataset", { exact: true }).selectOption("4");
  await page.getByRole("button", { name: "Next" }).click();

  // 3. Access: all three groups.
  await pickGroup(page, "Owners", "GG-Seismic-Owners");
  await pickGroup(page, "Writers", "GG-Seismic-Writers");
  await pickGroup(page, "Readers", "GG-Data-Readers");
  await page.getByRole("button", { name: "Next" }).click();

  // 4. How: Storage Mover by default; switch over by hand.
  await expect(page.getByRole("radio", { name: /Azure Storage Mover/ })).toBeChecked();
  await page.getByRole("radio", { name: /When I say/ }).check();
  await page.getByRole("button", { name: "Next" }).click();

  // 5. Review, then start.
  await expect(page.getByText("s3://acme-wells/wells/")).toBeVisible();
  await expect(page.getByText("AWS bills some of this")).toBeVisible();
  await page.getByRole("button", { name: "Start migration" }).click();

  // Still being catalogued: the start goes to the plan scheduler, so it starts by itself
  // once the source is catalogued, even with the page closed.
  await expect(page.getByText("It starts by itself")).toBeVisible({ timeout: 30_000 });
  expect(posts.map((p) => p.path)).toEqual(["/v1/sources", "/v1/migration-plans"]);
  expect(posts[1].body.waves[0]).toMatchObject({
    prefixes: ["wells/"],
    cutover: "manual",
    copier: "auto",
  });
  await expect(page.getByText("Objects found")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("link", { name: "Follow it in its plan" }).click();
  await page.getByRole("table", { name: "Waves" }).getByRole("link", { name: "wells/" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: /gravity-surveys: wells\// }),
  ).toBeVisible();

  // It walks the lifecycle and waits at Verified. The plan owns it: the admin cuts the
  // wave over on its plan, never on the migration's own page.
  await expect(page.getByText("Wave 1 of a staged plan")).toBeVisible();
  await expect(page.getByText("Every file verified ·", { exact: false })).toBeVisible({
    timeout: 30_000,
  });
  await page.getByRole("link", { name: "Open its plan" }).click();
  await page.getByRole("button", { name: "Cut over wave 1" }).click();
  await page.getByRole("table", { name: "Waves" }).getByRole("link", { name: "wells/" }).click();
  await expect(page.getByText("Done", { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(page.locator("body")).not.toContainText(/Prizmanyx/i);
});
