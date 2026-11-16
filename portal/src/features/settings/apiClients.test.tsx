import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("API clients", () => {
  it("lists clients with owner and scopes, and says no keys are stored", async () => {
    const { container } = renderApp("/settings/api-clients");
    const table = await screen.findByRole("table", { name: "API clients" });
    const row = within(table).getByRole("row", { name: /Migration planner/ });
    expect(within(row).getByText("Sam Patel")).toBeInTheDocument();
    expect(within(row).getByText("Policy.Propose, Archive.Read")).toBeInTheDocument();
    expect(
      await screen.findByText("Entra app credentials are the recommended way"),
    ).toBeInTheDocument();
    expect(screen.getByText("The alternative: an OmniFile API key.")).toBeInTheDocument();
    expect(screen.getByText(/preferably a certificate/)).toBeInTheDocument();
    expect(screen.getAllByText(/\/\.default/).length).toBeGreaterThan(0);
    await expectNoAxeViolations(container);
  });

  it("registers a client with scopes and an owner", async () => {
    const { user } = renderApp("/settings/api-clients");
    await user.click(await screen.findByRole("button", { name: "Register client" }));
    const dialog = screen.getByRole("dialog", { name: "Register an API client" });
    const submit = within(dialog).getByRole("button", { name: "Register client" });
    await user.type(
      within(dialog).getByRole("textbox", { name: "Application (client) id" }),
      "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    );
    await user.type(within(dialog).getByRole("textbox", { name: "Display name" }), "Cost reporter");
    await user.click(within(dialog).getByRole("checkbox", { name: /Archive\.Read/ }));
    expect(submit).toBeDisabled();
    await user.type(within(dialog).getByRole("searchbox", { name: "Owner" }), "riley");
    await user.click(await within(dialog).findByRole("button", { name: /Riley Moreno/ }));
    expect(
      within(dialog).getByText(/proposal from it is refused unless the owner/),
    ).toBeInTheDocument();
    await user.click(submit);
    const table = await screen.findByRole("table", { name: "API clients" });
    expect(await within(table).findByText("Cost reporter")).toBeInTheDocument();
    const sent = requestLog.find(
      (r) => r.method === "POST" && r.path === "/v1/settings/api-clients",
    );
    expect(sent?.body).toEqual({
      app_id: "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
      display_name: "Cost reporter",
      scopes: ["Archive.Read"],
      owner_identity_id: "9003",
    });
  });

  it("disables at once with If-Match, then revokes after a confirmation", async () => {
    const { user, container } = renderApp("/settings/api-clients/3");
    expect(await screen.findByRole("heading", { name: "Migration planner" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Disable" }));
    expect(await screen.findByRole("button", { name: "Enable" })).toBeInTheDocument();
    const patch = requestLog.find((r) => r.method === "PATCH");
    expect(patch?.body).toEqual({ enabled: false });
    expect(patch?.headers["if-match"]).toBe('"1"');
    await user.click(screen.getByRole("button", { name: "Revoke…" }));
    const dialog = screen.getByRole("dialog", { name: /Revoke Migration planner/ });
    await user.click(within(dialog).getByRole("button", { name: "Revoke client" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Enable" })).not.toBeInTheDocument(),
    );
    expect(screen.getAllByText("Revoked").length).toBeGreaterThan(0);
    expect(requestLog.some((r) => r.path === "/v1/settings/api-clients/3/revoke")).toBe(true);
    await expectNoAxeViolations(container);
  });
});
