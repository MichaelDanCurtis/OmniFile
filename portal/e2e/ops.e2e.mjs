// @ts-check
// The M6 operations pages: an ArchiveAdmin reads the Activity dashboard and the Migrations
// page from the real catalog; a Reader is shown neither (portal-api.md 5.10 and 5.13).
import { expect, test } from "@playwright/test";
import { signIn } from "./support.mjs";

test("an ArchiveAdmin reads the activity dashboard from the catalog", async ({ page }) => {
  await signIn(page, "admin", "/files");
  await page.getByRole("link", { name: "Activity" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Activity" })).toBeVisible();

  // The page shows what the API says, for the default 30-day window.
  const api = await (await page.request.get("/v1/activity?days=30")).json();
  const uploads = page.getByRole("region", { name: "Uploads" });
  const value = (term) =>
    uploads.locator("dt", { hasText: term }).locator("xpath=following-sibling::dd[1]");
  await expect(value("Files committed")).toHaveText(String(api.uploads.files));
  await expect(value("Waiting for read-back")).toHaveText(String(api.uploads.unverified));
  const recalls = page.getByRole("region", { name: "Recalls" });
  await expect(
    recalls.locator("dt", { hasText: "Open now" }).locator("xpath=following-sibling::dd[1]"),
  ).toHaveText(String(api.recalls.open));

  // One table row per day, newest first, and a 7-day window narrows it.
  const rows = page.getByRole("table", { name: "Daily activity" }).locator("tbody tr");
  await expect(rows).toHaveCount(30);
  await page.getByLabel("Window").selectOption("7");
  await expect(rows).toHaveCount(7);
});

test("an ArchiveAdmin sees sources and moving prefixes on the Migrations page", async ({
  page,
}) => {
  await signIn(page, "admin", "/files");
  await page.getByRole("link", { name: "Migrations" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Migrations" })).toBeVisible();

  const sources = (await (await page.request.get("/v1/sources")).json()).items;
  const moving = (await (await page.request.get("/v1/locations")).json()).items;
  if (moving.length === 0) {
    await expect(page.getByText("Nothing is being moved")).toBeVisible();
  } else {
    await expect(
      page.getByRole("table", { name: "Prefixes being moved" }).locator("tbody tr"),
    ).toHaveCount(moving.length);
  }
  if (sources.length === 0) {
    await expect(page.getByText("No sources registered")).toBeVisible();
  } else {
    await expect(page.getByRole("table", { name: "Sources" }).locator("tbody tr")).toHaveCount(
      sources.length,
    );
  }
});

test("a Reader has neither page", async ({ page }) => {
  await signIn(page, "reader", "/files");
  await expect(page.getByRole("link", { name: "Activity" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Migrations" })).toHaveCount(0);
  expect((await page.request.get("/v1/activity")).status()).toBe(403);
  expect((await page.request.get("/v1/locations")).status()).toBe(403);
  await page.goto("/activity");
  await expect(page.getByText("Activity is for ArchiveAdmin.")).toBeVisible();
});
