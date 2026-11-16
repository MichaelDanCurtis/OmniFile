// @ts-check
// WORM enable (decision 31) on a WORM-layout share: an ArchiveAdmin whose sign-in is too old
// is sent to sign in again and comes straight back, types the share name, and sees the lock
// deadline (M4.md "Exit").
import { expect, test } from "@playwright/test";
import { ageAuth, PEOPLE, seed, signIn } from "./support.mjs";

test("an ArchiveAdmin turns on WORM after re-authenticating", async ({ page }) => {
  const vault = seed().worm.vault;
  const path = `/shares/${vault.share_id}`;
  await signIn(page, "admin", path);
  // Signed in an hour ago, as far as the server is concerned.
  ageAuth("admin");
  await page.reload();

  await page.getByRole("button", { name: "Turn on WORM…" }).click();
  let dialog = page.getByRole("dialog", { name: `Turn on WORM for ${vault.name}` });
  await expect(dialog.getByText("Sign in again first")).toBeVisible();
  await dialog.getByRole("button", { name: "Sign in again to continue" }).click();
  // The dev issuer asks again (prompt=login), then back to the same step.
  await page.getByRole("link", { name: PEOPLE.admin }).click();
  await page.waitForURL((url) => url.pathname === path);
  dialog = page.getByRole("dialog", { name: `Turn on WORM for ${vault.name}` });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("Sign in again first")).toHaveCount(0);

  await dialog.getByLabel("Retention (days)").fill("7");
  await dialog.getByLabel("Test window (hours)").fill("2");
  await expect(dialog.getByText(/^It locks around /)).toBeVisible();
  const confirm = dialog.getByLabel(/^Type the share name/);
  const turnOn = dialog.getByRole("button", { name: "Turn on WORM" });
  await confirm.fill(vault.name.toUpperCase());
  await expect(turnOn).toBeDisabled();
  await confirm.fill(vault.name);
  await expect(turnOn).toBeEnabled();
  await turnOn.click();

  await expect(page.getByText(`WORM is on for ${vault.name}.`, { exact: false })).toBeVisible();
  const card = page.getByRole("region", { name: "WORM (write once, read many)" });
  await expect(card.getByText("In its test window")).toBeVisible();
  await expect(card.getByText("Locks")).toBeVisible();
  const share = await (await page.request.get(`/v1/shares/${vault.share_id}`)).json();
  expect(share.worm.enabled).toBe(true);
  expect(share.worm.mode).toBe("unlocked_test");
  expect(share.worm.lock_deadline).toBeTruthy();
});
