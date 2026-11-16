import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { setMockPriceDisplay } from "../../mocks/wp18";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

const sent = (method: string, path: string) =>
  requestLog.filter((r) => r.method === method && r.path === path);

describe("Settings, Prices and alerts", () => {
  it("chooses who sees prices, per role, with If-Match", async () => {
    const { container, user } = renderApp("/settings/prices-alerts", { persona: "admin" });
    const everyone = await screen.findByRole("radio", { name: "Everyone" });
    expect(everyone).toBeChecked();
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: "Admins only" }));
    expect(screen.getByRole("checkbox", { name: /Everyone else/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /DataStewards/ })).toBeChecked();
    // One click down: each role. Unticking DataStewards leaves ArchiveAdmins only.
    await user.click(screen.getByRole("checkbox", { name: /DataStewards/ }));
    expect(screen.getByRole("radio", { name: "ArchiveAdmins only" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/Who sees prices is saved/);
    const put = sent("PUT", "/v1/settings/prices");
    expect(put).toHaveLength(1);
    expect(put[0]?.body).toEqual({ show_to: ["ArchiveAdmin"] });
    expect(put[0]?.headers["if-match"]).toBe('"1"');
    expect(await screen.findByText(/Last changed by/)).toBeInTheDocument();
    // Nobody: a warning that it includes the admins.
    await user.click(screen.getByRole("radio", { name: "Nobody" }));
    expect(screen.getByText(/Nobody sees costs, ArchiveAdmins included/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("overrides a rate, resets one class, and resets all to list price", async () => {
    const { user } = renderApp("/settings/prices-alerts", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Azure Blob rates" });
    expect(within(table).queryByText(/^list /)).toBeNull();
    const hot = within(table).getByLabelText("Hot per gb-month");
    expect(hot).toHaveValue("0.0184");
    await user.clear(hot);
    await user.type(hot, "abc");
    expect(screen.getByText(/Each rate is a number/)).toBeInTheDocument();
    const azure = table.closest(".card") as HTMLElement;
    expect(within(azure).getByRole("button", { name: "Save rates" })).toBeDisabled();
    await user.clear(hot);
    await user.type(hot, "0.015");
    expect(within(table).getByText("list 0.0184")).toBeInTheDocument();
    await user.click(within(azure).getByRole("button", { name: "Save rates" }));
    await screen.findByText(/Azure Blob rates saved/);
    const put = sent("PUT", "/v1/price-tables/azure_blob");
    expect(put).toHaveLength(1);
    const body = put[0]?.body as {
      classes: Record<string, Record<string, string>>;
      list?: unknown;
    };
    expect(body.classes.Hot?.gb_month).toBe("0.015");
    expect(body.classes.Cool?.gb_month).toBe("0.01");
    expect(body.list).toBeUndefined(); // read-only: never sent back
    expect(put[0]?.headers["if-match"]).toBe('"0"');
    expect(await within(azure).findByText("Your rates")).toBeInTheDocument();
    // One class back to its list price.
    await user.click(within(table).getByRole("button", { name: "Reset Hot to list price" }));
    expect(within(table).getByLabelText("Hot per gb-month")).toHaveValue("0.0184");
    // The whole table back: DELETE with the override's ETag.
    await user.click(within(azure).getByRole("button", { name: "Reset all to list price" }));
    await screen.findByText(/Azure Blob is back on list prices/);
    const del = sent("DELETE", "/v1/price-tables/azure_blob");
    expect(del).toHaveLength(1);
    expect(del[0]?.headers["if-match"]).toBe('"1"');
    expect(await within(azure).findByText("List prices")).toBeInTheDocument();
  });

  it("hides every cost from a role the admin hid them from", async () => {
    const { db } = renderApp("/policies/1", { persona: "steward" });
    setMockPriceDisplay(db, ["ArchiveAdmin"]);
    expect(
      await screen.findByText("An ArchiveAdmin has hidden prices from your role."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("cost-per-tb")).toBeNull();
    // The mock, like the service, sends no amounts at all.
    await waitFor(() => expect(document.body.textContent).not.toMatch(/\$\d/));
  });

  it("shows the costs again to a role that may see them", async () => {
    renderApp("/policies/1", { persona: "steward" });
    expect(await screen.findByTestId("cost-per-tb")).toHaveTextContent("$");
  });
});
