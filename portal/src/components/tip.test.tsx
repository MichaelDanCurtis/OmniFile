import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { expectNoAxeViolations } from "../test/axe";
import { renderApp } from "../test/render";
import { Tip } from "./Tip";

describe("Tip: table detail a keyboard can reach (#60 review)", () => {
  it("is a button in the tab order, described by its detail, dismissed with Escape", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <p>
        <Tip label="Progress" tip="12 of 40 files, 1.2 GB of 4 GB">
          12 / 40
        </Tip>
      </p>,
    );
    await user.tab();
    const btn = screen.getByRole("button", { name: "Progress" });
    expect(btn).toHaveFocus();
    expect(btn).toHaveAccessibleDescription("12 of 40 files, 1.2 GB of 4 GB");
    expect(screen.getByRole("tooltip", { hidden: true })).toHaveTextContent("12 of 40 files");
    await user.keyboard("{Escape}");
    expect(container.querySelector(".tip")).toHaveAttribute("data-dismissed", "true");
    await user.tab({ shift: true });
    await user.tab();
    expect(container.querySelector(".tip")).not.toHaveAttribute("data-dismissed");
    await expectNoAxeViolations(container);
  });

  it("carries the migrations console's progress, ETA basis and wave start, with no title=", async () => {
    const { container } = renderApp("/migrations");
    const table = await screen.findByRole("table", { name: "Prefixes being moved" });
    const row = within(
      within(table).getByRole("link", { name: "raw/2019/batch-b/" }).closest("tr") as HTMLElement,
    );
    expect(row.getByRole("button", { name: "Progress" })).toHaveAccessibleDescription(
      /files, .+ of /,
    );
    expect(
      row.getByRole("button", { name: "How the ETA is worked out" }),
    ).toHaveAccessibleDescription(/the last hour's rate/);
    expect(row.getByRole("button", { name: "Starts" })).toHaveAccessibleDescription(/2026|2027/);
    expect(table.querySelector("[title]")).toBeNull();
    await expectNoAxeViolations(container);
  });
});
