// @ts-check
// An ArchiveAdmin edits a policy and sees the timeline, the first-year cost per TB and the
// checks update before saving; the saved version is active (M4.md "Exit").
import { expect, test } from "@playwright/test";
import { seed, signIn } from "./support.mjs";

test("an ArchiveAdmin edits a policy, sees the cost update, and saves it", async ({ page }) => {
  const world = seed();
  await signIn(page, "admin", `/policies/${world.dataset_id}`);
  await expect(page.getByRole("heading", { name: /^Policy for / })).toBeVisible();

  const cost = page.getByTestId("cost-per-tb");
  await expect(cost).toHaveText(/\d/);
  const before = await cost.textContent();
  const timeline = page.getByRole("region", { name: "A file's life under this policy" });
  await expect(timeline).toBeVisible();
  const timelineBefore = await timeline.textContent();

  const checks = page.getByRole("region", { name: "Checks" });
  const save = page.getByRole("button", { name: /^Save as version \d+$/ });

  // Archive at day 90, before the Cold day (120): the checks say so, and it can't be saved.
  // exact: the timeline slider is labelled "Move to Archive after, in days".
  const archive = page.getByLabel("Archive after", { exact: true });
  await archive.fill("90");
  await expect(checks.getByText(/to fix/)).toBeVisible();
  await expect(save).toBeDisabled();

  // Day 240: valid; the preview (debounced, server-side) redraws the cost and timeline.
  await archive.fill("240");
  await expect(checks.getByText(/to fix/)).toHaveCount(0);
  await expect(cost).not.toHaveText(before ?? "");
  await expect(timeline).not.toHaveText(timelineBefore ?? "");
  await expect(page.getByTestId("change-summary")).toContainText("240");
  await expect(save).toBeEnabled();
  const version = Number((await save.textContent())?.match(/\d+/)?.[0]);
  await save.click();
  await expect(page.getByText(`Version ${version} is active.`)).toBeVisible();

  // The server agrees: that version is the dataset's active policy, archiving at day 240.
  const active = await page.request.get(`/v1/datasets/${world.dataset_id}/policy`);
  expect(active.status()).toBe(200);
  const body = await active.json();
  expect(body.version).toBe(version);
  expect(body.state).toBe("active");
  expect(body.body.archive_day).toBe(240);
});
