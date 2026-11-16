// @ts-check
// Browser tests against `npm run dev:mock` (the SPA over its MSW mock), for flows the M4
// stack can't run: it has no S3 backend for the New migration wizard to pick, and its worker
// can't catalogue one. By hand: npx playwright test -c playwright.mock.config.mjs
// The mock resets on every full page load, so a test navigates inside the SPA.
import { defineConfig, devices } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.MOCK_PORT ?? 5288);
const out = join(process.env.M4_WORK ?? join(tmpdir(), "omnifile-mock-e2e"), "playwright-mock");

export default defineConfig({
  testDir: ".",
  testMatch: "*.mock.mjs",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  outputDir: `${out}/results`,
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1280, height: 900 },
  },
  webServer: {
    command: `npm run dev:mock -- --port ${PORT} --strictPort`,
    cwd: "..",
    url: `http://localhost:${PORT}/`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
  // `chromium`: the full browser in new headless mode (`npx playwright install chromium`).
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], channel: "chromium" } }],
});
