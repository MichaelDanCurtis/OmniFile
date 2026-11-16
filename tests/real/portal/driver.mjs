// @ts-check
// The portal side of the real-cloud runs (tests/real/harness/portal.py), with Playwright from
// portal/e2e (NODE_PATH points at its node_modules).
//
//   node tests/real/portal/driver.mjs CONFIG.json
//
// CONFIG: {action, url, socks, state, evidence, params}. Prints one JSON line:
// {"ok": true, "facts": {...}, ...} or {"ok": false, "error": ...}.
//
// The portal is private: the browser goes through the SOCKS proxy the harness opened over
// the test VM (names resolve on the VM side). `sign-in` opens a headed browser at the portal
// and waits while the owner signs in with Entra themselves; the session is saved to
// CONFIG.state for the other actions, which run headless. `exit-worm-enable` is headed again:
// WORM asks for a fresh sign-in (prompt=login).
import { chromium } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";

const cfg = JSON.parse(readFileSync(process.argv[2], "utf8"));
const p = cfg.params ?? {};
const facts = p.facts ?? {};
const HEADED = new Set(["sign-in", "exit-worm-enable"]);

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

async function api(page, method, path, body) {
  const session = await (await page.request.get(`${cfg.url}/auth/session`)).json();
  const r = await page.request.fetch(`${cfg.url}${path}`, {
    method,
    data: body,
    headers: {
      "X-CSRF-Token": session.csrf_token ?? "",
      Origin: cfg.url,
      "Idempotency-Key": `ofreal-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    },
  });
  const text = await r.text();
  return { status: r.status(), body: text ? JSON.parse(text) : null };
}

async function until(what, fn, ms) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) throw new Error(`timed out: ${what}`);
    await new Promise((r) => setTimeout(r, 15_000));
  }
}

async function file(page, ds, path) {
  const r = await api(page, "GET", `/v1/files?dataset_id=${ds}&path=${encodeURIComponent(path)}`);
  if (r.status !== 200) throw new Error(`GET /v1/files ${path}: ${r.status}`);
  return r.body.entry ?? r.body;
}

const ACTIONS = {
  // The owner signs in; the harness never sees a password.
  "sign-in": async (page, ctx) => {
    await page.goto(`${cfg.url}/auth/login?return_to=/files`);
    await page.waitForURL((u) => u.origin === new URL(cfg.url).origin && u.pathname === "/files", {
      timeout: 15 * 60_000,
    });
    await ctx.storageState({ path: cfg.state });
    const me = await api(page, "GET", "/v1/me");
    writeFileSync(`${cfg.evidence}/portal-me.json`, JSON.stringify(me.body, null, 2));
    return { me: me.body?.display_name ?? null };
  },
  // A guide on=portal step: open, click, fill, choose, expect, as written. An unknown
  // <value:NAME> on an expect line is captured from the text that follows the label.
  guide: async (page, ctx) => {
    const captured = {};
    for (const raw of p.lines) {
      const line = raw.trim();
      if (!line) continue;
      const [verb, ...rest] = line.split(" ");
      const arg = rest.join(" ");
      if (verb === "open") await page.goto(`${cfg.url}${arg}`);
      else if (verb === "click") {
        await page.getByRole("button", { name: arg }).or(page.getByRole("link", { name: arg })).first().click();
        if (/^Copy/.test(arg)) {
          await ctx.grantPermissions(["clipboard-read", "clipboard-write"]);
          captured.__clipboard__ = (await page.evaluate(() => navigator.clipboard.readText())).trim();
        }
      }
      else if (verb === "fill" || verb === "choose") {
        const [label, value] = arg.split(" = ");
        const field = page.getByLabel(label);
        if (verb === "fill") await field.fill(value);
        else await field.selectOption({ label: value });
      } else if (verb === "expect") {
        const m = arg.match(/^(.*?)\s*<value:([A-Za-z0-9_.-]+)>\s*$/);
        if (m) {
          const el = page.getByText(m[1], { exact: false }).first();
          await el.waitFor({ timeout: 60_000 });
          const text = (await el.locator("xpath=..").textContent()) ?? "";
          captured[m[2]] = text.split(m[1]).pop().trim().split(/\s+/)[0];
        } else await page.getByText(arg, { exact: false }).first().waitFor({ timeout: 60_000 });
      }
    }
    return { captured };
  },
  "exit-edge-enrolled": async (page) => {
    const edges = await until("an edge reporting", async () => {
      const r = await api(page, "GET", "/v1/edges");
      const ok = (r.body?.items ?? []).filter((e) => e.status === "healthy" || e.status === "ok");
      return ok.length ? ok : null;
    }, 30 * 60_000);
    return { edges: edges.map((e) => ({ id: e.edge_id, status: e.status })) };
  },
  "exit-uploaded": async (page) => {
    const ds = (await api(page, "GET", "/v1/datasets")).body.items[0].dataset_id;
    const ids = {};
    for (const proto of ["nfs3", "nfs4", "smb"]) {
      const e = await until(`${proto} file committed`, async () => {
        const f = await file(page, ds, facts[`${proto}_path`]);
        return f.state === "committed" ? f : null;
      }, 60 * 60_000);
      ids[proto] = e.file_id;
    }
    return { facts: { file_ids: ids, dataset_id: ds } };
  },
  "exit-pin-archive": async (page) => {
    const id = facts.file_ids.nfs3;
    const until_ = new Date(Date.now() + 2 * 86400_000).toISOString();
    const r = await api(page, "POST", "/v1/pins", {
      items: [id], tier: "archive", until: until_, reason: "OmniFile real-cloud M6 exit",
    });
    if (r.status >= 300) throw new Error(`POST /v1/pins: ${r.status} ${JSON.stringify(r.body)}`);
    return { facts: { pinned_file: id } };
  },
  "exit-recall-high": async (page) => {
    const id = facts.pinned_file;
    const opts = (await api(page, "GET", `/v1/files/${id}/recall-options`)).body;
    const offered = (opts.options ?? []).filter((o) => o.offered).map((o) => o.priority);
    const priority = offered.includes("high") ? "high" : offered[0];
    const r = await api(page, "POST", "/v1/recalls", { file_id: id, priority });
    if (r.status >= 300) throw new Error(`POST /v1/recalls: ${r.status}`);
    return { facts: { recall_id: r.body.recall_id, priority }, offered };
  },
  "exit-worm-share": async (page) => {
    const backends = (await api(page, "GET", "/v1/backends")).body.items;
    const worm = backends.find((b) => b.layout === "worm");
    if (!worm) throw new Error("no WORM backend");
    const ds = await api(page, "POST", "/v1/datasets", { name: "tst", backend_id: worm.backend_id });
    const share = await api(page, "POST", "/v1/shares", {
      dataset_id: ds.body.dataset_id, name: "tst", protocols: ["nfs", "smb"],
    });
    if (share.status >= 300) throw new Error(`POST /v1/shares: ${share.status}`);
    return { facts: { worm_share_id: share.body.share_id, worm_share: "tst" } };
  },
  // Headed: the dialog sends the owner to sign in again (prompt=login, max_age=0).
  "exit-worm-enable": async (page, ctx) => {
    const path = `/shares/${facts.worm_share_id}`;
    await page.goto(`${cfg.url}${path}`);
    await page.getByRole("button", { name: "Turn on WORM…" }).click();
    let dialog = page.getByRole("dialog");
    const again = dialog.getByRole("button", { name: "Sign in again to continue" });
    if (await again.count()) {
      await again.click();
      await page.waitForURL((u) => u.pathname === path, { timeout: 15 * 60_000 });
      await page.getByRole("button", { name: "Turn on WORM…" }).click();
      dialog = page.getByRole("dialog");
    }
    await dialog.getByLabel("Retention (days)").fill("1");
    // A test window longer than the run, so the service never locks the policy before
    // teardown (a locked policy can't be removed until it expires).
    await dialog.getByLabel("Test window (hours)").fill(String(p.test_window_hours ?? 72));
    await dialog.getByLabel(/^Type the share name/).fill("tst");
    await dialog.getByRole("button", { name: "Turn on WORM" }).click();
    await page.getByText("WORM is on for tst.", { exact: false }).waitFor();
    await ctx.storageState({ path: cfg.state });
    const s = (await api(page, "GET", `/v1/shares/${facts.worm_share_id}`)).body;
    if (!s.worm?.enabled || s.worm.mode !== "unlocked_test") throw new Error("WORM not on");
    return { worm: s.worm };
  },
  "exit-worm-retained": async (page) => {
    const f = await file(page, facts.worm_dataset_id ?? facts.dataset_id, facts.worm_path);
    const v = (await api(page, "GET", `/v1/files/${f.file_id}/versions`)).body.items;
    if (v.length < 2) throw new Error(`want 2 retained versions, got ${v.length}`);
    return { versions: v.map((x) => ({ id: x.version_id, retention_until: x.retention_until })) };
  },
  "exit-policy-cost": async (page) => {
    const ds = (await api(page, "GET", "/v1/datasets")).body.items[0].dataset_id;
    await page.goto(`${cfg.url}/policies/${ds}`);
    const cost = page.getByTestId("cost-per-tb");
    await cost.waitFor();
    const before = await cost.textContent();
    await page.getByLabel("Archive after").fill("240");
    await until("the cost to change", async () => (await cost.textContent()) !== before, 60_000);
    const me = await api(page, "GET", "/v1/me");
    writeFileSync(`${cfg.evidence}/portal-me-tenant.json`, JSON.stringify(me.body, null, 2));
    return { detail: `cost ${before} -> ${await cost.textContent()}`, me: me.body };
  },
};

const fn = ACTIONS[cfg.action];
if (!fn) {
  out({ ok: false, error: `no action ${cfg.action}` });
  process.exit(2);
}
const browser = await chromium.launch({
  headless: !HEADED.has(cfg.action),
  proxy: cfg.socks ? { server: cfg.socks } : undefined,
});
const ctx = await browser.newContext({
  storageState: cfg.action === "sign-in" ? undefined : cfg.state,
  ignoreHTTPSErrors: false,
});
const page = await ctx.newPage();
try {
  const got = await fn(page, ctx);
  out({ ok: true, action: cfg.action, ...got });
} catch (e) {
  await page.screenshot({ path: `${cfg.evidence}/portal-${cfg.action}-failed.png` }).catch(() => {});
  out({ ok: false, action: cfg.action, error: String(e) });
  process.exitCode = 1;
} finally {
  await browser.close();
}
