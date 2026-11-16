import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("activity", () => {
  it("shows uploads and recalls per day to an ArchiveAdmin", async () => {
    const { container, user } = renderApp("/activity", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Daily activity" });
    expect(within(table).getAllByRole("row")).toHaveLength(31); // header + 30 days
    expect(screen.getByRole("img", { name: /Bytes uploaded per day/ })).toBeInTheDocument();
    expect(screen.getByText("Waiting for read-back")).toBeInTheDocument();
    expect(screen.getByText(/5\.0 h \(90% within 14 h\)/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
    await user.selectOptions(screen.getByLabelText("Window"), "7");
    await screen.findByRole("table", { name: "Daily activity" });
    expect(
      requestLog.some((r) => r.path.includes("/v1/activity") && r.path.includes("days=7")),
    ).toBe(true);
    expect(
      await within(screen.getByRole("table", { name: "Daily activity" })).findAllByRole("row"),
    ).toHaveLength(8);
  });

  it("is not in the menu for anyone else", async () => {
    renderApp("/files", { persona: "steward" });
    await screen.findByRole("navigation");
    expect(screen.queryByRole("link", { name: /Activity/ })).not.toBeInTheDocument();
  });
});
