import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { requestLog } from "../../../mocks/handlers";
import { expectNoAxeViolations } from "../../../test/axe";
import { renderApp } from "../../../test/render";
import { reduceMotion } from "../../files/visual/testing";
import { MAX_DAY } from "./PolicyTimeline";

afterEach(() => vi.unstubAllGlobals());

const lastPreview = () =>
  requestLog.filter((r) => r.method === "POST" && r.path === "/v1/policies/preview").at(-1)
    ?.body as { body: Record<string, unknown> } | undefined;

async function timeline() {
  const tl = await screen.findByTestId("policy-timeline");
  return {
    tl,
    cool: within(tl).getByRole("slider", { name: "Move to Cool after, in days" }),
    cold: within(tl).getByRole("slider", { name: "Move to Cold after, in days" }),
    archive: within(tl).getByRole("slider", { name: "Move to Archive after, in days" }),
  };
}

describe("policy timeline", () => {
  it("draws the schedule with a slider per boundary, and passes axe", async () => {
    const { container } = renderApp("/policies/1", { persona: "admin" });
    const { tl, cool, cold, archive } = await timeline();
    expect(cool).toHaveAttribute("aria-valuenow", "30");
    expect(cool).toHaveAttribute("aria-valuemin", "0");
    expect(cool).toHaveAttribute("aria-valuemax", "119");
    expect(cold).toHaveAttribute("aria-valuemin", "31");
    expect(archive).toHaveAttribute("aria-valuemax", String(MAX_DAY));
    expect(cool).toHaveAttribute(
      "aria-valuetext",
      "Day 30: files move to Cool 30 days after the last read",
    );
    expect(
      within(tl).getByText("Days count from the last read. A dashed line marks one year."),
    ).toBeVisible();
    expect(within(tl).getByRole("list", { name: "Colour key for the timeline" })).toBeVisible();
    await expectNoAxeViolations(container);
  });

  it("moves a boundary by keyboard, bound to the same days as the inputs", async () => {
    const { user } = renderApp("/policies/1", { persona: "admin" });
    const { cool, cold, archive } = await timeline();
    cool.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByLabelText("Move to Cool after")).toHaveValue(31);
    await user.keyboard("{Shift>}{ArrowRight}{/Shift}");
    expect(screen.getByLabelText("Move to Cool after")).toHaveValue(41);
    await user.keyboard("{PageUp}");
    expect(cool).toHaveAttribute("aria-valuenow", "71");
    // End stops a day before Cold; Cold can't pass Archive either.
    await user.keyboard("{End}");
    expect(cool).toHaveAttribute("aria-valuenow", "119");
    archive.focus();
    await user.keyboard("{Home}");
    expect(screen.getByLabelText("Archive after")).toHaveValue(121);
    // Typing in the input moves the handle: the input is the alternative to dragging.
    const coldInput = screen.getByLabelText("Move to Cold after");
    await user.clear(coldInput);
    await user.type(coldInput, "100");
    // (A cleared day has no boundary, so the handle is drawn afresh.)
    expect(screen.getByRole("slider", { name: "Move to Cold after, in days" })).toHaveAttribute(
      "aria-valuenow",
      "100",
    );
    expect(cold).not.toBeInTheDocument();
    // The live preview asks with the dragged days.
    await waitFor(() =>
      expect(lastPreview()?.body).toMatchObject({ warm_day: 119, cold_day: 100, archive_day: 121 }),
    );
  });

  it("draws a minimum-duration warning on the boundary that causes it", async () => {
    const { user } = renderApp("/policies/1", { persona: "admin" });
    const { tl, cold } = await timeline();
    cold.focus();
    await user.keyboard("{Home}"); // Cool for one day: under its 30-day minimum
    await waitFor(() => expect(cold).toHaveAttribute("data-check", "warning"));
    const notes = within(tl).getByRole("list", { name: "On the timeline" });
    const note = within(notes).getByText(
      /Files stay in Cool for 1 days, but Azure bills Cool for at least 30 days/,
    );
    expect(cold.getAttribute("aria-describedby")).toContain(note.closest("li")?.id);
    expect(tl.querySelector('.tl-seg[data-tier="warm"]')).toHaveAttribute("data-short");
    // Moving back clears it.
    await user.keyboard("{PageUp}{PageUp}");
    await waitFor(() => expect(cold).not.toHaveAttribute("data-check"));
  });

  it("drags a boundary with the pointer", async () => {
    renderApp("/policies/1", { persona: "admin" });
    const { tl, cool } = await timeline();
    const track = tl.querySelector(".tl-track") as HTMLElement;
    vi.spyOn(track, "getBoundingClientRect").mockReturnValue({
      left: 0, top: 0, right: 1000, bottom: 54, width: 1000, height: 54, x: 0, y: 0,
      toJSON: () => ({}),
    }); // prettier-ignore
    // 500 days drawn (archive day 365 plus room), so 100 px of 1000 is day 50.
    fireEvent.pointerDown(cool, { pointerId: 1, clientX: 60 });
    fireEvent.pointerMove(cool, { pointerId: 1, clientX: 100 });
    fireEvent.pointerUp(cool, { pointerId: 1, clientX: 100 });
    expect(screen.getByLabelText("Move to Cool after")).toHaveValue(50);
    // Past Cold, it stops a day before it.
    fireEvent.pointerDown(cool, { pointerId: 1, clientX: 100 });
    fireEvent.pointerMove(cool, { pointerId: 1, clientX: 900 });
    fireEvent.pointerUp(cool, { pointerId: 1 });
    expect(cool).toHaveAttribute("aria-valuenow", "119");
  });

  it("shows Smart with only its archive day, counted from the write", async () => {
    const { user } = renderApp("/policies/1", { persona: "admin" });
    await timeline();
    await user.click(screen.getByRole("radio", { name: /Smart, then Archive on a fixed day/ }));
    const tl = screen.getByTestId("policy-timeline");
    expect(within(tl).getAllByRole("slider")).toHaveLength(1);
    const archive = within(tl).getByRole("slider", { name: "Move to Archive after, in days" });
    expect(archive).toHaveAttribute(
      "aria-valuetext",
      "Day 365: files move to Archive 365 days after the write",
    );
    expect(tl.querySelector('.tl-seg[data-tier="smart"]')).toHaveTextContent("Smart");
    expect(
      within(tl).getByText(/the provider moves each file among Hot, Cool and Cold by use/),
    ).toBeVisible();
    expect(within(tl).getByRole("list", { name: "Colour key for the timeline" })).toHaveTextContent(
      "SmartArchive",
    );
  });

  it("never clamps a typed day the handle didn't set", async () => {
    const { user } = renderApp("/policies/1", { persona: "admin" });
    await timeline();
    const input = screen.getByLabelText("Archive after");
    await user.clear(input);
    await user.type(input, "5000");
    const handle = screen.getByRole("slider", { name: "Move to Archive after, in days" });
    expect(handle).toHaveAttribute("aria-valuenow", "5000");
    handle.focus();
    await user.keyboard("{ArrowLeft}");
    expect(input).toHaveValue(4999);
  });

  it("is the same, still, with reduced motion", async () => {
    reduceMotion(true);
    renderApp("/policies/1", { persona: "admin" });
    const { tl } = await timeline();
    expect(tl).toHaveAttribute("data-motion", "reduce");
    const text = tl.textContent;
    cleanup();
    reduceMotion(false);
    renderApp("/policies/1", { persona: "admin" });
    const again = await timeline();
    expect(again.tl).toHaveAttribute("data-motion", "full");
    expect(again.tl.textContent).toBe(text);
  });
});
