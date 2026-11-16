import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { expectNoAxeViolations } from "../test/axe";
import { renderApp } from "../test/render";

/** WP17's shell: grouped navigation, the section crumb and the ⌘K box. */
describe("the shell", () => {
  it("groups the sidebar under Overview, Data, Operations and Admin for an ArchiveAdmin", async () => {
    const { container } = renderApp("/files", { persona: "admin" });
    const nav = await screen.findByRole("navigation", { name: "Main" });
    const text = nav.textContent;
    const order = ["Overview", "Files", "Activity", "Data", "Shares", "Policies", "Annexes"];
    order.push("Migrations", "Operations", "Recalls", "Proposals", "Audit", "Edges", "Admin");
    order.push("Settings");
    let at = -1;
    for (const word of order) {
      const next = text.indexOf(word, at + 1);
      expect(next, word).toBeGreaterThan(at);
      at = next;
    }
    expect(within(nav).getAllByRole("link")).toHaveLength(11);
    await expectNoAxeViolations(container);
  });

  it("draws only the sections a Reader has items in", async () => {
    renderApp("/files", { persona: "reader" });
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByText("Overview")).toBeInTheDocument();
    expect(within(nav).getByText("Operations")).toBeInTheDocument();
    expect(within(nav).queryByText("Data")).not.toBeInTheDocument();
    expect(within(nav).queryByText("Admin")).not.toBeInTheDocument();
  });

  it("names the section in the top bar, linking back to its first page", async () => {
    renderApp("/annexes/7", { persona: "admin" });
    const crumbs = await screen.findByRole("list", { name: "You are in" });
    expect(within(crumbs).getByText("Data")).toBeInTheDocument();
    expect(within(crumbs).getByRole("link", { name: "Annexes" })).toHaveAttribute(
      "href",
      "/annexes",
    );
  });

  it("jumps to a page from the search box, by ⌘K, arrows and Enter", async () => {
    const { user, router } = renderApp("/files", { persona: "admin" });
    const box = await screen.findByRole("combobox", { name: "Go to a page" });
    expect(box).toHaveAttribute("aria-expanded", "false");
    await user.keyboard("{Meta>}k{/Meta}");
    expect(box).toHaveFocus();
    await user.type(box, "migr");
    const list = screen.getByRole("listbox", { name: "Pages" });
    const options = within(list).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      "MigrationsData",
      "New migrationData",
      "Migration agentsData",
      "Migration plansData",
    ]);
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/migrations/agents"));
    expect(box).toHaveValue("");
  });

  it("offers a Reader only the pages a Reader can open", async () => {
    const { user } = renderApp("/files", { persona: "reader" });
    const box = await screen.findByRole("combobox", { name: "Go to a page" });
    await user.click(box);
    const names = within(screen.getByRole("listbox", { name: "Pages" }))
      .getAllByRole("option")
      .map((o) => o.firstChild?.textContent);
    expect(names).toEqual(["Files", "Recalls"]);
    await user.type(box, "settings");
    expect(screen.queryByRole("listbox", { name: "Pages" })).not.toBeInTheDocument();
  });
});
