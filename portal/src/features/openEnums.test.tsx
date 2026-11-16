import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  backendLabel,
  costItemLabel,
  migrationLabel,
  priorityLabel,
  stateLabel,
  tierLabel,
} from "../lib/format";
import { expectNoAxeViolations } from "../test/axe";
import { renderApp } from "../test/render";

// Decision 35: open response enums (x-extensible-enum) can gain values, such as
// a NetApp ONTAP backend or a handed_off migration. Unknown values show raw.
describe("open enums", () => {
  it("label known values and pass unknown ones through raw", () => {
    expect(tierLabel("warm")).toBe("Cool");
    expect(tierLabel("capacity")).toBe("capacity");
    expect(stateLabel("tiered")).toBe("tiered");
    expect(priorityLabel("bulk")).toBe("bulk");
    expect(backendLabel("netapp_ontap")).toBe("netapp_ontap");
    expect(migrationLabel("handed_off")).toBe("Handed off");
    expect(migrationLabel("frozen")).toBe("frozen");
    expect(costItemLabel("ontap_tiering")).toBe("ontap_tiering");
    expect(tierLabel("toString")).toBe("toString");
  });

  it("lists a dataset on an unknown backend, handed off", async () => {
    const { container } = renderApp("/policies", { persona: "admin" });
    const list = await screen.findByRole("list", { name: "Datasets" });
    const card = within(list).getByRole("link", { name: /Field archive \(ONTAP\)/ });
    expect(within(card).getByText(/netapp_ontap/)).toBeInTheDocument();
    expect(within(card).getByText(/ontap_fabricpool/)).toBeInTheDocument();
    expect(within(card).getByText("Handed off")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("opens the policy editor for it, with its own terms in the preview", async () => {
    const { container } = renderApp("/policies/3", { persona: "admin" });
    expect(
      await screen.findByText(/netapp_ontap · ontap_fabricpool · Handed off/),
    ).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: /ontap_fabricpool/ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Cost breakdown" })).toHaveTextContent(
      "ontap_tiering",
    );
    const checks = screen.getByRole("list", { name: "Checks" });
    expect(within(checks).getByText(/no prices for netapp_ontap/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows an unknown tier and file state raw in the file browser", async () => {
    const { container } = renderApp("/files/4/400?file=4001", { persona: "admin" });
    const table = await screen.findByRole("table", { name: "Folder contents" });
    const row = within(table).getByRole("row", { name: /core_scan_0007\.tif/ });
    expect(row).toHaveTextContent("capacity · FabricPool capacity tier");
    expect(within(row).getByText("tiered")).toBeInTheDocument();
    const drawer = await screen.findByRole("complementary", { name: "core_scan_0007.tif" });
    expect(within(drawer).getByText("tiered")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows an unknown recall priority raw", async () => {
    renderApp("/recalls/505", { persona: "admin" });
    expect(await screen.findByText(/bulk · requested/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Show in folder" })).toHaveAttribute(
      "href",
      "/files/4/400?file=4001",
    );
  });
});
