import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";
import { UUIDV7_PATTERN } from "../../api/uuidv7";

const DATASET_DIR = "/files/1/101";

describe("file browser", () => {
  it("lists the shares a user can read, and passes axe", async () => {
    const { container } = renderApp("/files", { persona: "reader" });
    const list = await screen.findByRole("list", { name: "Shares" });
    expect(within(list).getByText("long-term-storage")).toBeInTheDocument();
    expect(within(list).queryByText("contracts-worm")).not.toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows tier, where the bytes are, the next step, pins and holds", async () => {
    const { container } = renderApp(DATASET_DIR, { persona: "reader" });
    const table = await screen.findByRole("table", { name: "Folder contents" });
    const archived = within(table).getByRole("row", { name: /line_1042\.segy/ });
    expect(within(archived).getByText("Archive")).toBeInTheDocument();
    expect(within(archived).getByText("(needs a recall to read)")).toBeInTheDocument();
    expect(within(archived).getByText("In the cloud")).toBeInTheDocument();
    expect(
      within(archived).getByRole("link", { name: /Recall in progress: Rehydrating/ }),
    ).toHaveAttribute("href", "/recalls/501");
    const pinned = within(table).getByRole("row", { name: /line_1044\.segy/ });
    expect(within(pinned).getByText(/Hot until/)).toBeInTheDocument();
    const kept = within(table).getByRole("row", { name: /survey_notes\.docx/ });
    expect(within(kept).getByText(/Kept online until/)).toBeInTheDocument();
    const legal = within(table).getByRole("row", { name: /line_0999\.segy/ });
    expect(within(legal).getByText("Legal hold")).toBeInTheDocument();
    // A deleted file is not in the live view.
    expect(within(table).queryByText("line_0950.segy")).not.toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("hides pin and keep-online actions from a plain reader", async () => {
    const { user } = renderApp(DATASET_DIR, { persona: "reader" });
    await user.click(await screen.findByRole("checkbox", { name: "Select line_1043.segy" }));
    const bar = screen.getByRole("region", { name: "Selection actions" });
    expect(within(bar).getByRole("button", { name: /Recall/ })).toBeEnabled();
    expect(within(bar).queryByRole("button", { name: /Pin to a tier/ })).not.toBeInTheDocument();
    expect(within(bar).queryByRole("button", { name: /Keep online/ })).not.toBeInTheDocument();
  });

  it("recalls ahead with options and ETA, then shows the four steps", async () => {
    const { user, router } = renderApp(DATASET_DIR, { persona: "reader" });
    await user.click(await screen.findByRole("checkbox", { name: "Select line_1043.segy" }));
    await user.click(screen.getByRole("button", { name: /Recall…/ }));
    const dialog = await screen.findByRole("dialog", { name: "Recall line_1043.segy" });
    // 8.2 GB: under the no-promise size, so both priorities are offered with an ETA.
    const high = await within(dialog).findByRole("radio", { name: /High priority/ });
    expect(high).toBeEnabled();
    expect(within(dialog).getAllByText(/provider estimate/).length).toBeGreaterThan(0);
    await user.click(high);
    await user.click(within(dialog).getByRole("button", { name: "Recall file" }));

    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/recalls\/\d+$/));
    const steps = await screen.findByRole("list", { name: "Recall progress" });
    expect(within(steps).getAllByRole("listitem")).toHaveLength(4);
    expect(within(steps).getByText("Requested")).toBeInTheDocument();
    expect(within(steps).getByText("Available")).toBeInTheDocument();

    const post = requestLog.find((r) => r.method === "POST" && r.path === "/v1/recalls");
    expect(post?.body).toMatchObject({ file_id: "1002", priority: "high" });
    expect(post?.headers["idempotency-key"]).toMatch(UUIDV7_PATTERN);
    expect(post?.headers["x-csrf-token"]).toBe("mock-csrf-reader");
  });

  it("does not offer High where it can't be faster, and promises no time above 10 GB", async () => {
    const { user } = renderApp(`${DATASET_DIR}?file=1007`, { persona: "reader" });
    const drawer = await screen.findByRole("complementary", { name: "line_0999.segy" });
    await user.click(await within(drawer).findByRole("button", { name: /Recall ahead of time/ }));
    const dialog = await screen.findByRole("dialog", { name: "Recall line_0999.segy" });
    expect(await within(dialog).findByRole("radio", { name: /High priority/ })).toBeDisabled();
    expect(within(dialog).getByText(/wouldn't be faster/)).toBeInTheDocument();
    expect(within(dialog).getByText(/No time promised for files over 10 GB/)).toBeInTheDocument();
  });

  it("shows the preview warnings before a pin can be applied", async () => {
    const { user } = renderApp(DATASET_DIR, { persona: "steward" });
    await user.click(await screen.findByRole("checkbox", { name: "Select line_1045.segy" }));
    await user.click(screen.getByRole("button", { name: /Pin to a tier/ }));
    const dialog = await screen.findByRole("dialog", { name: /Pin line_1045\.segy to a tier/ });
    await user.click(within(dialog).getByRole("radio", { name: /Hot/ }));
    await user.type(within(dialog).getByRole("textbox", { name: "Why?" }), "Reprocessing");

    // Cold since 20 days: moving it now costs an early-deletion fee.
    const warnings = await within(dialog).findByRole("list", { name: "Pin warnings" });
    expect(await within(warnings).findByText(/early-deletion fee/)).toBeInTheDocument();
    const apply = within(dialog).getByRole("button", { name: "Apply pin" });
    expect(apply).toBeDisabled();
    await user.click(within(dialog).getByRole("checkbox", { name: /I've read the warnings/ }));
    await waitFor(() => expect(apply).toBeEnabled());
    await user.click(apply);
    await waitFor(() =>
      expect(requestLog.some((r) => r.method === "POST" && r.path === "/v1/pins")).toBe(true),
    );
    const post = requestLog.find((r) => r.method === "POST" && r.path === "/v1/pins");
    expect(post?.body).toMatchObject({ items: ["1004"], tier: "hot", reason: "Reprocessing" });
    const previews = requestLog.filter((r) => r.path === "/v1/pins/preview");
    expect(previews.length).toBeGreaterThan(0);
    expect(previews.every((p) => !p.headers["idempotency-key"])).toBe(true);
  });

  it("keeps a file online for 14 days by default", async () => {
    const { user } = renderApp(DATASET_DIR, { persona: "steward" });
    await user.click(await screen.findByRole("checkbox", { name: "Select nav_2026.p190" }));
    await user.click(screen.getByRole("button", { name: /Keep online/ }));
    const dialog = await screen.findByRole("dialog", { name: "Keep nav_2026.p190 online" });
    expect(within(dialog).getByText(/no provider fee/)).toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "Why?" }), "Field crew");
    await user.click(within(dialog).getByRole("button", { name: "Keep online" }));
    await waitFor(() =>
      expect(requestLog.some((r) => r.method === "POST" && r.path === "/v1/holds")).toBe(true),
    );
    const body = requestLog.find((r) => r.path === "/v1/holds")?.body as {
      kind: string;
      until: string;
    };
    expect(body.kind).toBe("keep_online");
    const days = (new Date(body.until).getTime() - Date.now()) / 86_400_000;
    expect(days).toBeGreaterThan(13.9);
    expect(days).toBeLessThan(14.1);
  });
});

describe("rewind", () => {
  it("browses as of a date inside the share's depth, including files deleted since", async () => {
    const asOf = new Date(Date.now() - 10 * 86_400_000).toISOString();
    const { container } = renderApp(`${DATASET_DIR}?as_of=${encodeURIComponent(asOf)}`, {
      persona: "reader",
    });
    const table = await screen.findByRole("table", { name: /as of the chosen time/ });
    const deleted = within(table).getByRole("row", { name: /line_0950\.segy/ });
    expect(within(deleted).getByText("Deleted since")).toBeInTheDocument();
    const changed = within(table).getByRole("row", { name: /survey_notes\.docx/ });
    expect(within(changed).getByText("Changed since")).toBeInTheDocument();
    const picker = screen.getByLabelText("Show this folder as it was on");
    expect(picker).toHaveAttribute("min");
    expect(picker).toHaveAttribute("max");
    await expectNoAxeViolations(container);
  });

  it("explains a date beyond the rewind depth", async () => {
    const asOf = new Date(Date.now() - 60 * 86_400_000).toISOString();
    renderApp(`${DATASET_DIR}?as_of=${encodeURIComponent(asOf)}`, { persona: "reader" });
    expect(await screen.findByText(/further back than this share keeps/)).toBeInTheDocument();
  });

  it("offers decision 34's three restores, with the resulting names worked out", async () => {
    const { user } = renderApp(`${DATASET_DIR}?file=1006`, { persona: "steward" });
    const versions = await screen.findByRole("list", { name: "Versions of survey_notes.docx" });
    expect(within(versions).getAllByRole("listitem")).toHaveLength(4);
    const restore = within(versions).getAllByRole("button", { name: /Restore the version/ })[0];
    if (!restore) throw new Error("no restore button");
    await user.click(restore);
    const dialog = await screen.findByRole("dialog", { name: "Restore survey_notes.docx" });
    const replace = within(dialog).getByRole("radio", { name: /Replace the current file/ });
    expect(replace).toBeChecked();
    expect(
      within(dialog).getByText(/kept as an older version, so nothing is lost/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("radio", { name: /^Restore as survey_notes-restored\.docx/ }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("radio", {
        name: /^Restore as survey_notes-restored-\d{4}-\d{2}-\d{2}-\d{6}Z\.docx/,
      }),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Restore" }));
    expect(await within(dialog).findByRole("status")).toBeInTheDocument();
    const post = requestLog.find((r) => r.path.endsWith("/restore"));
    expect(post?.body).toEqual({ target: "overwrite" });
  });

  it("says when a restored name is taken and offers the other options", async () => {
    const { user, db } = renderApp(`${DATASET_DIR}?file=1006`, { persona: "steward" });
    const clash = db.files.get("1006");
    if (!clash) throw new Error("no fixture");
    db.files.set("1099", {
      ...clash,
      file_id: "1099",
      name: "survey_notes-restored.docx",
      versions: [],
    });
    const versions = await screen.findByRole("list", { name: "Versions of survey_notes.docx" });
    const restore = within(versions).getAllByRole("button", { name: /Restore the version/ })[0];
    if (!restore) throw new Error("no restore button");
    await user.click(restore);
    const dialog = await screen.findByRole("dialog", { name: "Restore survey_notes.docx" });
    await user.click(
      within(dialog).getByRole("radio", { name: /^Restore as survey_notes-restored\.docx/ }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Restore" }));
    expect(await within(dialog).findByText("That name is taken")).toBeInTheDocument();
    expect(
      within(dialog).getByText(/survey_notes-restored\.docx already exists in this folder/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("radio", { name: /^Restore as survey_notes-restored\.docx/ }),
    ).toBeDisabled();
    // The next free option is chosen; restoring with it works.
    const stamped = within(dialog).getByRole("radio", { name: /restored-\d{4}-/ });
    expect(stamped).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(requestLog.filter((r) => r.path.endsWith("/restore")).map((r) => r.body)).toEqual([
        { target: "restored" },
        { target: "restored_timestamped" },
      ]),
    );
  });

  it("restores a deleted file only under a new name", async () => {
    const { user } = renderApp(`${DATASET_DIR}?file=1009`, { persona: "steward" });
    const versions = await screen.findByRole("list", { name: "Versions of line_0950.segy" });
    expect(within(versions).getByText("Deleted")).toBeInTheDocument();
    const restore = within(versions).getAllByRole("button", { name: /Restore the version/ })[0];
    if (!restore) throw new Error("no restore button");
    await user.click(restore);
    const dialog = await screen.findByRole("dialog", { name: "Restore line_0950.segy" });
    expect(within(dialog).getByRole("radio", { name: /Replace the current file/ })).toBeDisabled();
    expect(
      within(dialog).getByRole("radio", { name: /^Restore as line_0950-restored\.segy/ }),
    ).toBeChecked();
  });

  it("lets the setter of a keep-online hold release it, by identity", async () => {
    const { user } = renderApp(`${DATASET_DIR}?file=1006`, { persona: "reader" });
    const drawer = await screen.findByRole("complementary", { name: "survey_notes.docx" });
    // Riley (a Reader) set this hold from Teams; Readers can't set holds in the portal.
    const release = await within(drawer).findByRole("button", { name: "Release" });
    expect(within(drawer).queryByRole("button", { name: /Keep online/ })).not.toBeInTheDocument();
    await user.click(release);
    await waitFor(() =>
      expect(requestLog.some((r) => r.path === "/v1/holds/851/release")).toBe(true),
    );
    expect(requestLog.some((r) => r.path.startsWith("/v1/holds?file_id=1006"))).toBe(true);
  });

  it("downloads a version through the worker's short-lived link", async () => {
    const { user } = renderApp(`${DATASET_DIR}?file=1006`, { persona: "reader" });
    const versions = await screen.findByRole("list", { name: "Versions of survey_notes.docx" });
    expect(
      within(versions).queryByRole("button", { name: /Restore the version/ }),
    ).not.toBeInTheDocument();
    const download = within(versions).getAllByRole("button", { name: /Download the version/ })[0];
    if (!download) throw new Error("no download button");
    await user.click(download);
    expect(
      await screen.findByText(/Download started/, undefined, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(requestLog.some((r) => r.method === "GET" && r.path.startsWith("/v1/downloads/"))).toBe(
      true,
    );
  });
});
