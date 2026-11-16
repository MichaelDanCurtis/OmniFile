// @ts-check
// The migrations console in a browser, against the dev:mock API (portal-api.md 5.22): the
// overview and a drill-down, an in-place fix, the agents view, and a wave moved on the
// schedule with the keyboard and saved. With CONSOLE_SHOTS=<dir>, it also saves the
// console's screenshots, light and dark (docs/milestones/results/wp12-console-*.png).
import { expect, test } from "@playwright/test";

const shots = process.env.CONSOLE_SHOTS;

/** @param {import("@playwright/test").Page} page @param {string} name */
async function shot(page, name) {
  if (!shots) return;
  await page.setViewportSize({ width: 1600, height: 1000 });
  for (const scheme of /** @type {const} */ (["light", "dark"])) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await page.waitForTimeout(150);
    await page.screenshot({ path: `${shots}/wp12-console-${name}-${scheme}.png`, fullPage: true });
  }
  await page.emulateMedia({ colorScheme: "light" });
}

test("an ArchiveAdmin works the migrations console", async ({ page }) => {
  const posts = [];
  page.on("request", (r) => {
    const path = new URL(r.url()).pathname;
    if ((r.method() === "POST" || r.method() === "PATCH") && path.startsWith("/v1/")) {
      posts.push({ method: r.method(), path, body: r.postDataJSON() });
    }
  });
  await page.goto("/migrations");
  const table = page.getByRole("table", { name: "Prefixes being moved" });
  await expect(table).toBeVisible();
  await expect(page.getByRole("region", { name: /Needs attention/ })).toBeVisible();
  await page.getByRole("button", { name: "Show who works on raw/2019/batch-b/" }).click();
  await expect(page.getByRole("table", { name: "Restore waves for migration 7" })).toBeVisible();
  await shot(page, "overview");

  const attention = page.getByRole("region", { name: /Needs attention/ });
  await attention.getByRole("button", { name: "Restore again" }).click();
  await expect.poll(() => posts.map((p) => p.path)).toContain("/v1/migrations/7/restore-again");

  await page.getByRole("link", { name: "Agents" }).first().click();
  await expect(page.getByRole("table", { name: "Storage Mover agents" })).toBeVisible();
  await shot(page, "agents");

  await page.getByRole("link", { name: "Migrations", exact: true }).first().click();
  await page.getByRole("link", { name: "Plans" }).first().click();
  await page.getByRole("link", { name: "Seismic raw, by year" }).click();
  const handle = page.getByRole("slider", { name: "Planned start of wave 4, 2022 onwards" });
  await handle.focus();
  await page.keyboard.press("Shift+ArrowRight");
  await expect(handle).toHaveAttribute("aria-valuetext", /not saved/);
  await shot(page, "schedule");
  await page.getByRole("button", { name: "Save" }).click();
  await expect.poll(() => posts.filter((p) => p.method === "PATCH").length).toBe(1);
  await expect(page.locator("body")).not.toContainText(/Prizmanyx/i);
});
