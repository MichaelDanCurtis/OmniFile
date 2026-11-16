import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

const deletes = () => requestLog.filter((r) => r.method === "DELETE");

describe("remove a share", () => {
  it("is not offered to a DataSteward who owns the share", async () => {
    renderApp("/shares/1", { persona: "steward" });
    await screen.findByRole("region", { name: "Save settings" });
    expect(screen.queryByRole("button", { name: /Remove share/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Remove this share" })).not.toBeInTheDocument();
  });

  it("says plainly what goes and what stays, then removes it with If-Match", async () => {
    const { user, db, router, container } = renderApp("/shares/2", { persona: "admin" });
    const etag = db.shares.find((s) => s.share_id === "2")?.etag;
    await user.click(await screen.findByRole("button", { name: "Remove share…" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove processing-scratch?" });
    expect(within(dialog).getByText("People lose access through this share.")).toBeVisible();
    expect(within(dialog).getByText("Nothing is deleted.")).toBeVisible();
    expect(
      within(dialog).getByText(/its files and their versions\s+stay, and a new share can serve/),
    ).toBeVisible();
    await expectNoAxeViolations(container);

    await user.click(within(dialog).getByRole("button", { name: "Remove share" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/shares"));
    expect(deletes()).toHaveLength(1);
    expect(deletes()[0]?.path).toBe("/v1/shares/2");
    expect(deletes()[0]?.headers["if-match"]).toBe(etag);
    expect(deletes()[0]?.headers["x-csrf-token"]).toBe("mock-csrf-admin");

    // Only the share went: the dataset stays, and the list no longer has it.
    expect(db.shares.some((s) => s.share_id === "2")).toBe(false);
    expect(db.datasets.find((d) => d.dataset_id === "1")?.shares.map((s) => s.name)).toEqual([
      "long-term-storage",
    ]);
    const table = await screen.findByRole("table", { name: "Shares" });
    await waitFor(() =>
      expect(within(table).queryByRole("link", { name: "processing-scratch" })).toBeNull(),
    );
    expect(within(table).getByRole("link", { name: "long-term-storage" })).toBeInTheDocument();
  });

  it("explains share_in_use when folder shares still need the root", async () => {
    const { user, db, router } = renderApp("/shares/1", { persona: "admin" });
    db.sharePaths.set("2", "/reports");
    await user.click(await screen.findByRole("button", { name: "Remove share…" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove long-term-storage?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove share" }));
    expect(await within(dialog).findByText("The share wasn't removed")).toBeInTheDocument();
    expect(within(dialog).getByText(/processing-scratch still needs/)).toBeInTheDocument();
    expect(deletes()).toHaveLength(1);
    expect(router.state.location.pathname).toBe("/shares/1");
    expect(db.shares.some((s) => s.share_id === "1")).toBe(true);
  });

  it("is disabled, with the reason, for a WORM share", async () => {
    renderApp("/shares/3", { persona: "admin" });
    const button = await screen.findByRole("button", { name: "Remove share…" });
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(/A WORM share can't be removed/);
  });

  it("shows the server's worm_violation refusal", async () => {
    const { user, db } = renderApp("/shares/2", { persona: "admin" });
    await user.click(await screen.findByRole("button", { name: "Remove share…" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove processing-scratch?" });
    // WORM was turned on elsewhere after this page loaded.
    const share = db.shares.find((s) => s.share_id === "2");
    if (share) share.worm = { ...share.worm, enabled: true };
    await user.click(within(dialog).getByRole("button", { name: "Remove share" }));
    expect(await within(dialog).findByText(/it is never removed/)).toBeInTheDocument();
    expect(db.shares.some((s) => s.share_id === "2")).toBe(true);
  });
});
