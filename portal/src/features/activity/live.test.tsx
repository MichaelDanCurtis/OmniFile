import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";
import { describe as describeEvent } from "./live/LivePanel";

const livePolls = () => requestLog.filter((r) => r.path.startsWith("/v1/activity/live")).length;

describe("the live Activity feed", () => {
  it("streams events in, with counters, sparklines and the edges", async () => {
    const { container } = renderApp("/activity", { persona: "admin" });
    const feed = await screen.findByRole("list", { name: "Live events" });
    expect(within(feed).getAllByRole("listitem").length).toBeGreaterThan(5);
    for (const label of [
      "Uploads per minute",
      "Bytes moved, 15 min",
      "Recalls in flight",
      "Edges online",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(
      screen.getByRole("img", { name: /Uploads per minute, the last 30 minutes/ }),
    ).toBeInTheDocument();
    const strip = screen.getByRole("list", { name: "Edges" });
    expect(
      within(strip).getByRole("button", { name: /edge-aberdeen-1: Not reporting/ }),
    ).toBeInTheDocument();
    // A poll with `since` brings new rows, which animate in; the first answer doesn't.
    await waitFor(() => expect(container.querySelector(".live-row[data-fresh]")).not.toBeNull());
    expect(requestLog.some((r) => r.path.includes("since=") && r.path.includes("wait=20"))).toBe(
      true,
    );
    // The day-by-day dashboard is still below.
    expect(await screen.findByRole("table", { name: "Daily activity" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("filters by kind and edge, and pauses and resumes", async () => {
    const { user } = renderApp("/activity", { persona: "admin" });
    const feed = await screen.findByRole("list", { name: "Live events" });
    const heartbeat = screen.getByRole("button", { name: "Heartbeat" });
    expect(heartbeat).toHaveAttribute("aria-pressed", "true");
    await user.click(heartbeat);
    expect(heartbeat).toHaveAttribute("aria-pressed", "false");
    expect(feed.querySelector('[data-kind="heartbeat"].live-row')).toBeNull();
    await user.selectOptions(screen.getByLabelText("Edge"), "edge-houston-1");
    for (const row of within(screen.getByRole("list", { name: "Live events" })).getAllByRole(
      "listitem",
    )) {
      expect(row).toHaveTextContent("edge-houston-1");
    }
    const pause = screen.getByRole("button", { name: "Pause live feed" });
    await user.click(pause);
    expect(screen.getByRole("button", { name: "Resume live feed" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText("Paused")).toBeInTheDocument();
    const before = livePolls();
    await new Promise((r) => setTimeout(r, 450));
    expect(livePolls()).toBe(before); // nothing is asked while paused
    await user.click(screen.getByRole("button", { name: "Resume live feed" }));
    await waitFor(() => expect(livePolls()).toBeGreaterThan(before));
    // Resuming carries on from where it was: the next poll has a cursor.
    expect(requestLog.filter((r) => r.path.startsWith("/v1/activity/live")).at(-1)?.path).toMatch(
      /since=/,
    );
  });

  it("never names a file the viewer can't read", async () => {
    const { db } = renderApp("/activity", { persona: "admin" });
    db.users.admin.reader_of = ["1", "2", "4"]; // not contracts-worm
    const secret = [...db.files.values()].filter((f) => f.kind === "file" && f.share_id === "3");
    expect(secret.length).toBeGreaterThan(0);
    const feed = await screen.findByRole("list", { name: "Live events" });
    await waitFor(() =>
      expect(within(feed).getAllByText(/a file you can't read/).length).toBeGreaterThan(0),
    );
    for (const f of secret) expect(feed).not.toHaveTextContent(f.name);
  });

  it("describes each kind in plain words", () => {
    const base = {
      id: "x",
      at: "2026-11-05T10:00:00Z",
      state: null,
      edge_id: "1",
      edge_name: "edge-houston-1",
      dataset_id: null,
      share_id: null,
      share_name: null,
      file_id: null,
      name: null,
      path: null,
      hidden: false,
      bytes: null,
      person: null,
      detail: {},
    };
    expect(describeEvent({ ...base, kind: "upload", name: "a.segy" })).toBe("Uploaded a.segy");
    expect(describeEvent({ ...base, kind: "upload", hidden: true })).toBe(
      "Uploaded a file you can't read",
    );
    expect(
      describeEvent({ ...base, kind: "recall", state: "rehydrating", name: "a.segy" }),
    ).toMatch(/^Recall .+: a\.segy$/);
    expect(
      describeEvent({
        ...base,
        kind: "tier",
        state: "moved",
        name: "a",
        detail: { from: "hot", to: "cool" },
      }),
    ).toBe("Moved a from hot to cool");
    expect(
      describeEvent({
        ...base,
        kind: "tier",
        state: "late",
        name: "a",
        detail: { archive_at: "2026-11-01", to: "archive" },
      }),
    ).toBe("Archived after its date (2026-11-01): a");
    expect(
      describeEvent({
        ...base,
        kind: "tier",
        state: "waiting",
        name: "a",
        detail: { to: "cold", why: "own_copy_slow" },
      }),
    ).toBe("Move to cold waiting: a");
    expect(
      describeEvent({ ...base, kind: "heartbeat", detail: { software_version: "0.3.0" } }),
    ).toBe("edge-houston-1 reported in (0.3.0)");
    expect(
      describeEvent({
        ...base,
        kind: "migration",
        state: "cut_over",
        detail: { migration_id: "7" },
      }),
    ).toBe("Migration 7 cut over");
    // An open enum: a kind this portal doesn't know is shown as it comes.
    expect(describeEvent({ ...base, kind: "snapshot", state: "made" })).toBe("snapshot made");
  });
});
