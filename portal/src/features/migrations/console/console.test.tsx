import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { requestLog } from "../../../mocks/handlers";
import { expectNoAxeViolations } from "../../../test/axe";
import { renderApp } from "../../../test/render";
import { polling } from "../api";
import { attentionLabel } from "./consoleLabels";

const posts = (path: string) => requestLog.filter((r) => r.method === "POST" && r.path === path);
const patches = () => requestLog.filter((r) => r.method === "PATCH");

beforeEach(() => {
  polling.ms = 20;
});
afterEach(() => {
  polling.ms = 3000;
});

describe("the migrations console", () => {
  it("shows throughput, an ETA with its basis, health and the next wave", async () => {
    const { container } = renderApp("/migrations");
    const table = await screen.findByRole("table", { name: "Prefixes being moved" });
    const copying = within(table).getByRole("link", { name: "raw/2019/batch-b/" }).closest("tr");
    if (!copying) throw new Error("row");
    const row = within(copying);
    expect(row.getByText("180 MB/s")).toBeInTheDocument();
    expect(row.getByText("Needs attention")).toBeInTheDocument();
    // The ETA comes from the last hour's rate, and says so.
    expect(row.getByText(/the last hour's rate/)).toBeInTheDocument();
    expect(row.getByRole("link", { name: /^2021/ })).toHaveAttribute("href", "/migrations/plans/3");
    expect(container.textContent).not.toMatch(/Prizmanyx/i);
    await expectNoAxeViolations(container);
  });

  it("promises no time while objects are still archived in S3", async () => {
    const { db } = renderApp("/migrations");
    const m = db.migration.migrations.find((x) => x.migration_id === "7");
    if (!m) throw new Error("fixture");
    m.files = { ...m.files, verified: 100 };
    const table = await screen.findByRole("table", { name: "Prefixes being moved" });
    const row = within(
      within(table).getByRole("link", { name: "raw/2019/batch-b/" }).closest("tr") as HTMLElement,
    );
    expect(row.getByText("No time promised")).toBeInTheDocument();
    expect(row.getByText(/archive class and must be restored first/)).toBeInTheDocument();
  });

  it("expands a migration to show who works on it", async () => {
    const { user } = renderApp("/migrations");
    await user.click(
      await screen.findByRole("button", { name: "Show who works on raw/2019/batch-b/" }),
    );
    expect(
      await screen.findByRole("table", { name: "Storage Mover agents for migration 7" }),
    ).toBeInTheDocument();
    const waves = screen.getByRole("table", { name: "Restore waves for migration 7" });
    expect(within(waves).getByText("omnifile-wave-7-2")).toBeInTheDocument();
    expect(within(waves).getByText("980 / 1,400")).toBeInTheDocument();
    expect(screen.getByText("edge-houston-1")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Hide who works on raw/2019/batch-b/" }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("offers each fix in place, and every one reaches the API", async () => {
    const { user } = renderApp("/migrations");
    const panel = await screen.findByRole("region", { name: /Needs attention/ });
    const failed = within(panel)
      .getAllByText("Copies failed their check")
      .map((el) => el.closest("li"))
      .find((li) => li && within(li).queryByRole("link", { name: "migration 7" }));
    if (!failed) throw new Error("item");
    await user.click(within(failed as HTMLElement).getByRole("button", { name: "Copy again" }));
    await waitFor(() => expect(posts("/v1/migrations/7/rerun")).toHaveLength(1));
    await user.click(within(panel).getByRole("button", { name: "Restore again" }));
    await waitFor(() => expect(posts("/v1/migrations/7/restore-again")).toHaveLength(1));
    // Exclusion needs a fresh sign-in, on the migration's own page.
    expect(
      within(failed as HTMLElement).getByRole("link", { name: "Exclude objects…" }),
    ).toHaveAttribute("href", "/migrations/7?exclude=1");
    expect(within(panel).getByText("Agent offline")).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "See the agents" })).toHaveAttribute(
      "href",
      "/migrations/agents",
    );
  });

  it("opens the exclude dialog from the console's link", async () => {
    renderApp("/migrations/7?exclude=1");
    expect(
      await screen.findByRole("dialog", { name: "Exclude objects from verification" }),
    ).toBeInTheDocument();
  });

  it("lists the agents and the jobs, and where they run", async () => {
    const { container } = renderApp("/migrations/agents");
    const agents = await screen.findByRole("table", { name: "Storage Mover agents" });
    expect(within(agents).getByText("smagent-calgary-02")).toBeInTheDocument();
    expect(within(agents).getByText("Offline")).toBeInTheDocument();
    expect(within(agents).getByText("houston-mover-01")).toBeInTheDocument();
    const jobs = screen.getByRole("table", { name: "Jobs under way" });
    expect(within(jobs).getByRole("link", { name: "raw/2019/batch-b/" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("is for ArchiveAdmin only", async () => {
    renderApp("/migrations/agents", { persona: "steward" });
    expect(await screen.findByText("Migrations are for ArchiveAdmin.")).toBeInTheDocument();
  });
});

describe("staged plans", () => {
  it("retries a failed wave and cancels a running one, on the plan", async () => {
    const { user, db } = renderApp("/migrations/plans/3");
    const plan = db.migration.console.plans.find((p) => p.plan_id === "3");
    const m7 = db.migration.migrations.find((x) => x.migration_id === "7");
    if (!plan || !m7) throw new Error("fixture");
    m7.state = "failed";
    const wave = plan.waves.find((w) => w.seq === 2);
    if (wave) wave.state = "failed";
    await user.click(await screen.findByRole("button", { name: "Retry wave 2" }));
    await waitFor(() => expect(posts("/v1/migration-plans/3/waves/2/retry")).toHaveLength(1));
    await user.click(await screen.findByRole("button", { name: "Cancel wave 2" }));
    await waitFor(() => expect(posts("/v1/migration-plans/3/waves/2/cancel")).toHaveLength(1));
  });

  it("unblocks a blocked wave by dropping what it waits for", async () => {
    // Re-review of #45: a blocked wave isn't a dead end.
    const { user, db } = renderApp("/migrations/plans/3");
    const plan = db.migration.console.plans.find((p) => p.plan_id === "3");
    const w3 = plan?.waves.find((w) => w.seq === 3);
    if (!w3) throw new Error("fixture");
    w3.state = "blocked";
    w3.waiting_reason = "wave 2 was cancelled: this wave doesn't start";
    await user.click(await screen.findByRole("button", { name: "Don't wait for wave 2" }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0]?.body).toEqual({ base_version: 4, after: null });
  });

  it("says when a split leaves out folders you can't read", async () => {
    renderApp("/migrations/plans/new?source=9");
    expect(await screen.findByText("Folders you can't read")).toBeInTheDocument();
    expect(screen.getByText(/aren.t named here or put in a wave/)).toBeInTheDocument();
  });

  it("approves a plan proposed through the API", async () => {
    const { user } = renderApp("/migrations/plans/4");
    expect(await screen.findByText(/Proposed, waiting for approval/)).toBeInTheDocument();
    expect(screen.getByText(/proposed through the API/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts("/v1/migration-plans/4/approve")).toHaveLength(1));
    expect(await screen.findByText(/^Approved/)).toBeInTheDocument();
  });

  it("moves a wave on the schedule with the keyboard, within its dependencies, then saves", async () => {
    const { user, container } = renderApp("/migrations/plans/3");
    const handle = await screen.findByRole("slider", { name: "Planned start of wave 3, 2021" });
    const before = Number(handle.getAttribute("aria-valuenow"));
    // Wave 3 waits for wave 2: Home takes it to wave 2's start, never before.
    fireEvent.keyDown(handle, { key: "Home" });
    const lo = Number(handle.getAttribute("aria-valuemin"));
    expect(Number(handle.getAttribute("aria-valuenow"))).toBe(lo);
    fireEvent.keyDown(handle, { key: "ArrowRight", shiftKey: true });
    expect(Number(handle.getAttribute("aria-valuenow"))).toBe(lo + 86_400_000);
    expect(handle).toHaveAttribute("aria-valuetext", expect.stringMatching(/ UTC, not saved$/));
    expect(before).not.toBe(lo + 86_400_000);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0]?.body).toEqual({
      base_version: 4,
      start_at: new Date(lo + 86_400_000).toISOString(),
    });
    // A started or finished wave can't be moved.
    expect(
      screen.getByRole("slider", { name: "Planned start of wave 1, 2018 and before" }),
    ).toHaveAttribute("aria-disabled", "true");
    await expectNoAxeViolations(container);
  });

  it("reschedules through the typed alternative, and says when it breaks a dependency", async () => {
    const { user } = renderApp("/migrations/plans/3");
    const waves = await screen.findByRole("table", { name: "Waves" });
    const input = within(waves).getByLabelText("Start of wave 3 (UTC)");
    await user.clear(input);
    await user.type(input, "2020-01-01T10:00");
    expect(within(waves).getByText(/It waits for wave 2/)).toBeInTheDocument();
    expect(within(waves).getAllByRole("button", { name: "Reschedule" })[0]).toBeDisabled();
  });

  it("splits a source into waves and makes the plan", async () => {
    const { user, router } = renderApp("/migrations/plans/new?source=9");
    const preview = await screen.findByRole("table", { name: "Waves to make" });
    await waitFor(() => expect(within(preview).getAllByRole("row")).toHaveLength(5));
    expect(screen.getByText(/lie directly under/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Plan name"), "Raw 2021 in four");
    await user.click(screen.getByRole("button", { name: "Make plan" }));
    await waitFor(() =>
      expect(router.state.location.pathname).toMatch(/^\/migrations\/plans\/\d+$/),
    );
    const body = posts("/v1/migration-plans")[0]?.body as {
      dataset_id: string;
      waves: { after: number | null; window: unknown; prefixes: string[] }[];
    };
    expect(body.dataset_id).toBe("1");
    expect(body.waves).toHaveLength(4);
    expect(body.waves.map((w) => w.after)).toEqual([null, 1, 2, 3]);
    expect(body.waves[0]?.window).toEqual({ start: "22:00", end: "06:00" });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Raw 2021 in four" }),
    ).toBeInTheDocument();
  });

  it("names the kinds of copy nothing will check", () => {
    expect(attentionLabel("check_given_up")).toBe("Checks gave up");
    expect(attentionLabel("copy_stranded")).toBe("Copies nothing will check");
    expect(attentionLabel("no_checksum")).toBe("Copies with no checksum");
  });
});
