import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("audit", () => {
  it("shows who did what, with the client address for an ArchiveAdmin", async () => {
    const { container } = renderApp("/audit", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Audit records" });
    expect(within(table).getAllByText("recall.request").length).toBeGreaterThan(0);
    expect(within(table).getByRole("columnheader", { name: "Client" })).toBeInTheDocument();
    expect(within(table).getAllByText("deny").length).toBeGreaterThan(0);
    await expectNoAxeViolations(container);
  });

  it("scopes a share Owner to their share and hides client addresses", async () => {
    renderApp("/audit", { persona: "steward" });
    const table = await screen.findByRole("table", { name: "Audit records" });
    expect(within(table).queryByRole("columnheader", { name: "Client" })).not.toBeInTheDocument();
    expect(requestLog.find((r) => r.path.startsWith("/v1/audit"))?.path).toContain("share_id=1");
  });

  it("filters by action", async () => {
    const { user } = renderApp("/audit", { persona: "admin" });
    await screen.findByRole("table", { name: "Audit records" });
    await user.type(screen.getByLabelText("Action starts with"), "pin");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(requestLog.some((r) => r.path.includes("action=pin"))).toBe(true));
  });
});
