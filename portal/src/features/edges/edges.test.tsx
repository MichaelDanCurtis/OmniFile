import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("edges", () => {
  it("shows each edge's health and why one needs attention", async () => {
    const { container } = renderApp("/edges", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Edges" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(6); // header + 5 edges
    const row = (name: string) => {
      const tr = within(table).getByRole("rowheader", { name }).closest("tr");
      if (!tr) throw new Error(`no row for ${name}`);
      return tr;
    };
    expect(within(row("edge-houston-1")).getByText("OK")).toBeInTheDocument();
    expect(
      within(row("edge-houston-2")).getByText(
        "SMB guard not armed; break-glass account used: seal it again",
      ),
    ).toBeInTheDocument();
    expect(
      within(row("edge-perth-1")).getByText(
        "data volume almost full; can't read its shares' quiet periods: uploads wait the longest period",
      ),
    ).toBeInTheDocument();
    expect(within(row("edge-aberdeen-1")).getByText("Not reporting")).toBeInTheDocument();
    expect(within(row("edge-new")).getByText("Never")).toBeInTheDocument();
    expect(
      screen.getByText(/5 edges: 1 ok, 2 needs attention, 1 not reporting, 1 not enrolled/),
    ).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("Add edge opens the wizard", async () => {
    renderApp("/edges", { persona: "admin" });
    expect(await screen.findByRole("link", { name: "Add edge" })).toHaveAttribute(
      "href",
      "/edges/new",
    );
  });

  it("gives a pending edge a new code", async () => {
    const user = userEvent.setup();
    renderApp("/edges", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Edges" });
    const pending = within(table).getByRole("rowheader", { name: "edge-new" }).closest("tr");
    if (!pending) throw new Error("no pending row");
    await user.click(within(pending).getByRole("button", { name: "New code" }));
    const dialog = await screen.findByRole("dialog", { name: "New code for edge-new" });
    await user.click(within(dialog).getByRole("button", { name: "Create a new code" }));
    expect(await within(dialog).findByLabelText(/Enrolment code for/)).toBeInTheDocument();
    // Enrolled edges are never offered one.
    const ok = within(table).getByRole("rowheader", { name: "edge-houston-1" }).closest("tr");
    expect(ok && within(ok).queryByRole("button", { name: "New code" })).toBeNull();
  });

  it("revokes a lost edge once its name is typed", async () => {
    const user = userEvent.setup();
    renderApp("/edges", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Edges" });
    await user.click(within(table).getByRole("button", { name: "Revoke edge-perth-1" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke edge-perth-1?" });
    expect(within(dialog).getByText(/Nothing is deleted/)).toBeInTheDocument();
    expect(within(dialog).getByText(/can.t be undone/)).toBeInTheDocument();
    const go = within(dialog).getByRole("button", { name: "Revoke edge" });
    expect(go).toBeDisabled();
    const typed = within(dialog).getByRole("textbox", { name: /Type the edge name/ });
    await user.type(typed, "edge-perth");
    expect(go).toBeDisabled();
    await user.type(typed, "-1");
    await user.click(go);
    expect(await screen.findByText(/edge-perth-1 was revoked/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /Revoke/ })).not.toBeInTheDocument();
    await waitFor(() =>
      expect(within(table).queryByRole("rowheader", { name: "edge-perth-1" })).toBeNull(),
    );
    const sent = requestLog.find((r) => r.path.endsWith("/v1/edges/3/revoke"));
    expect(sent?.method).toBe("POST");
    expect(sent?.body).toEqual({});
  });

  it("is not in the menu for anyone else", async () => {
    renderApp("/files", { persona: "steward" });
    await screen.findByRole("navigation");
    expect(screen.queryByRole("link", { name: /Edges/ })).not.toBeInTheDocument();
  });
});
