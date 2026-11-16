import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";
import { DEFAULT_BODY, bodyToForm, diffBodies, formToBody } from "./policyForm";

const previews = () =>
  requestLog.filter((r) => r.method === "POST" && r.path === "/v1/policies/preview");

describe("policy form", () => {
  it("round-trips a scheduled body", () => {
    expect(formToBody(bodyToForm(DEFAULT_BODY))).toEqual(DEFAULT_BODY);
  });

  it("drops Cool and Cold days for Smart, and counts from the write", () => {
    const body = formToBody({ ...bodyToForm(DEFAULT_BODY), mode: "smart" });
    expect(body).not.toHaveProperty("warm_day");
    expect(body).not.toHaveProperty("cold_day");
    expect(body.clock).toBe("written");
    expect(body.archive_day).toBe(365);
  });

  it("leaves out an empty archive day so the server can say it's required", () => {
    expect(formToBody({ ...bodyToForm(DEFAULT_BODY), archiveDay: "" })).not.toHaveProperty(
      "archive_day",
    );
  });

  it("diffs field by field, notify included", () => {
    const next = {
      ...DEFAULT_BODY,
      archive_day: 270,
      notify: { ...DEFAULT_BODY.notify, owners: true },
    };
    expect(diffBodies(DEFAULT_BODY, next)).toEqual([
      { field: "archive_day", from: 365, to: 270 },
      { field: "notify.owners", from: false, to: true },
    ]);
  });
});

describe("policy editor", () => {
  it("asks the four plain questions up front and keeps every knob under More settings", async () => {
    const { container } = renderApp("/policies/1", { persona: "admin" });
    for (const q of [
      "Which files does this cover?",
      "When should files move to cheaper storage?",
      "What do people see when they open an archived file?",
      "Who is told when a recall starts?",
    ]) {
      expect(await screen.findByRole("heading", { name: q })).toBeInTheDocument();
    }
    const more = screen.getByText("More settings").closest("details");
    expect(more).not.toHaveAttribute("open");
    for (const knob of [/Keep the first/, /free the copy again/, "Data size"]) {
      expect(screen.getByLabelText(knob)).toBeInTheDocument();
    }
    expect(
      await screen.findByRole("img", { name: /Days count from a file's last read/ }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("cost-per-tb")).toHaveTextContent("$");
    await expectNoAxeViolations(container);
  });

  it("re-previews 250 ms after the last edit, and the timeline, cost and checks follow", async () => {
    const { user } = renderApp("/policies/1", { persona: "admin" });
    const archive = await screen.findByLabelText("Archive after");
    await screen.findByTestId("cost-per-tb");
    const before = screen.getByTestId("cost-per-tb").textContent;
    const count = previews().length;

    await user.clear(archive);
    await user.type(archive, "200");
    // Typing "2", "20", "200" quickly gives one preview, not three.
    await waitFor(() => expect(previews().length).toBe(count + 1));
    expect((previews().at(-1)?.body as { body: { archive_day: number } }).body.archive_day).toBe(
      200,
    );
    await waitFor(() => expect(screen.getByTestId("cost-per-tb").textContent).not.toBe(before));
    expect(
      screen.getByRole("img", { name: /Archive \(Archive\)|Archive from day 200/ }),
    ).toBeInTheDocument();

    // Cold at 120 and Archive at 200: Cold's 90-day minimum isn't met.
    const checks = screen.getByRole("list", { name: "Checks" });
    expect(await within(checks).findByText(/bills Cold for at least 90 days/)).toBeInTheDocument();
    expect(screen.getByTestId("change-summary")).toHaveTextContent(
      "Archive after (days): 365 → 200",
    );
  });

  it("blocks saving while a check is an error, and links the check to its setting", async () => {
    const { user } = renderApp("/policies/1", { persona: "admin" });
    const cold = await screen.findByLabelText("Move to Cold after");
    await user.clear(cold);
    await user.type(cold, "10");
    const checks = await screen.findByRole("list", { name: "Checks" });
    const error = await within(checks).findByText(/must come after day 30/);
    expect(screen.getByRole("button", { name: /Save as version 5/ })).toBeDisabled();
    const item = error.closest("li");
    if (!item) throw new Error("no check item");
    await user.click(within(item).getByRole("button", { name: "Go to the setting" }));
    expect(cold).toHaveFocus();
  });

  it("saves a new active version from the active one (ArchiveAdmin)", async () => {
    const { user } = renderApp("/policies/1", { persona: "admin" });
    await user.click(await screen.findByRole("checkbox", { name: /The share's Owners/ }));
    const save = screen.getByRole("button", { name: "Save as version 5" });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    expect(await screen.findByText(/Version 5 is active/)).toBeInTheDocument();
    const post = requestLog.find(
      (r) => r.method === "POST" && r.path === "/v1/datasets/1/policies",
    );
    expect(post?.body).toMatchObject({ base_version: 4, body: { notify: { owners: true } } });
  });

  it("saves what people see on a share through the share settings", async () => {
    const { user } = renderApp("/policies/1", { persona: "admin" });
    const group = await screen.findByRole("group", { name: /On processing-scratch/ });
    await user.click(within(group).getByRole("radio", { name: /Tell them straight away/ }));
    await waitFor(() =>
      expect(
        previews().some((p) =>
          JSON.stringify(p.body).includes('"share_overrides":[{"share_id":"2","on_open":"fail"'),
        ),
      ).toBe(true),
    );
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(
        requestLog.some((r) => r.method === "PATCH" && r.path === "/v1/shares/2/settings"),
      ).toBe(true),
    );
    const patch = requestLog.find((r) => r.method === "PATCH");
    expect(patch?.headers["if-match"]).toBe('"6"');
    expect(patch?.body).toMatchObject({ on_open: "fail" });
  });

  it("lets a DataSteward propose, not save", async () => {
    const { user } = renderApp("/policies/1", { persona: "steward" });
    const archive = await screen.findByLabelText("Archive after");
    expect(screen.queryByRole("button", { name: /Save as version/ })).not.toBeInTheDocument();
    await user.clear(archive);
    await user.type(archive, "300");
    // Like Save, Propose waits for the preview of the current edit (and no errors).
    const propose = screen.getByRole("button", { name: /Propose this change/ });
    await waitFor(() => expect(propose).toBeEnabled());
    await user.click(propose);
    const dialog = await screen.findByRole("dialog", { name: "Propose this policy" });
    await user.type(
      within(dialog).getByRole("textbox", { name: "Why this change?" }),
      "Crews stop reading at 9 months",
    );
    await user.click(within(dialog).getByRole("button", { name: "Send proposal" }));
    await waitFor(() =>
      expect(
        requestLog.some((r) => r.method === "POST" && r.path === "/v1/policies/proposals"),
      ).toBe(true),
    );
    expect(requestLog.find((r) => r.path === "/v1/policies/proposals")?.body).toMatchObject({
      dataset_id: "1",
      base_version: 4,
      body: { archive_day: 300 },
    });
  });

  it("offers no Smart on a dataset with a WORM share", async () => {
    renderApp("/policies/2", { persona: "admin" });
    expect(await screen.findByRole("radio", { name: /Smart, then Archive/ })).toBeDisabled();
    expect(screen.getByText(/this dataset has a WORM share/)).toBeInTheDocument();
  });
});
