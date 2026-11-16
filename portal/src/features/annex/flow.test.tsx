import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "../../mocks/server";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("Settings, Filers: the flow", () => {
  it("draws filer, edge, annex and Azure for each filer, with its state", async () => {
    const { container } = renderApp("/settings/filers", { persona: "admin" });
    const legend = await screen.findByRole("list", { name: "What an annex needs" });
    expect(within(legend).getAllByRole("listitem")).toHaveLength(4);
    expect(await within(legend).findByText(/3 of 5 online/)).toBeInTheDocument();
    // ontap01: mounted, on an online edge, one annex into Azure.
    const ontap = await screen.findByRole("list", { name: "ontap01: what it needs" });
    expect(within(ontap).getByText("Mounted")).toBeInTheDocument();
    expect(within(ontap).getByText("Read-only")).toBeInTheDocument();
    expect(within(ontap).getByText("sec=krb5")).toBeInTheDocument();
    expect(within(ontap).getByText("edge-houston-1")).toBeInTheDocument();
    expect(await within(ontap).findByRole("link", { name: /long-term-storage/ })).toHaveAttribute(
      "href",
      "/annexes/7",
    );
    expect(within(ontap).getByText("dataset geo-annex")).toBeInTheDocument();
    // isilon02: its edge isn't reporting and its mount failed, so what follows is locked.
    const isilon = screen.getByRole("list", { name: "isilon02: what it needs" });
    expect(within(isilon).getByText("Mount failed")).toBeInTheDocument();
    expect(within(isilon).getByText("mount timed out")).toBeInTheDocument();
    expect(within(isilon).getByText("Not reporting")).toBeInTheDocument();
    expect(within(isilon).getByRole("link", { name: "Deploy an edge" })).toHaveAttribute(
      "href",
      "/edges/new",
    );
    expect(within(isilon).getByText("No annex yet")).toBeInTheDocument();
    const locked = isilon.querySelectorAll('[data-tone="locked"]');
    expect(locked).toHaveLength(2);
    expect(within(isilon).getAllByText(/Locked until the step before it is done/)).toHaveLength(2);
    await expectNoAxeViolations(container);
  });

  it("with no edge online, says to deploy one first and locks the rest", async () => {
    server.use(
      http.get("*/v1/edges", () =>
        HttpResponse.json({
          edges: [
            {
              edge_id: "5",
              name: "edge-new",
              state: "pending",
              status: "pending",
              software_version: null,
              kernel: null,
              guard_state: null,
              last_heartbeat_at: null,
              certificate_not_after: null,
              health: {},
            },
          ],
        }),
      ),
    );
    const { container } = renderApp("/settings/filers", { persona: "admin" });
    expect(await screen.findByText("Deploy an edge first.")).toBeInTheDocument();
    const cta = screen.getAllByRole("link", { name: "Deploy an edge" })[0];
    expect(cta).toHaveAttribute("href", "/edges/new");
    const legend = screen.getByRole("list", { name: "What an annex needs" });
    expect(within(legend).getByText("No edge is reporting.")).toBeInTheDocument();
    expect(legend.querySelectorAll('[data-tone="locked"]')).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Add a filer" })).toBeDisabled();
    await expectNoAxeViolations(container);
  });
});
