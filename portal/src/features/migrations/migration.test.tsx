import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { http } from "msw";
import { example } from "../../mocks/examples.gen";
import { requestLog } from "../../mocks/handlers";
import { server } from "../../mocks/server";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";
import { polling } from "./api";

const posts = (path: string) => requestLog.filter((r) => r.method === "POST" && r.path === path);
const step = (name: string) =>
  within(screen.getByRole("list", { name: "Migration progress" }))
    .getByText(name)
    .closest("li");

beforeEach(() => {
  polling.ms = 20;
});
afterEach(() => {
  polling.ms = 3000;
});

describe("a migration's page", () => {
  it("shows the lifecycle, counts and copier, and cuts over a verified manual migration", async () => {
    const { user, container } = renderApp("/migrations/5");
    await screen.findByRole("heading", { level: 1, name: /Seismic survey archive: raw\/2019\// });
    expect(step("Verifying")).toHaveAttribute("data-status", "done");
    expect(step("Verified")).toHaveAttribute("data-status", "current");
    expect(step("Cut over")).toHaveAttribute("data-status", "pending");
    const files = screen.getByRole("region", { name: "Files" });
    expect(within(files).getByLabelText("14998 of 15000 files verified")).toBeInTheDocument();
    expect(within(files).getByText("Excluded").nextElementSibling).toHaveTextContent(
      "2 (stay served from S3)",
    );
    const copier = screen.getByRole("region", { name: "Copier" });
    expect(within(copier).getByText("API (an external copier)")).toBeInTheDocument();
    expect(within(copier).getByText("Runs reported").nextElementSibling).toHaveTextContent("3");
    expect(screen.getByText("API client migration-partner")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/Prizmanyx/i);

    // The cutover report: the full list for an ArchiveAdmin (owner, 2026-11-04).
    const report = screen.getByRole("region", { name: "Cutover report" });
    expect(
      await within(report).findByText("4 deletes are waiting in the source."),
    ).toBeInTheDocument();
    const rows = within(report).getByRole("table", { name: "Deletes waiting in the source" });
    expect(within(rows).getAllByRole("row")).toHaveLength(5); // a header and four rows
    expect(
      within(report).getByText(
        /readable by ArchiveAdmins and by registered API clients with Archive\.Adopt/,
      ),
    ).toBeInTheDocument();
    expect(
      within(report).getByText(/An API client never reads another client's migration/),
    ).toBeInTheDocument();
    expect(within(report).queryByText(/no AD identity/)).not.toBeInTheDocument();
    await expectNoAxeViolations(container);

    await user.click(screen.getByRole("button", { name: "Cut over" }));
    await waitFor(() => expect(posts("/v1/migrations/5/cutover")).toHaveLength(1));
    expect(posts("/v1/migrations/5/cutover")[0]?.body).toEqual({});
    expect(await screen.findByText("Done")).toBeInTheDocument();
    expect(step("Cut over")).toHaveAttribute("data-status", "done");
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("offers no Cut over to an automatic migration, and cancels after a confirmation", async () => {
    const { user, db } = renderApp("/migrations/7");
    // Not a plan's here (a plan's migration is cancelled on its plan).
    const own = db.migration.migrations.find((x) => x.migration_id === "7");
    if (own) own.plan = null;
    await screen.findByRole("heading", { level: 1, name: /raw\/2019\/batch-b\// });
    expect(step("Copying")).toHaveAttribute("data-status", "current");
    expect(screen.queryByRole("button", { name: "Cut over" })).not.toBeInTheDocument();
    expect(screen.getByText("omnifile-migration-7")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    const dialog = await screen.findByRole("dialog", { name: "Cancel this migration?" });
    expect(within(dialog).getByText(/Nothing is deleted anywhere/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel migration" }));
    expect(
      await screen.findByText(/Nothing was deleted\. Files that switched/),
    ).toBeInTheDocument();
    expect(posts("/v1/migrations/7/cancel")).toHaveLength(1);
    // It stopped where it had got to: copies were already being verified.
    expect(step("Copying")).toHaveAttribute("data-status", "done");
    expect(step("Verifying")).toHaveAttribute("data-status", "failed");
  });

  it("retries a failed migration", async () => {
    const { user, db } = renderApp("/migrations/7");
    const m = db.migration.migrations.find((x) => x.migration_id === "7");
    if (!m) throw new Error("fixture");
    m.plan = null;
    m.state = "failed";
    m.last_error = "Storage Mover job omnifile-migration-7 failed: the agent is offline";
    expect(await screen.findByText("The migration stopped")).toBeInTheDocument();
    expect(screen.getByText(/the agent is offline/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(posts("/v1/migrations/7/retry")).toHaveLength(1));
    await waitFor(() =>
      expect(screen.queryByText("The migration stopped")).not.toBeInTheDocument(),
    );
  });

  it("says why copies are stuck when there is no error to show", async () => {
    const { db } = renderApp("/migrations/7");
    const m = db.migration.migrations.find((x) => x.migration_id === "7");
    if (!m) throw new Error("fixture");
    m.last_error = null;
    m.stuck_copies = { given_up: 2, stranded: 1, no_checksum: 1 };
    expect(await screen.findByText(/The check of 2 copies kept failing/)).toBeInTheDocument();
    expect(screen.getByText(/with no check queued: 1\./)).toBeInTheDocument();
    expect(screen.getByText(/no checksum on record for them: 1\./)).toBeInTheDocument();
  });

  it("excludes objects only after a fresh sign-in", async () => {
    const { user, db } = renderApp("/migrations/7", { signedInAgoMs: 20 * 60_000 });
    await user.click(await screen.findByRole("button", { name: "Exclude objects" }));
    const dialog = await screen.findByRole("dialog", { name: "Exclude objects from verification" });
    const submit = within(dialog).getByRole("button", { name: "Exclude them" });
    expect(submit).toBeDisabled();
    await user.type(
      within(dialog).getByLabelText("Object keys"),
      "raw/2019/batch-b/sse_c.segy\nraw/2018/elsewhere.segy",
    );
    expect(within(dialog).getByText(/1 key is outside the prefix/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Why"), "SSE-C: S3 can't give a checksum");
    await user.click(submit);
    expect(await within(dialog).findByText("Sign in again to continue")).toBeInTheDocument();

    db.authTime = Date.now();
    await user.click(submit);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const last = posts("/v1/migrations/7/exclude").at(-1);
    expect(last?.body).toEqual({
      object_keys: ["raw/2019/batch-b/sse_c.segy", "raw/2018/elsewhere.segy"],
      reason: "SSE-C: S3 can't give a checksum",
    });
    const files = screen.getByRole("region", { name: "Files" });
    await waitFor(() =>
      expect(within(files).getByText("Excluded").nextElementSibling).toHaveTextContent(/^1/),
    );
  });

  it("takes the keys to exclude from the worker's list", async () => {
    const { user, db } = renderApp("/migrations/7?exclude=1");
    const m = db.migration.migrations.find((x) => x.migration_id === "7");
    if (!m) throw new Error("fixture");
    m.stuck_copies = { given_up: 0, stranded: 1, no_checksum: 203 };
    const many = Array.from(
      { length: 203 },
      (_, n) => `raw/2019/batch-b/${String(n).padStart(3, "0")}`,
    );
    db.migration.stuckKeys.set("7", { no_checksum: many, stranded: ["raw/2019/batch-b/s.segy"] });
    const dialog = await screen.findByRole("dialog", { name: "Exclude objects from verification" });
    const list = await within(dialog).findByRole("group", { name: "Objects to exclude" });
    expect(within(dialog).getByLabelText("Copies holding up the cutover")).toHaveValue(
      "no_checksum",
    );
    expect(within(list).getAllByRole("checkbox")).toHaveLength(200);

    // A page at a time, and what it adds goes where a typed key would.
    await user.click(within(list).getByRole("checkbox", { name: "raw/2019/batch-b/001" }));
    const box = within(dialog).getByLabelText("Object keys");
    expect(box).toHaveValue("raw/2019/batch-b/001");
    await user.click(within(dialog).getByRole("button", { name: "Show more" }));
    await waitFor(() => expect(within(list).getAllByRole("checkbox")).toHaveLength(203));
    expect(within(dialog).queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    await user.click(within(list).getByRole("checkbox", { name: "raw/2019/batch-b/202" }));
    expect(box).toHaveValue("raw/2019/batch-b/001\nraw/2019/batch-b/202");
    await user.click(within(list).getByRole("checkbox", { name: "raw/2019/batch-b/001" }));
    expect(box).toHaveValue("raw/2019/batch-b/202");

    // The other kind has its own list, and a word of warning. What was chosen stays chosen.
    await user.selectOptions(
      within(dialog).getByLabelText("Copies holding up the cutover"),
      "stranded",
    );
    expect(await within(dialog).findByText(/could still be verified/)).toBeInTheDocument();
    await user.click(
      await within(dialog).findByRole("checkbox", { name: "raw/2019/batch-b/s.segy" }),
    );
    await user.type(within(dialog).getByLabelText("Why"), "SSE-C");
    await user.click(within(dialog).getByRole("button", { name: "Exclude them" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(posts("/v1/migrations/7/exclude").at(-1)?.body).toEqual({
      object_keys: ["raw/2019/batch-b/202", "raw/2019/batch-b/s.segy"],
      reason: "SSE-C",
    });
  });

  it("chooses every key shown in one go, and says when the list stops short", async () => {
    const { user, db } = renderApp("/migrations/7?exclude=1");
    const m = db.migration.migrations.find((x) => x.migration_id === "7");
    if (!m) throw new Error("fixture");
    m.stuck_copies = { given_up: 0, stranded: 0, no_checksum: 4000000 };
    db.migration.stuckKeys.set("7", {
      no_checksum: ["raw/2019/batch-b/a", "raw/2019/batch-b/b"],
      stranded: [],
    });
    const dialog = await screen.findByRole("dialog", { name: "Exclude objects from verification" });
    await user.click(await within(dialog).findByRole("button", { name: "Choose all 2 shown" }));
    expect(within(dialog).getByLabelText("Object keys")).toHaveValue(
      "raw/2019/batch-b/a\nraw/2019/batch-b/b",
    );
    expect(within(dialog).getByRole("button", { name: "Choose all 2 shown" })).toBeDisabled();
    expect(within(dialog).getByText(/first 10,000 of 4,000,000 by key/)).toBeInTheDocument();
  });

  it("has no list to take from when nothing is stuck", async () => {
    const { user } = renderApp("/migrations/7");
    await user.click(await screen.findByRole("button", { name: "Exclude objects" }));
    const dialog = await screen.findByRole("dialog", { name: "Exclude objects from verification" });
    expect(
      within(dialog).queryByLabelText("Copies holding up the cutover"),
    ).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("Object keys")).toBeInTheDocument();
  });

  it("tells an admin with no AD identity why the cutover report shows no rows", async () => {
    const { db } = renderApp("/migrations/5");
    db.users.admin.sid = null;
    const report = await screen.findByRole("region", { name: "Cutover report" });
    expect(await within(report).findByText(/Your account has no AD identity/)).toBeInTheDocument();
    expect(within(report).queryByRole("table")).not.toBeInTheDocument();
  });

  it("compares the rows with the total only once the whole report is in", async () => {
    // Review of #41: a first page with more to come is not "fewer rows than the total".
    server.use(
      http.get("*/v1/migrations/:id/tombstones", () =>
        Response.json({
          ...example.TombstoneReport,
          migration_id: "5",
          source_deletes_pending: 4,
          next_cursor: "more",
        }),
      ),
    );
    renderApp("/migrations/5");
    const report = await screen.findByRole("region", { name: "Cutover report" });
    expect(
      await within(report).findByRole("table", { name: "Deletes waiting in the source" }),
    ).toBeInTheDocument();
    expect(within(report).getByText("The first 100 rows are shown.")).toBeInTheDocument();
    expect(
      within(report).queryByText(/no AD identity|The list changed|You see the/),
    ).not.toBeInTheDocument();
  });

  it("sends a plan's migration to its plan for cutover, cancel and retry", async () => {
    // Review of #45: the plan owns its waves' migrations.
    const { db } = renderApp("/migrations/7");
    const m = db.migration.migrations.find((x) => x.migration_id === "7");
    if (!m) throw new Error("fixture");
    m.state = "failed";
    expect(await screen.findByText("Wave 2 of a staged plan")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open its plan" })).toHaveAttribute(
      "href",
      "/migrations/plans/3",
    );
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cut over" })).not.toBeInTheDocument();
    // Exclude stays: it is the admin's decision, with a fresh sign-in.
    expect(screen.getByRole("button", { name: "Exclude objects" })).toBeInTheDocument();
  });

  it("shows a state this portal doesn't know as it is", async () => {
    const { db } = renderApp("/migrations/6");
    const m = db.migration.migrations.find((x) => x.migration_id === "6");
    if (!m) throw new Error("fixture");
    m.state = "handed_off";
    m.cut_over_at = null;
    expect(await screen.findByText(/^handed_off · from/)).toBeInTheDocument();
  });
});
