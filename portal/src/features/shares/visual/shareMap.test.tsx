import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { example } from "../../../mocks/examples.gen";
import { requestLog } from "../../../mocks/handlers";
import { server } from "../../../mocks/server";
import { expectNoAxeViolations } from "../../../test/axe";
import { renderApp } from "../../../test/render";
import { reduceMotion } from "../../files/visual/testing";

afterEach(() => vi.unstubAllGlobals());

async function theMap() {
  const card = await screen.findByRole("region", { name: "Where this share's data is" });
  await within(card).findByTestId("share-map");
  return card;
}

describe("share map", () => {
  it("shows where the share's files are, per edge state and tier, and passes axe", async () => {
    const { container } = renderApp("/shares/1", { persona: "admin" });
    const card = await theMap();
    expect(
      within(card).getByText(/Counts cover every file in the share\. The lists below name only/),
    ).toBeInTheDocument();
    // The roll-up's time, plainly.
    expect(await within(card).findByText(/^As of /)).toBeVisible();
    const gateway = within(card).getByRole("group", { name: "The gateway" });
    expect(within(gateway).getByRole("button", { name: /Whole files.+7 files/ })).toBeVisible();
    expect(within(gateway).getByRole("button", { name: /Stubs.+4 files/ })).toBeVisible();
    const live = within(card).getByRole("group", { name: "Live account, Azure Blob" });
    expect(within(live).getByRole("button", { name: /Hot \(Azure Hot\).+4 files/ })).toBeVisible();
    expect(
      within(live).getByRole("button", { name: /Cool \(Azure Cool\).+1 file\b/ }),
    ).toBeVisible();
    const archive = within(card).getByRole("group", { name: "Archive account, Azure Blob" });
    expect(within(archive).getByRole("button", { name: /Archive.+5 files/ })).toBeVisible();
    // Recalls in flight, by step.
    expect(within(card).getByText("2 recalls coming back")).toBeVisible();
    expect(
      within(card).getByRole("img", {
        name: /Archive to the gateway: 0 requested, 1 rehydrating, 1 downloading/,
      }),
    ).toBeVisible();
    expect(within(card).getByRole("list", { name: "Colour key for the map" })).toBeVisible();
    await expectNoAxeViolations(container);
  });

  it("opens a node's files, linked into the browser", async () => {
    const { user, router } = renderApp("/shares/1", { persona: "admin" });
    const card = await theMap();
    const node = within(card).getByRole("button", { name: /Archive.+5 files/ });
    await user.click(node);
    expect(node).toHaveAttribute("aria-pressed", "true");
    expect(router.state.location.search).toBe("?node=tier%3Aarchive%3Ahome");
    const table = await within(card).findByRole("table", { name: "Files: Archive in Azure Blob" });
    const link = within(table).getByRole("link", { name: "line_1042.segy" });
    expect(link).toHaveAttribute("href", "/files/1/101?file=1001");
    expect(within(table).getAllByRole("row")).toHaveLength(6);
    expect(
      requestLog.some((r) => r.path.startsWith("/v1/shares/1/map/files?tier=archive&role=home")),
    ).toBe(true);
    // Clicking it again closes the list.
    await user.click(node);
    await waitFor(() => expect(within(card).queryByRole("table")).not.toBeInTheDocument());
  });

  it("gives a DataSteward whole-share counts, without names (owner, 2026-11-04)", async () => {
    renderApp("/shares/1", { persona: "steward" });
    const card = await theMap();
    expect(within(card).getByText(/^Counts cover every file in the share/)).toBeVisible();
    expect(
      within(card).getByRole("button", { name: /Archive \(Azure Archive\).+5 files/ }),
    ).toBeVisible();
  });

  it("says counting, never zero, before the first count", async () => {
    server.use(
      http.get("*/v1/shares/:id/map", () =>
        HttpResponse.json({
          ...example.ShareMap,
          share_id: "1",
          scope: "all",
          basis: "counting",
          computed_at: null,
          edge: null,
          stores: [],
          source: null,
          migration_state: "steady",
          recalls: { open: 0, by_step: { requested: 0, rehydrating: 0, downloading: 0 } },
          migrations: [],
        }),
      ),
    );
    renderApp("/shares/1", { persona: "admin" });
    const card = await theMap();
    expect(
      within(card).getByText(
        "Counting this share's files. The totals show once the service has counted them.",
      ),
    ).toBeVisible();
    const gateway = within(card).getByRole("group", { name: "The gateway" });
    expect(
      within(gateway).getByRole("button", {
        name: "Whole files, open instantly: not counted. Show these files.",
      }),
    ).toBeVisible();
    expect(within(card).queryByText(/^As of /)).not.toBeInTheDocument();
  });

  it("says when counts are limited to what the caller can read (RBAC)", async () => {
    server.use(
      http.get("*/v1/shares/:id/map", () =>
        HttpResponse.json({
          ...example.ShareMap,
          share_id: "1",
          dataset_id: "1",
          scope: "readable",
          migrations: null,
        }),
      ),
    );
    renderApp("/shares/1", { persona: "steward" });
    const card = await theMap();
    expect(within(card).getByText("Counts cover only the files you can read.")).toBeVisible();
    // The S3 source of a migrating dataset, without the ArchiveAdmin-only migration counts.
    const source = within(card).getByRole("group", { name: "Migration source, Amazon S3" });
    expect(
      within(source).getByRole("button", {
        name: /Archive \(S3 Glacier Flexible Retrieval\).+300 files/,
      }),
    ).toBeVisible();
    expect(within(card).getByText("Migrating from Amazon S3 to Azure Blob")).toBeVisible();
    expect(within(card).getByRole("img", { name: "Migration in progress" })).toBeVisible();
    expect(within(card).queryByText(/\/\d+$/)).not.toBeInTheDocument();
  });

  it("gives an ArchiveAdmin the migrations in flight", async () => {
    server.use(
      http.get("*/v1/shares/:id/map", () =>
        HttpResponse.json({
          ...example.ShareMap,
          share_id: "1",
          dataset_id: "1",
          scope: "all",
          migrations: [
            {
              migration_id: "5",
              state: "copying",
              prefix: "raw/2019/",
              files: { total: 15000, verified: 9000 },
            },
          ],
        }),
      ),
    );
    renderApp("/shares/1", { persona: "admin" });
    const card = await theMap();
    expect(
      within(card).getByRole("img", { name: "raw/2019/: 9000 of 15000 verified, copying" }),
    ).toBeVisible();
  });

  it("is still with reduced motion, and says the same", async () => {
    reduceMotion(false);
    renderApp("/shares/1", { persona: "admin" });
    let card = await theMap();
    expect(within(card).getByTestId("share-map")).toHaveAttribute("data-motion", "full");
    const text = within(card).getByTestId("share-map").textContent;
    cleanup();
    reduceMotion(true);
    renderApp("/shares/1", { persona: "admin" });
    card = await theMap();
    expect(within(card).getByTestId("share-map")).toHaveAttribute("data-motion", "reduce");
    expect(within(card).getByTestId("share-map").textContent).toBe(text);
  });
});
