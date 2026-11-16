// @ts-check
// Shared by the M4 browser tests: the stack's facts (dev/m4/stack.py writes stack.json),
// the seeded world (tests/e2e/m4.py seed writes seed.json), sign-in through the dev
// issuer's user picker, and the two things a browser can't do itself: move the virtual
// clock (sim's /_sim/advance) and play the edge's fill (api, mutual TLS).
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { request as httpsRequest } from "node:https";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

export function workDir() {
  return process.env.M4_WORK ?? join(tmpdir(), "omnifile-m4");
}

/** @returns {any} */
export function stack() {
  return JSON.parse(readFileSync(join(workDir(), "stack.json"), "utf8"));
}

/** @returns {any} */
export function seed() {
  return JSON.parse(readFileSync(join(workDir(), "seed.json"), "utf8"));
}

/** The dev issuer's display names (dev/m4/world.py). */
export const PEOPLE = {
  admin: "Ada Admin",
  admin2: "Abe Admin",
  owner: "Olga Owner",
  reader: "Rita Reader",
  steward: "Sam Steward",
};

/**
 * Sign in as `who` and land on `path`: /auth/login, the dev issuer's picker, back.
 * @param {import("@playwright/test").Page} page
 * @param {keyof typeof PEOPLE} who
 * @param {string} path
 */
export async function signIn(page, who, path) {
  await page.goto(`/auth/login?return_to=${encodeURIComponent(path)}`);
  await page.getByRole("link", { name: PEOPLE[who] }).click();
  await page.waitForURL((url) => url.pathname === path);
}

/**
 * Make `who`'s sign-ins an hour old (tests/e2e/m4.py age-auth), so a WORM change needs a
 * fresh sign-in (portal-api.md 2.4).
 * @param {keyof typeof PEOPLE} who
 */
export function ageAuth(who) {
  const py = process.env.M4_SERVICE_PY ?? join(REPO, "service", ".venv", "bin", "python");
  execFileSync(py, [join(REPO, "tests", "e2e", "m4.py"), "age-auth", who], {
    env: { ...process.env, M4_WORK: workDir() },
    stdio: "pipe",
  });
}

/**
 * A POST with the signed-in page's session (CSRF token, Origin, Idempotency-Key), for
 * setting up what the UI under test doesn't offer.
 * @param {import("@playwright/test").Page} page @param {string} path @param {unknown} body
 */
export async function apiPost(page, path, body) {
  const session = await (await page.request.get("/auth/session")).json();
  return page.request.post(path, {
    data: body,
    headers: {
      "X-CSRF-Token": session.csrf_token,
      Origin: stack().urls.web,
      "Idempotency-Key": `e2e-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    },
  });
}

/** Move the shared virtual clock (sim, api, worker). @param {number} seconds */
export async function advance(seconds) {
  const r = await fetch(`${stack().urls.sim}/_sim/advance`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ seconds }),
  });
  if (!r.ok) throw new Error(`advance: ${r.status}`);
}

/**
 * The edge's `POST .../recalls/{id}/filled`, over mutual TLS with the test edge's
 * certificate (tests/e2e/m4.py seed issues it).
 * @param {string} recallId @param {string|number} versionId @param {number} size
 */
export function edgeFilled(recallId, versionId, size) {
  const s = stack();
  const edgeId = seed().edge_id;
  const url = new URL(`${s.urls.api}/v0/edges/${edgeId}/recalls/${recallId}/filled`);
  const body = JSON.stringify({ version_id: String(versionId), resident_bytes: size });
  return new Promise((resolve, reject) => {
    const req = httpsRequest(
      {
        hostname: url.hostname,
        port: url.port,
        path: url.pathname,
        method: "POST",
        ca: readFileSync(s.pki.ca_crt),
        cert: readFileSync(join(workDir(), "pki", "edge.crt")),
        key: readFileSync(join(workDir(), "pki", "edge.key")),
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(body),
          "idempotency-key": `e2e-filled-${recallId}-${Date.now()}`,
        },
      },
      (res) => {
        res.resume();
        res.on("end", () =>
          res.statusCode === 204
            ? resolve(undefined)
            : reject(new Error(`filled: ${res.statusCode}`)),
        );
      },
    );
    req.on("error", reject);
    req.end(body);
  });
}
