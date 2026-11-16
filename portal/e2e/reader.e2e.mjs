// @ts-check
// A Reader sees only the files their AD groups can read, recalls one ahead of time, and
// watches the four-step view (M4.md "Exit").
import { expect, test } from "@playwright/test";
import { advance, edgeFilled, seed, signIn } from "./support.mjs";

test("a Reader browses only what their AD groups allow", async ({ page }) => {
  const world = seed();
  await signIn(page, "reader", `/files/${world.share_id}/${world.root_id}`);
  const table = page.getByRole("table", { name: "Folder contents" });
  await expect(table.getByRole("link", { name: "survey" })).toBeVisible();
  // restricted/ is readable by the share's Owners only.
  await expect(table.getByText("restricted")).toHaveCount(0);

  await table.getByRole("link", { name: "survey" }).click();
  await expect(table.getByRole("button", { name: "line_4001.segy" })).toBeVisible();
  await expect(table.getByRole("button", { name: "notes.txt" })).toBeVisible();

  // The folder itself, by URL: not found, never "forbidden" (portal-api.md 3.2).
  const r = await page.request.get(`/v1/dirs/${world.restricted_id}/children`);
  expect(r.status()).toBe(404);
  const payroll = await page.request.get(`/v1/files/${world.files["payroll.csv"].file_id}`);
  expect(payroll.status()).toBe(404);
});

test("a Reader recalls a file ahead of time and watches the four steps", async ({ page }) => {
  const world = seed();
  const file = world.files["line_4001.segy"];
  await signIn(page, "reader", `/files/${world.share_id}/${world.survey_id}`);
  const table = page.getByRole("table", { name: "Folder contents" });
  await table.getByRole("button", { name: "line_4001.segy" }).click();
  await page.getByRole("button", { name: /Recall ahead of time/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Standard");
  await dialog.getByRole("button", { name: "Recall file" }).click();

  await page.waitForURL(/\/recalls\/\d+$/);
  const recallId = page.url().split("/").pop() ?? "";
  const steps = page.getByRole("list", { name: "Recall progress" });
  const step = (name) => steps.getByRole("listitem").filter({ hasText: name });
  await expect(step("Requested")).toBeVisible();
  // The worker starts the restore: step 2 is current.
  await expect(step("Rehydrating")).toHaveAttribute("data-status", "current", { timeout: 60_000 });
  // Standard rehydrate is "up to 15 h": move the virtual clock past it.
  await advance(16 * 3600);
  await expect(step("Downloading")).toHaveAttribute("data-status", "current", { timeout: 90_000 });
  // The edge fills the file and reports it.
  await edgeFilled(recallId, file.versions[file.versions.length - 1], file.size);
  await expect(step("Available")).toHaveAttribute("data-status", "done", { timeout: 60_000 });
  await expect(step("Requested")).toHaveAttribute("data-status", "done");
});
