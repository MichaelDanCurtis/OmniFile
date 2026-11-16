// @ts-check
// M4 browser tests (docs/milestones/M4.md "Exit"): chromium against the real `web` app and
// its dev OIDC issuer, as brought up by dev/m4/stack.py and seeded by tests/e2e/m4.py seed.
// tests/e2e/m4_exit_test.sh runs them; by hand: M4_WORK=<stack work dir> npx playwright test
import { defineConfig, devices } from "@playwright/test";
import { stack, workDir } from "./support.mjs";

const out = `${workDir()}/playwright`;

export default defineConfig({
  testDir: ".",
  // Not *.spec.*: vitest (the portal unit tests) would pick those up.
  testMatch: "*.e2e.mjs",
  // One stack, one catalog: the tests run in order, one at a time.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  outputDir: `${out}/results`,
  reporter: [["list"], ["json", { outputFile: `${out}/results.json` }]],
  use: {
    baseURL: stack().urls.web,
    // The stack's TLS certificate is from its own dev CA.
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1280, height: 900 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
