import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

describe("recalls", () => {
  it("shows the four steps with the current one marked", async () => {
    const { container } = renderApp("/recalls/502", { persona: "reader" });
    const steps = await screen.findByRole("list", { name: "Recall progress" });
    const items = within(steps).getAllByRole("listitem");
    expect(items.map((i) => i.dataset.status)).toEqual(["done", "done", "current", "pending"]);
    expect(items[2]).toHaveAttribute("aria-current", "step");
    expect(screen.getByText(/provider estimate/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("promises no time above 10 GB", async () => {
    renderApp("/recalls/501", { persona: "reader" });
    expect(await screen.findByText(/No time promised/)).toBeInTheDocument();
    expect(screen.getByText(/You and 1 other person/)).toBeInTheDocument();
  });

  it("offers to recall again after a failure", async () => {
    const { user } = renderApp("/recalls/504", { persona: "reader" });
    const steps = await screen.findByRole("list", { name: "Recall progress" });
    expect(
      within(steps)
        .getAllByRole("listitem")
        .map((i) => i.dataset.status),
    ).toEqual(["done", "done", "failed", "pending"]);
    await user.click(screen.getByRole("button", { name: "Recall it again" }));
    expect(await screen.findByText("Recall started again.")).toBeInTheDocument();
    expect(
      requestLog.find((r) => r.method === "POST" && r.path === "/v1/recalls")?.body,
    ).toMatchObject({
      file_id: "1002",
    });
  });

  it("says a replaced file's recall stopped, not that it failed", async () => {
    const { container } = renderApp("/recalls/506", { persona: "reader" });
    const steps = await screen.findByRole("list", { name: "Recall progress" });
    expect(
      within(steps)
        .getAllByRole("listitem")
        .map((i) => i.dataset.status),
    ).toEqual(["done", "done", "pending", "pending"]);
    expect(screen.getByText("The recall stopped")).toBeInTheDocument();
    expect(
      screen.getAllByText(/The file was replaced while it was being recalled\./).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("File replaced")).toBeInTheDocument();
    // Nothing failed, so nothing offers to try again.
    expect(screen.queryByRole("button", { name: "Recall it again" })).not.toBeInTheDocument();
    expect(screen.queryByText(/didn't finish/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("lists a stopped recall by what happened to its file", async () => {
    const { container } = renderApp("/recalls", { persona: "reader" });
    const table = await screen.findByRole("table", { name: "Recalls" });
    expect(within(table).getByText("File replaced")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Stopped: file replaced or deleted/ })).toBeVisible();
    await expectNoAxeViolations(container);
  });

  it("lists my recalls, and everyone's only for an ArchiveAdmin", async () => {
    renderApp("/recalls", { persona: "reader" });
    expect(await screen.findByRole("table", { name: "Recalls" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Everyone's" })).not.toBeInTheDocument();
  });
});
