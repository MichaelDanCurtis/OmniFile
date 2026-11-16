import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { browserNav } from "../../api/client";
import { getAnnexMock } from "../../mocks/annexHandlers";
import { requestLog } from "../../mocks/handlers";
import { server } from "../../mocks/server";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";
import { replaceSummary, type Placement } from "./returnsApi";
import { ruleDiff } from "./rulesApi";

/** The route's answer to an ArchiveAdmin who signed in over 10 minutes ago. */
const reauthRequired = () =>
  HttpResponse.json(
    {
      type: "urn:omnifile:error:reauth_required",
      title: "reauth required",
      status: 401,
      code: "reauth_required",
      detail: "Sign in again to do this.",
      retryable: false,
      retry_after_s: null,
      request_id: "test",
      max_age_s: 600,
    },
    { status: 401, headers: { "Content-Type": "application/problem+json" } },
  );

const calls = (method: string, path: RegExp) =>
  requestLog.filter((r) => r.method === method && path.test(r.path));

describe("annexes", () => {
  it("lists annexes and offers Create to ArchiveAdmin and DataSteward", async () => {
    const { container } = renderApp("/annexes", { persona: "steward" });
    const table = await screen.findByRole("table", { name: "Annexes" });
    expect(within(table).getByRole("link", { name: "long-term-storage" })).toBeInTheDocument();
    expect(within(table).getByText("Copying")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create an Annex" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows the annex: header, timeline, files with the NAS stop, and problems", async () => {
    const { container } = renderApp("/annexes/7", { persona: "admin" });
    expect(await screen.findByRole("heading", { name: "long-term-storage" })).toBeInTheDocument();
    expect(
      screen.getByText("\\\\ontap01.corp.example\\geo\\long-term-storage"),
    ).toBeInTheDocument();
    expect(screen.getByText("Read-only, readable")).toBeInTheDocument();
    expect(screen.getByText(/14210 files/)).toBeInTheDocument();
    expect(
      await screen.findByRole("img", { name: /arrived, 1 left the NAS, 1 copied/ }),
    ).toBeInTheDocument();
    const files = await screen.findByRole("table", { name: "Annexed files" });
    const row = (path: string) => {
      const tr = within(files).getByRole("rowheader", { name: path }).closest("tr");
      if (!tr) throw new Error(path);
      return tr;
    };
    expect(within(row("dataset/line_1042.segy")).getByText("Verified")).toBeInTheDocument();
    expect(within(row("dataset/line_1042.segy")).getByText("NAS")).toBeInTheDocument();
    expect(within(row("dataset/line_1042.segy")).getByText("Archive")).toBeInTheDocument();
    expect(within(row("dataset/new.segy")).getByText("Waiting")).toBeInTheDocument();
    expect(within(row("dataset/old.segy")).getByText("Deleted on the NAS")).toBeInTheDocument();
    const problems = await screen.findByRole("list", { name: "Annex problems" });
    expect(within(problems).getByText("A change the scans missed")).toBeInTheDocument();
    expect(within(problems).getByText("Skipped")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("lets an ArchiveAdmin stop re-copying a refused copy, resume it, and quiet its alert", async () => {
    const { user, container } = renderApp("/annexes/7", { persona: "admin" });
    const problems = await screen.findByRole("list", { name: "Annex problems" });
    expect(within(problems).getByText("Azure refused the copy")).toBeInTheDocument();
    expect(within(problems).getByText(/refused it 3 times in a row/)).toBeInTheDocument();
    await user.click(
      within(problems).getByRole("button", { name: "Stop re-copying dataset/new.segy" }),
    );
    await waitFor(() => expect(calls("POST", /\/files\/103\/copy-stop$/)).toHaveLength(1));
    expect(
      await within(problems).findByText("Copying stopped: Azure refused the copy"),
    ).toBeInTheDocument();
    expect(
      within(problems).getByText(/isn't uploaded again until a copy verifies/),
    ).toBeInTheDocument();
    await user.click(
      within(problems).getByRole("button", { name: "Acknowledge the alert for dataset/new.segy" }),
    );
    await waitFor(() => expect(calls("POST", /\/files\/103\/copy-refused-ack$/)).toHaveLength(1));
    expect(await within(problems).findByText(/acknowledged the alert/)).toBeInTheDocument();
    expect(within(problems).queryByRole("button", { name: /Acknowledge the alert/ })).toBeNull();
    await user.click(
      within(problems).getByRole("button", { name: "Resume re-copying dataset/new.segy" }),
    );
    await waitFor(() => expect(calls("POST", /\/files\/103\/copy-resume$/)).toHaveLength(1));
    expect(await within(problems).findByText("Azure refused the copy")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("offers the acknowledgement for a copy stopped below the bound, and says a list is cut short", async () => {
    // Review of #93: stopped at its first refusal, the alert is up for it too.
    renderApp("/annexes/7", { persona: "admin" });
    const mock = getAnnexMock();
    mock.refused.set("7", [
      {
        file_id: "103",
        path: "dataset/new.segy",
        at_bound: false,
        alerting: true,
        copy_refusals: 1,
        copy_refused_at: new Date().toISOString(),
        copy_stopped: { by: "user:admin", at: new Date().toISOString() },
        copy_refused_ack: null,
      },
    ]);
    mock.refusedTotal.set("7", 250);
    const problems = await screen.findByRole("list", { name: "Annex problems" });
    expect(
      within(problems).getByRole("button", { name: "Acknowledge the alert for dataset/new.segy" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Showing 1 of 250 refused copies/)).toBeInTheDocument();
  });

  it("asks an ArchiveAdmin to sign in again to stop re-copying", async () => {
    const { user } = renderApp("/annexes/7", { persona: "admin" });
    server.use(http.post("*/v1/annexes/7/files/103/copy-stop", reauthRequired));
    const problems = await screen.findByRole("list", { name: "Annex problems" });
    await user.click(
      within(problems).getByRole("button", { name: "Stop re-copying dataset/new.segy" }),
    );
    expect(await screen.findByRole("button", { name: /Sign in again/ })).toBeInTheDocument();
  });

  it("shows others a refused copy without the buttons", async () => {
    renderApp("/annexes/7", { persona: "steward" });
    const problems = await screen.findByRole("list", { name: "Annex problems" });
    expect(within(problems).getByText("Azure refused the copy")).toBeInTheDocument();
    expect(within(problems).queryByRole("button", { name: /re-copying/ })).toBeNull();
  });

  it("pauses copying with If-Match", async () => {
    const { user } = renderApp("/annexes/7", { persona: "admin" });
    await user.click(await screen.findByRole("button", { name: "Pause copying" }));
    await waitFor(() => expect(calls("PATCH", /^\/v1\/annexes\/7$/)).toHaveLength(1));
    const [patch] = calls("PATCH", /^\/v1\/annexes\/7$/);
    expect(patch?.headers["if-match"]).toBe('"4"');
    expect(patch?.body).toEqual({ state: "paused" });
    expect(await screen.findByRole("button", { name: "Resume copying" })).toBeInTheDocument();
  });

  it("lets an ArchiveAdmin purge the kept copy of a file deleted on the NAS", async () => {
    const { user } = renderApp("/annexes/7", { persona: "admin" });
    const purge = await screen.findByRole("button", { name: "Purge copy of dataset/old.segy" });
    expect(screen.queryByRole("button", { name: "Purge copy of dataset/notes.txt" })).toBeNull();
    await user.click(purge);
    // Asked first: the copy is the last one.
    const dialog = await screen.findByRole("dialog", { name: "Purge the kept copy?" });
    expect(calls("POST", /\/files\/104\/purge$/)).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Keep it" }));
    expect(calls("POST", /\/files\/104\/purge$/)).toHaveLength(0);
    await user.click(purge);
    await user.click(
      within(await screen.findByRole("dialog", { name: "Purge the kept copy?" })).getByRole(
        "button",
        { name: "Purge the copy" },
      ),
    );
    await waitFor(() => expect(calls("POST", /\/files\/104\/purge$/)).toHaveLength(1));
    expect(await screen.findByText("Purging")).toBeInTheDocument();
  });

  it("says the files are hidden until their folders are read, and which can't be", async () => {
    // Decision 63: renderApp resets the mock, so the read set is set after it.
    renderApp("/annexes/7", { persona: "admin" });
    getAnnexMock().readSets.set("7", {
      ready: false,
      filer_answered: true,
      read_at: null,
      folders: 3,
      unreadable: 0,
    });
    let problems = await screen.findByRole("list", { name: "Annex problems" });
    expect(
      await within(problems).findByText("Files are hidden until their folders are read"),
    ).toBeInTheDocument();
    expect(within(problems).getByText(/3 folders read so far/)).toBeInTheDocument();
    cleanup();
    renderApp("/annexes/7", { persona: "admin" });
    getAnnexMock().readSets.set("7", {
      ready: true,
      filer_answered: true,
      read_at: null,
      folders: 9,
      unreadable: 2,
    });
    problems = await screen.findByRole("list", { name: "Annex problems" });
    expect(
      await within(problems).findByText("Folders whose permissions can't be read"),
    ).toBeInTheDocument();
    expect(within(problems).getByText(/2 folders: the files below them/)).toBeInTheDocument();
  });

  it("says which folders above a file narrow who may read it", async () => {
    renderApp("/annexes/7", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Annexed files" });
    expect(
      await within(table).findByText(
        /Only for those this folder let through: long-term-storage\/dataset/,
      ),
    ).toBeInTheDocument();
  });

  it("an ArchiveAdmin answers whether the share limits access, with who and when", async () => {
    // Decision 65.
    const { user } = renderApp("/annexes/7", { persona: "admin" });
    expect(await screen.findByText(/No: the files.* own permissions decide/)).toBeInTheDocument();
    expect(screen.getByText(/Confirmed by user:admin@corp.example/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm again" }));
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls("PATCH", /\/v1\/filers\/41$/)).toHaveLength(1));
  });

  it("flags a NAS that shows no ACLs until an ArchiveAdmin acknowledges it", async () => {
    // Decision 64.
    const { user } = renderApp("/annexes/7", { persona: "admin" });
    expect(await screen.findByText("Not shown: group-readable copies wait")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() =>
      expect(
        calls("PATCH", /\/v1\/filers\/41$/).some((c) =>
          JSON.stringify(c.body).includes("acls_hidden_acknowledged"),
        ),
      ).toBe(true),
    );
    expect(await screen.findByText("Not shown, acknowledged")).toBeInTheDocument();
  });

  it("offers a fresh sign-in when the acknowledgement needs one", async () => {
    // Codex on #84, the same for decision 64's Acknowledge.
    server.use(http.patch("*/v1/filers/:id", () => reauthRequired()));
    const { user } = renderApp("/annexes/7", { persona: "admin" });
    await user.click(await screen.findByRole("button", { name: "Acknowledge" }));
    await user.click(await screen.findByRole("button", { name: "Sign in again" }));
    expect(browserNav.assign).toHaveBeenCalledWith(expect.stringMatching(/reauth=1$/));
  });

  it("offers a fresh sign-in when the answer needs one", async () => {
    // Codex on #84: the route refuses an ArchiveAdmin who signed in over 10 minutes ago.
    server.use(http.patch("*/v1/filers/:id", () => reauthRequired()));
    const { user } = renderApp("/annexes/7", { persona: "admin" });
    await user.click(await screen.findByRole("button", { name: "Confirm again" }));
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await user.click(await screen.findByRole("button", { name: "Sign in again" }));
    expect(browserNav.assign).toHaveBeenCalledWith(expect.stringMatching(/reauth=1$/));
  });

  it("says the files are hidden while the filer's question is unanswered", async () => {
    renderApp("/annexes/7", { persona: "steward" });
    getAnnexMock().readSets.set("7", {
      ready: true,
      filer_answered: false,
      read_at: null,
      folders: 9,
      unreadable: 0,
    });
    const problems = await screen.findByRole("list", { name: "Annex problems" });
    expect(
      await within(problems).findByText("The filer's access question isn't answered"),
    ).toBeInTheDocument();
  });

  it("shows drift as counts to someone who isn't an ArchiveAdmin", async () => {
    renderApp("/annexes/7", { persona: "steward" });
    const problems = await screen.findByRole("list", { name: "Annex problems" });
    expect(within(problems).queryByText("dataset/notes.txt")).toBeNull();
  });

  it("approves a proposal by its version, showing its rules, changes and a fresh count", async () => {
    const { user } = renderApp("/annexes/7", { persona: "admin" });
    // renderApp resets the mock: propose it before the page's first fetch.
    const mock = getAnnexMock();
    const proposed = mock.annexes.find((a) => a.annex_id === "7");
    const [v1] = mock.rulesets.get("7") ?? [];
    if (!proposed || !v1) throw new Error("annex 7");
    proposed.ruleset_state = "proposed";
    mock.rulesets.set("7", [
      v1,
      {
        ...v1,
        version: 2,
        state: "proposed",
        mode: "custom",
        owner_identity_id: "12",
        owner_name: "Owen Owner",
        proposed_via: "api",
        proposed_by_app: "app:prizmanyx",
        decided_at: null,
        decided_by_identity_id: null,
        body: {
          rules: [
            {
              id: "scratch",
              trigger: "arrived",
              conditions: { path: ["**/scratch/*"] },
              actions: [{ exclude: true }],
            },
            ...(v1.body.rules as unknown[]),
          ],
        },
      } as typeof v1,
    ]);
    expect(
      await screen.findByRole("heading", { name: "Waiting for approval" }),
    ).toBeInTheDocument();
    // Never the unversioned approval: the page points at the proposal.
    expect(screen.queryByRole("button", { name: "Approve and start copying" })).toBeNull();
    const changes = await screen.findByRole("generic", { name: "Changes" });
    expect(within(changes).getByText("Adds rule scratch")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Rule flow").length).toBeGreaterThan(0);
    await waitFor(() => expect(calls("POST", /\/annexes\/7\/dry-runs$/)).toHaveLength(1));
    await user.click(await screen.findByRole("button", { name: "Approve version 2" }));
    await waitFor(() =>
      expect(calls("POST", /\/annexes\/7\/rulesets\/2\/approve$/)).toHaveLength(1),
    );
    expect(calls("POST", /\/ruleset\/approve$/)).toHaveLength(0);
  });

  it("creates a Copy annex: share, mode, dataset, owners, start", async () => {
    const { user, container, router } = renderApp("/annexes/new?filer=41&path=long-term-storage", {
      persona: "admin",
    });
    expect(await screen.findByRole("heading", { name: "Create an Annex" })).toBeInTheDocument();
    expect(screen.getByLabelText("Filer")).toHaveValue("41");
    expect(screen.getByLabelText("Folder on the share")).toHaveValue("long-term-storage");
    expect(screen.getByRole("radio", { name: /^Copy/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /^Move now/ })).toBeEnabled();
    const start = screen.getByRole("button", { name: "Start copying" });
    expect(start).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Dataset"), "new");
    await user.type(screen.getByLabelText("New dataset's name"), "geo-annex-2");
    await screen.findByRole("option", { name: "azure-weu" });
    await user.selectOptions(screen.getByLabelText("Storage"), "1");
    await user.type(screen.getByLabelText("Owners group"), "GG-Seismic-Owners");
    const results = await screen.findByRole("list", { name: "Groups matching GG-Seismic-Owners" });
    await user.click(await within(results).findByRole("button", { name: /GG-Seismic-Owners/ }));
    await user.type(screen.getByLabelText("Name of the annex"), "lts-2");
    await expectNoAxeViolations(container);
    await user.click(start);
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/annexes\/\d+$/));
    const [ds] = calls("POST", /^\/v1\/datasets$/);
    expect(ds?.body).toMatchObject({ name: "geo-annex-2", backend_id: "1", edge_id: "1" });
    const [annex] = calls("POST", /^\/v1\/annexes$/);
    expect(annex?.body).toMatchObject({
      name: "lts-2",
      filer_id: "41",
      path: "long-term-storage",
      mode: "copy",
    });
    expect(annex?.headers["idempotency-key"]).toBeTruthy();
  });

  it("registers a filer inline, NFS sec=sys labelled host-trusted", async () => {
    const { user } = renderApp("/annexes/new", { persona: "admin" });
    await user.click(await screen.findByRole("button", { name: "Add a filer" }));
    await user.type(screen.getByLabelText("Filer name"), "isilon01");
    await user.type(screen.getByLabelText("Host"), "isilon01.corp.example");
    await user.selectOptions(screen.getByLabelText("Protocol"), "nfs3");
    await user.type(screen.getByLabelText("Export path"), "/ifs/geo");
    await user.selectOptions(screen.getByLabelText("Kind of NAS"), "powerscale");
    await screen.findByRole("option", { name: "edge-houston-1" });
    await user.selectOptions(screen.getByLabelText("Edge next to it"), "1");
    await user.selectOptions(screen.getByLabelText("The edge signs in as"), "sec_sys");
    expect(screen.getByText(/Host-trusted: the NAS trusts the edge's address/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add filer" }));
    await waitFor(() => expect(calls("POST", /^\/v1\/filers$/)).toHaveLength(1));
    expect(calls("POST", /^\/v1\/filers$/)[0]?.body).toMatchObject({
      protocol: "nfs3",
      credential_mode: "sec_sys",
      vendor: "powerscale",
      event_source: "scan",
    });
    expect(await screen.findByText(/This filer is host-trusted/)).toBeInTheDocument();
  });

  it("lists the filers in Settings with how the edge signs in and their health", async () => {
    const { container } = renderApp("/settings/filers", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Filers" });
    const ontap = within(
      within(table).getByRole("rowheader", { name: "ontap01" }).closest("tr") as HTMLElement,
    );
    expect(ontap.getByText("NetApp ONTAP")).toBeInTheDocument();
    expect(ontap.getByText("AD computer account (Kerberos)")).toBeInTheDocument();
    expect(ontap.getByText("Mounted")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Filers" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("saves the annex defaults with If-Match", async () => {
    const { user } = renderApp("/settings/annex-defaults", { persona: "admin" });
    const quiet = await screen.findByLabelText("Quiet period before a file is copied (minutes)");
    expect(quiet).toHaveValue(10);
    const save = screen.getByRole("button", { name: "Save defaults" });
    expect(save).toBeDisabled();
    await user.clear(screen.getByLabelText("Full scan every (minutes)"));
    await user.type(screen.getByLabelText("Full scan every (minutes)"), "30");
    expect(screen.getByText(/full scans no more often than quick ones/)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Full scan every (minutes)"));
    await user.type(screen.getByLabelText("Full scan every (minutes)"), "10080");
    await user.clear(quiet);
    await user.type(quiet, "20");
    await user.click(save);
    await waitFor(() => expect(calls("PUT", /annex-defaults$/)).toHaveLength(1));
    const [put] = calls("PUT", /annex-defaults$/);
    expect(put?.headers["if-match"]).toBe('"1"');
    expect(put?.body).toEqual({
      quiet_minutes: 20,
      quick_scan_minutes: 60,
      full_scan_minutes: 10080,
      reconcile_days: 7,
    });
  });

  it("is not offered to a reader", async () => {
    renderApp("/annexes/new", { persona: "reader" });
    expect(
      await screen.findByText("Annexes are created by ArchiveAdmins and DataStewards."),
    ).toBeInTheDocument();
  });

  it("creates an Archive-after annex: copying starts, the removal waits for approval", async () => {
    const { user } = renderApp("/annexes/new?filer=41&path=long-term-storage", {
      persona: "admin",
    });
    await user.click(await screen.findByRole("radio", { name: /^Archive after N days/ }));
    const days = screen.getByLabelText("Days after arrival");
    await user.clear(days);
    await user.type(days, "45");
    expect(screen.getByText(/Removals wait for an ArchiveAdmin/)).toBeInTheDocument();
  });

  it("shows the running rules and a proposal an ArchiveAdmin approves", async () => {
    renderApp("/annexes/7", { persona: "steward" });
    const list = getAnnexMock().rulesets.get("7") ?? [];
    const base = list[0];
    if (!base) throw new Error("no running rule set");
    list.push({
      ...base,
      version: 2,
      state: "proposed",
      mode: "archive_after",
      removes: true,
      needs_archive_admin: true,
      rationale: "free the NAS",
      dry_run_id: null,
    });
    const rules = await screen.findByRole("region", { name: "Rules" });
    expect(await within(rules).findByText(/Version 1 is running/)).toBeInTheDocument();
    const proposals = await within(rules).findByRole("list", { name: "Proposals" });
    expect(within(proposals).getByText(/This removes files from the NAS/)).toBeInTheDocument();
    expect(within(proposals).getByText("An ArchiveAdmin approves this one.")).toBeInTheDocument();
    expect(within(proposals).queryByRole("button", { name: "Approve version 2" })).toBeNull();
    expect(screen.getByRole("region", { name: "Removals" })).toHaveTextContent(
      /Nothing is removed from the NAS now/,
    );
  });

  it("marks a proposal that replaces files on the NAS (decision 85)", async () => {
    renderApp("/annexes/7", { persona: "admin" });
    const list = getAnnexMock().rulesets.get("7") ?? [];
    const base = list[0];
    if (!base) throw new Error("no running rule set");
    list.push({ ...base, version: 3, state: "proposed", replaces_on_nas: ["back"] });
    const rules = await screen.findByRole("region", { name: "Rules" });
    const proposals = await within(rules).findByRole("list", { name: "Proposals" });
    const note = within(proposals).getByTestId("replaces-on-nas");
    expect(note).toHaveTextContent(/Replaces files on the NAS: rule back/);
    expect(note).toHaveTextContent(/empty for the grace period/);
  });

  it("says what replacing on a NAS does when the Back half chooses it", async () => {
    const { user, container } = renderApp("/annexes/7/rules", { persona: "admin" });
    expect(await screen.findByRole("heading", { name: "Rules" })).toBeInTheDocument();
    const back = screen.getByRole("region", { name: "Back: when a removed file is recalled" });
    await user.click(within(back).getByRole("button", { name: "Back where it came from" }));
    const conflict = within(back).getByLabelText("If the name is taken");
    expect(within(back).queryByText(/name stays empty for the grace period/)).toBeNull();
    await user.selectOptions(conflict, "overwrite_keep_version");
    const hint = within(back).getByText(/name stays empty for the grace period/);
    expect(hint).toHaveTextContent(/only the file's own name in its original folder/);
    expect(hint).toHaveTextContent(/-replaced-/);
    await expectNoAxeViolations(container);
  });

  it("says what became of the file a placement replaced", () => {
    const p = (replace: Placement["replace"]) => ({ replace }) as Placement;
    const base = { refused: null, replaced_version_id: "12", hidden_name: null, kept_as: null };
    expect(replaceSummary(p(null))).toBeNull();
    expect(replaceSummary(p({ ...base, state: "replaced" }))).toBe(
      "Replaced; the old content is version 12.",
    );
    expect(
      replaceSummary(p({ ...base, state: "kept_aside", kept_as: "a-replaced-x.segy" })),
    ).toMatch(/kept as a-replaced-x.segy/);
    expect(replaceSummary(p({ ...base, state: "refused", refused: "legal_hold" }))).toMatch(
      /Not replaced \(legal_hold\)/,
    );
    expect(
      replaceSummary(p({ ...base, state: "hidden", hidden_name: ".omnifile-replacing-1-a" })),
    ).toMatch(/hidden as .omnifile-replacing-1-a/);
    expect(replaceSummary(p({ ...base, state: "gone" }))).toMatch(/OmniFile deleted nothing/);
  });

  it("builds rules: an ordered list, a diagram with live counts, saved as a proposal", async () => {
    const { user, container, router } = renderApp("/annexes/7/rules", { persona: "admin" });
    expect(await screen.findByRole("heading", { name: "Rules" })).toBeInTheDocument();
    // The running Copy rules, then live counts from a dry run.
    expect(
      await screen.findByText(/files .* would be copied/, {}, { timeout: 4000 }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start from Archive after 30 days" }));
    expect(screen.getByRole("note")).toHaveTextContent(/need an ArchiveAdmin/);
    const flow = screen.getByLabelText("Rule flow");
    expect(within(flow).getAllByText(/leaves the NAS/).length).toBeGreaterThan(0);
    // Reorder with the buttons (Alt+arrows do the same).
    const legends = () =>
      screen.getAllByRole("group").map((g) => g.querySelector("legend")?.textContent ?? "");
    expect(legends().filter((l) => l.startsWith("Rule")).length).toBe(5);
    await user.click(screen.getByRole("button", { name: "Move rule 1 down" }));
    await user.type(screen.getByLabelText("Why (for the approver)"), "free the NAS");
    await expectNoAxeViolations(container);
    await user.click(screen.getByRole("button", { name: "Save as a proposal" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/annexes/7"));
    const [call] = calls("POST", /^\/v1\/annexes\/7\/rulesets$/);
    const body = call?.body as {
      body: { rules: { trigger: string; id: string }[] };
      rationale: string;
    };
    expect(body.rationale).toBe("free the NAS");
    // The template's Back rule comes with it: back where it came from.
    expect(body.body.rules.map((r) => r.id)).toEqual([
      "recopy",
      "copy",
      "archive",
      "stay",
      "keep",
      "back",
    ]);
    await waitFor(() => expect(calls("POST", /\/annexes\/7\/dry-runs$/).length).toBeGreaterThan(0));
  });

  it("builds the Back half: where a recalled file lands, with the preview's counts", async () => {
    const { user, container, router } = renderApp("/annexes/7/rules", { persona: "admin" });
    expect(await screen.findByRole("heading", { name: "Rules" })).toBeInTheDocument();
    const back = screen.getByRole("region", { name: "Back: when a removed file is recalled" });
    // The running Copy rules have none: placed nowhere.
    expect(within(back).getByText(/placed nowhere/)).toBeInTheDocument();
    await user.click(within(back).getByRole("button", { name: "Back where it came from" }));
    const flow = within(back).getByLabelText("Back rule flow");
    expect(within(flow).getByText("placed at back where it came from")).toBeInTheDocument();
    expect(within(flow).getByText(/hold 14 days/)).toBeInTheDocument();
    // The service's preview over the removed files.
    expect(
      await within(flow).findByText(/today: 1 files .*1 names taken/, {}, { timeout: 4000 }),
    ).toBeInTheDocument();
    // A second rule, for a group, into a folder of this annex; moved first.
    await user.click(within(back).getByRole("button", { name: "+ Back rule" }));
    const place = within(back).getAllByLabelText("Place at")[1];
    if (!place) throw new Error("no second Place at");
    await user.selectOptions(place, "annex_folder");
    const folder = within(back).getByLabelText("Folder");
    await user.type(folder, "restored");
    await user.click(within(back).getByRole("button", { name: "Move back rule 2 up" }));
    await expectNoAxeViolations(container);
    await user.click(screen.getByRole("button", { name: "Save as a proposal" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/annexes/7"));
    const [call] = calls("POST", /^\/v1\/annexes\/7\/rulesets$/);
    const rules = (
      call?.body as { body: { rules: { trigger: string; id: string; actions: object[] }[] } }
    ).body.rules;
    const backRules = rules.filter((r) => r.trigger === "recall_completed");
    expect(backRules.map((r) => r.id)).toEqual([expect.stringMatching(/^back-/), "back"]);
    expect(backRules[0]?.actions).toContainEqual({
      place_at: { kind: "nas_folder", path: "restored" },
    });
    expect(calls("POST", /\/annexes\/7\/return-preview$/).length).toBeGreaterThan(0);
  });

  it("gets a removed file back to where it came from", async () => {
    const { user, container } = renderApp("/annexes/7", { persona: "admin" });
    await user.click(await screen.findByRole("button", { name: "Get dataset/old.segy back" }));
    const dialog = await screen.findByRole("dialog", { name: "Get it back" });
    const where = await within(dialog).findByRole("group", { name: "Where" });
    const radios = within(where).getAllByRole("radio");
    expect(radios[0]).toBeChecked();
    // A place the requester can't add a file to can't be picked, and says why.
    expect(radios[1]).toBeDisabled();
    expect(within(where).getByText(/isn't an Owner or Writer/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/Why do you need it/), "reprocessing");
    await expectNoAxeViolations(container);
    await user.click(within(dialog).getByRole("button", { name: "Place it there" }));
    await waitFor(() => expect(calls("POST", /^\/v1\/placements$/)).toHaveLength(1));
    const [call] = calls("POST", /^\/v1\/placements$/);
    expect(call?.body).toEqual({ file_id: "104", place_at: "default", note: "reprocessing" });
    expect(await within(dialog).findByText("Ready to be placed")).toBeInTheDocument();
    // Only a file that has left the NAS can be got back.
    expect(screen.queryByRole("button", { name: "Get dataset/new.segy back" })).toBeNull();
  });

  it("shows a check that blocks saving next to its rule", async () => {
    const { user } = renderApp("/annexes/7/rules", { persona: "admin" });
    await screen.findByRole("heading", { name: "Rules" });
    const [ext] = await screen.findAllByLabelText("Extensions");
    if (!ext) throw new Error("no Extensions field");
    // The arrival rule gets a condition: no rule is left for everything else.
    await user.type(ext, "segy");
    await user.click(screen.getByRole("button", { name: "Save as a proposal" }));
    expect(await screen.findByRole("list", { name: "Problems with rule 1" })).toHaveTextContent(
      /Needs a last rule for everything else/,
    );
  });

  it("counts a reorder within a trigger as a change", () => {
    const a = { id: "a", trigger: "arrived" as const, conditions: {}, actions: [] };
    const b = {
      id: "b",
      trigger: "arrived" as const,
      conditions: { extension: ["tmp"] },
      actions: [],
    };
    const age = { id: "c", trigger: "age" as const, conditions: {}, actions: [] };
    const d = ruleDiff([b, age, a], [a, b, age]);
    expect(d.moved.map((r) => r.id)).toEqual(["b", "a"]);
    expect(d.added.length + d.removed.length + d.changed.length).toBe(0);
    // Another trigger's rules in between don't move anything.
    expect(ruleDiff([a, age, b], [a, b, age]).moved).toEqual([]);
  });

  it("lets an ArchiveAdmin end the dry run, and nobody else (decision 59)", async () => {
    const { user } = renderApp("/annexes/7", { persona: "admin" });
    const a = getAnnexMock().annexes.find((x) => x.annex_id === "7");
    if (!a?.removals) throw new Error("annex 7");
    a.removals.dry_run_until = new Date(Date.now() - 60_000).toISOString();
    a.removals.confirmed_version = null;
    // Decision 59: what the dry run would remove, before anyone confirms it.
    const what = await screen.findByLabelText("What the dry run would remove");
    expect(what).toHaveTextContent(/2 files/);
    expect(within(what).getByText("surveys/2025/line_0001.segy")).toBeInTheDocument();
    const button = await screen.findByRole("button", { name: "Confirm removals for version 1" });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);
    await waitFor(() => expect(calls("POST", /\/annexes\/7\/removals\/confirm$/)).toHaveLength(1));
    const [call] = calls("POST", /\/annexes\/7\/removals\/confirm$/);
    expect(call?.headers["if-match"]).toBeTruthy();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Confirm removals/ })).toBeNull(),
    );
  });

  it("never offers the confirmation to someone who isn't an ArchiveAdmin", async () => {
    renderApp("/annexes/7", { persona: "steward" });
    const a = getAnnexMock().annexes.find((x) => x.annex_id === "7");
    if (!a?.removals) throw new Error("annex 7");
    a.removals.dry_run_until = new Date(Date.now() - 60_000).toISOString();
    expect(await screen.findByRole("region", { name: "Removals" })).toHaveTextContent(
      /until an ArchiveAdmin who reviewed it confirms/,
    );
    expect(screen.queryByRole("button", { name: /Confirm removals/ })).toBeNull();
  });
});
