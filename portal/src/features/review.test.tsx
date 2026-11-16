import { act, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { browserNav } from "../api/client";
import { requestLog } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

const problem = (status: number, code: string, detail: string) =>
  HttpResponse.json(
    { type: `urn:omnifile:error:${code}`, title: code, status, code, detail, retryable: false },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );

describe("review fixes", () => {
  it("doesn't offer Approve on your own proposal, but keeps Reject and Withdraw", async () => {
    const { db } = renderApp("/proposals/702", { persona: "admin" });
    const entry = db.proposals.find((p) => p.proposal.proposal_id === "702");
    if (!entry) throw new Error("no fixture");
    entry.proposer = "admin";
    entry.proposal.proposed_by = { ...entry.proposal.proposed_by, identity_id: "9001" };
    expect(await screen.findByText(/another ArchiveAdmin has to approve it/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument();
  });

  it("sends you to the folder for an inherited hold instead of releasing it", async () => {
    const { db } = renderApp("/files/1/101?file=1003", { persona: "admin" });
    db.holds.push({
      hold_id: "860",
      file_id: "101",
      name: "dataset",
      kind: "keep_online",
      until: null,
      reason: "Field season",
      set_by: { kind: "identity", display_name: "Avery Chen", app_id: null, identity_id: "9001" },
      set_at: new Date().toISOString(),
      released_at: null,
    });
    const drawer = await screen.findByRole("complementary", { name: "line_1044.segy" });
    const link = await within(drawer).findByRole("link", { name: /Set on a folder above/ });
    expect(link).toHaveAttribute("href", "/files/1/101?file=101");
    expect(within(drawer).queryByRole("button", { name: "Release" })).not.toBeInTheDocument();
  });

  it("asks before releasing a legal hold", async () => {
    const { user } = renderApp("/files/1/101?file=1007", { persona: "admin" });
    const drawer = await screen.findByRole("complementary", { name: "line_0999.segy" });
    await user.click(await within(drawer).findByRole("button", { name: "Release" }));
    const confirm = within(drawer).getByRole("alertdialog", { name: /Release the legal hold/ });
    expect(requestLog.some((r) => r.path.endsWith("/release"))).toBe(false);
    await user.click(within(confirm).getByRole("button", { name: "Release legal hold" }));
    await waitFor(() =>
      expect(requestLog.some((r) => r.path === "/v1/holds/852/release")).toBe(true),
    );
  });

  it("defaults to the first offered recall priority and sends any offered one", async () => {
    server.use(
      http.get("*/v1/files/1002/recall-options", () =>
        HttpResponse.json({
          file_id: "1002",
          version_id: "1",
          size: 8_200_000_000,
          needs_restore: true,
          online: false,
          no_promise_above_bytes: 10_000_000_000,
          active_recall: null,
          options: [
            {
              priority: "standard",
              offered: false,
              reason: "Backlog",
              eta: null,
              eta_confidence: "none",
            },
            { priority: "high", offered: true, eta: null, eta_confidence: "none" },
            { priority: "bulk", offered: true, eta: null, eta_confidence: "none" },
          ],
        }),
      ),
    );
    const { user } = renderApp("/files/1/101", { persona: "reader" });
    await user.click(await screen.findByRole("checkbox", { name: "Select line_1043.segy" }));
    await user.click(screen.getByRole("button", { name: /Recall…/ }));
    const dialog = await screen.findByRole("dialog", { name: "Recall line_1043.segy" });
    expect(await within(dialog).findByRole("radio", { name: /High priority/ })).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: /Standard/ })).toBeDisabled();
    await user.click(within(dialog).getByRole("radio", { name: /bulk/ }));
    await user.click(within(dialog).getByRole("button", { name: "Recall file" }));
    await waitFor(() =>
      expect(
        requestLog.find((r) => r.method === "POST" && r.path === "/v1/recalls")?.body,
      ).toMatchObject({
        priority: "bulk",
      }),
    );
  });

  it("ignores ?worm=cancel when WORM is off", async () => {
    renderApp("/shares/1?worm=cancel", { persona: "admin" });
    await screen.findByRole("heading", { name: /WORM/ });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("reopens the WORM step from ?worm=enable and drops the param on close", async () => {
    const { user, router } = renderApp("/shares/1?worm=enable", { persona: "admin" });
    const dialog = await screen.findByRole("dialog", { name: /Turn on WORM/ });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("refuses a download link that isn't https", async () => {
    server.use(
      http.post("*/v1/files/:id/versions/:vid/downloads", ({ params }) =>
        HttpResponse.json(
          {
            download_id: "1",
            file_id: String(params.id),
            version_id: String(params.vid),
            state: "ready",
            url: "http://example.test/blob",
          },
          { status: 202 },
        ),
      ),
    );
    const { user } = renderApp("/files/1/101?file=1006", { persona: "reader" });
    const versions = await screen.findByRole("list", { name: "Versions of survey_notes.docx" });
    const download = within(versions).getAllByRole("button", { name: /Download the version/ })[0];
    if (!download) throw new Error("no download button");
    await user.click(download);
    expect(await screen.findByText(/wasn't a secure \(https\) address/)).toBeInTheDocument();
    expect(browserNav.assign).not.toHaveBeenCalled();
  });

  it("says exactly what saved when a share setting fails after the policy saved", async () => {
    server.use(
      http.patch("*/v1/shares/2/settings", () => problem(412, "precondition_failed", "Stale")),
    );
    const { user } = renderApp("/policies/1", { persona: "admin" });
    await user.click(await screen.findByRole("checkbox", { name: /The share's Owners/ }));
    const group = screen.getByRole("group", { name: /On processing-scratch/ });
    await user.click(within(group).getByRole("radio", { name: /Tell them straight away/ }));
    const save = screen.getByRole("button", { name: "Save as version 5" });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    const title = await screen.findByText("Partly saved");
    const report = title.closest(".callout");
    if (!report) throw new Error("no report");
    expect(report).toHaveTextContent("Partly saved");
    expect(report).toHaveTextContent("Saved: version 5 of the policy is active.");
    expect(report).toHaveTextContent("Not saved for processing-scratch");
  });

  it("moves focus into the drawer, closes it on Esc and returns focus to the row", async () => {
    const { user } = renderApp("/files/1/101", { persona: "reader" });
    const name = await screen.findByRole("button", { name: "survey_notes.docx" });
    await user.click(name);
    const heading = await screen.findByRole("heading", { name: "survey_notes.docx", level: 2 });
    await waitFor(() => expect(heading).toHaveFocus());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("complementary")).not.toBeInTheDocument());
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(screen.getByRole("button", { name: "survey_notes.docx" })).toHaveFocus();
  });
});
