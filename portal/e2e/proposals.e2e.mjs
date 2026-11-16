// @ts-check
// Proposals (anti-pattern 7): a DataSteward proposes a policy change in the editor and an
// ArchiveAdmin approves it; an ArchiveAdmin's own proposal needs a second ArchiveAdmin, and
// its proposer is not offered Approve (M4.md "Exit").
import { expect, test } from "@playwright/test";
import { apiPost, seed, signIn } from "./support.mjs";

/** @param {import("@playwright/test").Page} page */
async function activePolicy(page, datasetId) {
  const r = await page.request.get(`/v1/datasets/${datasetId}/policy`);
  expect(r.status()).toBe(200);
  return r.json();
}

/** @param {import("@playwright/test").Locator} approve */
async function notOffered(approve) {
  // Absent, or present but disabled: either way the proposer can't approve.
  if ((await approve.count()) > 0) await expect(approve).toBeDisabled();
}

test("a DataSteward proposes a change and an ArchiveAdmin approves it", async ({ page }) => {
  const world = seed();
  await signIn(page, "steward", `/policies/${world.dataset_id}`);
  // exact: the timeline slider is labelled "Move to Archive after, in days" (as in policy.e2e.mjs).
  await page.getByLabel("Archive after", { exact: true }).fill("300");
  await expect(page.getByTestId("change-summary")).toContainText("300");
  // A DataSteward can't save, only propose.
  await expect(page.getByRole("button", { name: /^Save as version/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Propose this change…" }).click();
  const dialog = page.getByRole("dialog", { name: "Propose this policy" });
  await dialog.getByLabel("Why this change?").fill("e2e: archive a little later");
  await dialog.getByRole("button", { name: "Send proposal" }).click();
  await expect(page.getByText("Proposal sent to the ArchiveAdmins.")).toBeVisible();

  const list = await page.request.get(
    `/v1/policies/proposals?dataset_id=${world.dataset_id}&state=pending`,
  );
  const mine = (await list.json()).items.find(
    (/** @type {any} */ p) => p.rationale === "e2e: archive a little later",
  );
  expect(mine).toBeTruthy();
  const before = await activePolicy(page, world.dataset_id);
  expect(before.body.archive_day).not.toBe(300);

  // The proposer: no Approve; it waits for an ArchiveAdmin.
  await page.goto(`/proposals/${mine.proposal_id}`);
  await expect(page.getByText("Only an ArchiveAdmin can approve or reject this.")).toBeVisible();
  await notOffered(page.getByRole("button", { name: "Approve and make it active" }));

  await page.context().clearCookies();
  await signIn(page, "admin", `/proposals/${mine.proposal_id}`);
  await page.getByRole("button", { name: "Approve and make it active" }).click();
  await expect(page.getByText(/^Approved\. Version \d+ is now active\.$/)).toBeVisible();
  const after = await activePolicy(page, world.dataset_id);
  expect(after.body.archive_day).toBe(300);
  expect(after.proposal_id).toBe(mine.proposal_id);
});

test("an ArchiveAdmin's own proposal needs a second ArchiveAdmin", async ({ page }) => {
  const world = seed();
  await signIn(page, "admin", "/proposals");
  const active = await activePolicy(page, world.dataset_id);
  const r = await apiPost(page, "/v1/policies/proposals", {
    dataset_id: String(world.dataset_id),
    body: { ...active.body, archive_day: 330 },
    rationale: "e2e: my own proposal",
    base_version: active.version,
  });
  expect(r.status()).toBe(201);
  const proposal = await r.json();

  await page.goto(`/proposals/${proposal.proposal_id}`);
  await expect(page.getByRole("heading", { name: "Decide" })).toBeVisible();
  await notOffered(page.getByRole("button", { name: "Approve and make it active" }));
  expect((await activePolicy(page, world.dataset_id)).version).toBe(active.version);

  await page.context().clearCookies();
  await signIn(page, "admin2", `/proposals/${proposal.proposal_id}`);
  await page.getByRole("button", { name: "Approve and make it active" }).click();
  await expect(page.getByText(/^Approved\. Version \d+ is now active\.$/)).toBeVisible();
  const after = await activePolicy(page, world.dataset_id);
  expect(after.body.archive_day).toBe(330);
  expect(after.proposal_id).toBe(proposal.proposal_id);
});
