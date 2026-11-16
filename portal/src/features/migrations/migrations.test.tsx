import { screen, within } from "@testing-library/react";
import { http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "../../mocks/server";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("migrations", () => {
  it("shows prefixes being moved and their sources to an ArchiveAdmin", async () => {
    const { container } = renderApp("/migrations", { persona: "admin" });
    const prefixes = await screen.findByRole("table", { name: "Prefixes being moved" });
    expect(within(prefixes).getByText("Copying")).toBeInTheDocument();
    expect(within(prefixes).getByText("Every file verified")).toBeInTheDocument();
    expect(within(prefixes).getByText("Done: served from the new backend")).toBeInTheDocument();
    expect(within(prefixes).getByLabelText("9100 of 15000 files verified")).toBeInTheDocument();
    // Failed / excluded, per migration.
    expect(within(prefixes).getByText("2 / 0")).toBeInTheDocument();
    expect(within(prefixes).getByText("2 / 2")).toBeInTheDocument();
    // An external copier is "API", never a product name (owner, 2026-11-04).
    expect(within(prefixes).getByText("API (an external copier)")).toBeInTheDocument();
    expect(within(prefixes).getAllByText("Azure Storage Mover")).toHaveLength(2);
    expect(container.textContent).not.toMatch(/Prizmanyx/i);
    // Each prefix opens its migration.
    expect(within(prefixes).getByRole("link", { name: "raw/2019/" })).toHaveAttribute(
      "href",
      "/migrations/5",
    );
    const sources = await screen.findByRole("table", { name: "Sources" });
    expect(within(sources).getByText("acme-seismic/raw/2019/")).toBeInTheDocument();
    expect(
      within(sources).getByText(/older than the bucket's event queue keeps/),
    ).toBeInTheDocument();
    // A source with no migration yet can start one; one that has one can't.
    expect(
      within(sources).getByRole("link", { name: "Start the migration of acme-seismic/raw/2021/" }),
    ).toHaveAttribute("href", "/migrations/new?source=9");
    expect(
      within(sources).queryByRole("link", {
        name: "Start the migration of acme-seismic/raw/2019/",
      }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New migration" })).toHaveAttribute(
      "href",
      "/migrations/new",
    );
    await expectNoAxeViolations(container);
  });

  it("offers Start migration from the server's count, not from one page of migrations", async () => {
    // Review of #41: the offer came from the first 500 migrations listed.
    server.use(http.get("*/v1/migrations", () => Response.json({ items: [], next_cursor: "x" })));
    renderApp("/migrations", { persona: "admin" });
    const sources = await screen.findByRole("table", { name: "Sources" });
    // Source 3 has migrations the page didn't list: the server says so.
    expect(
      within(sources).queryByRole("link", {
        name: "Start the migration of acme-seismic/raw/2019/",
      }),
    ).not.toBeInTheDocument();
    expect(
      within(sources).getByRole("link", { name: "Start the migration of acme-seismic/raw/2021/" }),
    ).toBeInTheDocument();
  });

  it("is not in the menu for anyone else", async () => {
    renderApp("/files", { persona: "steward" });
    await screen.findByRole("navigation");
    expect(screen.queryByRole("link", { name: /Migrations/ })).not.toBeInTheDocument();
  });

  it("tells anyone else the wizard is for ArchiveAdmin", async () => {
    renderApp("/migrations/new", { persona: "reader" });
    expect(await screen.findByText("Migrations are for ArchiveAdmin.")).toBeInTheDocument();
  });

  it("tells anyone else a migration's page is for ArchiveAdmin", async () => {
    renderApp("/migrations/5", { persona: "steward" });
    expect(await screen.findByText("Migrations are for ArchiveAdmin.")).toBeInTheDocument();
  });
});
