import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("proposals", () => {
  it("lists pending proposals with who sent them", async () => {
    const { container } = renderApp("/proposals", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Proposals" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(
      within(table).getAllByText("From: API · Migration planner · owner Sam Patel")[0],
    ).toBeInTheDocument();
    expect(within(table).queryByText(/Prizmanyx/)).not.toBeInTheDocument();
    expect(within(table).getByText("Out of date")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows the diff against the active policy and the preview", async () => {
    const { container } = renderApp("/proposals/701", { persona: "admin" });
    const diff = await screen.findByRole("table", { name: "What would change" });
    const archive = within(diff).getByRole("row", { name: /Archive after/ });
    expect(within(archive).getByText("365")).toHaveClass("was");
    expect(within(archive).getByText("270")).toHaveClass("now");
    expect(screen.getByText("Nothing has changed yet")).toBeInTheDocument();
    expect(screen.getByTestId("cost-per-tb")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("lets an ArchiveAdmin approve, which makes it the active version", async () => {
    const { user } = renderApp("/proposals/701", { persona: "admin" });
    await user.click(await screen.findByRole("button", { name: "Approve and make it active" }));
    expect(await screen.findByText(/Version 5 is now active/)).toBeInTheDocument();
    expect(requestLog.some((r) => r.path === "/v1/policies/proposals/701/approve")).toBe(true);
  });

  it("needs a note to reject, and accept_stale to approve a stale one", async () => {
    const { user } = renderApp("/proposals/702", { persona: "admin" });
    const approve = await screen.findByRole("button", { name: "Approve and make it active" });
    expect(approve).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /Approve it anyway/ }));
    expect(approve).toBeEnabled();
    await user.type(screen.getByRole("textbox", { name: /Note/ }), "Owners get enough mail");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(requestLog.some((r) => r.path.endsWith("/702/reject"))).toBe(true));
    expect(requestLog.find((r) => r.path.endsWith("/702/reject"))?.body).toEqual({
      note: "Owners get enough mail",
    });
  });

  it("shows approve only to an ArchiveAdmin", async () => {
    renderApp("/proposals/701", { persona: "steward" });
    expect(
      await screen.findByText("Only an ArchiveAdmin can approve or reject this."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
  });

  it("lets the proposer withdraw their own proposal", async () => {
    const { user } = renderApp("/proposals/702", { persona: "steward" });
    await user.click(await screen.findByRole("button", { name: "Withdraw my proposal" }));
    expect(await screen.findByText("Proposal withdrawn.")).toBeInTheDocument();
  });
});

describe("proposals carry what users see (share_overrides)", () => {
  it("shows a proposal's share changes and applies them only on approval", async () => {
    const { user, db } = renderApp("/proposals/701", { persona: "admin" });
    const diff = await screen.findByRole("table", { name: "What would change" });
    const row = within(diff).getByRole("row", { name: /What people see on processing-scratch/ });
    expect(within(row).getByText("Program waits")).toHaveClass("was");
    expect(within(row).getByText("Tell them straight away")).toHaveClass("now");
    expect(db.shares.find((s) => s.share_id === "2")?.on_open).toBe("wait");
    await user.click(screen.getByRole("button", { name: "Approve and make it active" }));
    expect(await screen.findByText(/is now active/)).toBeInTheDocument();
    expect(db.shares.find((s) => s.share_id === "2")?.on_open).toBe("fail");
    expect(requestLog.some((r) => r.method === "PATCH")).toBe(false);
  });

  it("puts a DataSteward's question 3 answers in the proposal, not on the share", async () => {
    const { user } = renderApp("/policies/1", { persona: "steward" });
    const group = await screen.findByRole("group", { name: /On processing-scratch/ });
    await user.click(within(group).getByRole("radio", { name: /Tell them straight away/ }));
    // Like Save, Propose waits for the preview of the current edit (and no errors).
    const propose = screen.getByRole("button", { name: /Propose this change/ });
    await waitFor(() => expect(propose).toBeEnabled());
    await user.click(propose);
    const dialog = await screen.findByRole("dialog", { name: "Propose this policy" });
    await user.type(
      within(dialog).getByRole("textbox", { name: "Why this change?" }),
      "People, not jobs",
    );
    await user.click(within(dialog).getByRole("button", { name: "Send proposal" }));
    await waitFor(() =>
      expect(
        requestLog.some((r) => r.method === "POST" && r.path === "/v1/policies/proposals"),
      ).toBe(true),
    );
    expect(requestLog.find((r) => r.path === "/v1/policies/proposals")?.body).toMatchObject({
      share_overrides: [{ share_id: "2", on_open: "fail" }],
    });
    expect(requestLog.some((r) => r.method === "PATCH")).toBe(false);
  });
});
