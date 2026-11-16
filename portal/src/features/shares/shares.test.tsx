import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { getDb, requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";
import { browserNav } from "../../api/client";
import { UUIDV7_PATTERN } from "../../api/uuidv7";

describe("share settings", () => {
  it("shows the Kerberos throughput note when Kerberos is chosen", async () => {
    const { user, container } = renderApp("/shares/2", { persona: "admin" });
    const nfs = await screen.findByRole("group", { name: "Allowed security flavours" });
    expect(screen.queryByText("Kerberos and throughput")).not.toBeInTheDocument();
    await user.click(within(nfs).getByRole("checkbox", { name: /krb5p/ }));
    expect(screen.getByText("Kerberos and throughput")).toBeInTheDocument();
    expect(
      screen.getByText(/53% fewer IOPS with krb5, and about 77% fewer with krb5p/),
    ).toBeInTheDocument();
    // The server's preview checks follow the change.
    const checks = await screen.findByRole("list", { name: "Share checks" });
    expect(await within(checks).findByText(/Kerberos costs throughput/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("saves only the changed fields, with If-Match", async () => {
    const { user } = renderApp("/shares/1", { persona: "steward" });
    const depth = await screen.findByLabelText("Let people go back");
    await user.clear(depth);
    await user.type(depth, "60");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(requestLog.some((r) => r.method === "PATCH")).toBe(true));
    const patch = requestLog.find((r) => r.method === "PATCH");
    expect(patch?.body).toEqual({ rewind_depth_days: 60 });
    expect(patch?.headers["if-match"]).toBe('"14"');
    // A steward who owns the share can't touch NFS security.
    expect(screen.getByRole("checkbox", { name: /krb5p/ })).toBeDisabled();
  });

  describe("how long a file must sit untouched (decision 67)", () => {
    const LEGEND = /How long a file must sit untouched/;

    it("is up front on an NFSv3 share, with what it trades and what it protects", async () => {
      const { container } = renderApp("/shares/2", { persona: "admin" });
      const group = await screen.findByRole("group", { name: LEGEND });
      expect(within(group).getByRole("radio", { name: /The default: 10 seconds/ })).toBeChecked();
      expect(screen.getByText(/fewer partial versions, but uploads start later/)).toBeVisible();
      expect(screen.getByText(/NFSv3 has no open or close/)).toBeVisible();
      expect(screen.getByText(/pauses for longer than twice this time/)).toBeVisible();
      await expectNoAxeViolations(container);
    });

    it("sends a period of the share's own, and null to go back to the default", async () => {
      const { user } = renderApp("/shares/2", { persona: "admin" });
      await user.click(await screen.findByRole("radio", { name: /Choose a time/ }));
      const secs = screen.getByLabelText("Wait");
      await user.clear(secs);
      await user.type(secs, "90");
      await user.click(screen.getByRole("button", { name: "Save settings" }));
      await waitFor(() => expect(requestLog.some((r) => r.method === "PATCH")).toBe(true));
      expect(requestLog.find((r) => r.method === "PATCH")?.body).toEqual({ quiet_s: 90 });
      // Back to the default, on the saved share.
      expect(await screen.findByRole("radio", { name: /Choose a time/ })).toBeChecked();
      expect(screen.getByLabelText("Wait")).toHaveValue(90);
      requestLog.length = 0;
      await user.click(screen.getByRole("radio", { name: /The default: 10 seconds/ }));
      await user.click(screen.getByRole("button", { name: "Save settings" }));
      await waitFor(() => expect(requestLog.some((r) => r.method === "PATCH")).toBe(true));
      expect(requestLog.find((r) => r.method === "PATCH")?.body).toEqual({ quiet_s: null });
    });

    it("won't save a period out of range", async () => {
      const { user } = renderApp("/shares/2", { persona: "admin" });
      // Another change is waiting too: a bad period holds back the whole save.
      const depth = await screen.findByLabelText("Let people go back");
      await user.clear(depth);
      await user.type(depth, "8");
      await user.click(await screen.findByRole("radio", { name: /Choose a time/ }));
      const secs = screen.getByLabelText("Wait");
      const save = screen.getByRole("button", { name: "Save settings" });
      for (const bad of ["9", "601", "1.5"]) {
        await user.clear(secs);
        await user.type(secs, bad);
        expect(secs).toBeInvalid();
        expect(save).toBeDisabled();
      }
      await user.clear(secs);
      await user.type(secs, "600");
      expect(secs).toBeValid();
      expect(save).toBeEnabled();
    });

    it("is read-only to anyone but Owners and ArchiveAdmins", async () => {
      // The steward doesn't own the NFSv3 share.
      renderApp("/shares/2", { persona: "steward" });
      const group = await screen.findByRole("group", { name: LEGEND });
      for (const radio of within(group).getAllByRole("radio")) expect(radio).toBeDisabled();
      expect(
        screen.getByText(/Only this share's Owners or an ArchiveAdmin can change how long/),
      ).toBeVisible();
    });

    it("an Owner can change it", async () => {
      renderApp("/shares/1", { persona: "steward" });
      const more = await screen.findByText(/This matters most on NFSv3/);
      expect(more).toBeInTheDocument();
      expect(screen.getByRole("radio", { name: /Choose a time/ })).toBeEnabled();
    });

    it("is one click down where NFSv3 isn't served, and says what it does there", async () => {
      const { user, container } = renderApp("/shares/1", { persona: "admin" });
      const summary = await screen.findByText(/This matters most on NFSv3/);
      expect(summary).toHaveTextContent("10 seconds");
      expect(screen.queryByRole("radio", { name: /Choose a time/ })).not.toBeVisible();
      await user.click(summary);
      expect(screen.getByRole("radio", { name: /Choose a time/ })).toBeVisible();
      // NFSv4 and SMB, as the edge really treats them.
      expect(
        screen.getByText(/a file a client holds open is never treated as finished/),
      ).toBeVisible();
      expect(screen.getByText(/pauses with the file still open/)).toBeVisible();
      expect(screen.queryByText(/NFSv3 has no open or close/)).not.toBeInTheDocument();
      await expectNoAxeViolations(container);
    });

    it("shows a WORM share's default, and why it's longer", async () => {
      const { user } = renderApp("/shares/3", { persona: "admin" });
      const summary = await screen.findByText(/This matters most on NFSv3/);
      expect(summary).toHaveTextContent("60 seconds");
      await user.click(summary);
      expect(screen.getByRole("radio", { name: /The default: 60 seconds/ })).toBeChecked();
      expect(screen.getByText(/can't be removed until retention ends/)).toBeVisible();
    });

    it("asks before a WORM share waits less than its default, and says why", async () => {
      const { user } = renderApp("/shares/3", { persona: "admin" });
      await user.click(await screen.findByText(/This matters most on NFSv3/));
      await user.click(screen.getByRole("radio", { name: /Choose a time/ }));
      const secs = screen.getByLabelText("Wait");
      await user.clear(secs);
      await user.type(secs, "20");
      await user.click(screen.getByRole("button", { name: "Save settings" }));
      const dialog = await screen.findByRole("dialog", { name: /Wait only 20 seconds/ });
      expect(within(dialog).getByText(/keeps the half-written file as a version/)).toBeVisible();
      expect(within(dialog).getByText(/can't be deleted\s+until its retention ends/)).toBeVisible();
      expect(requestLog.some((r) => r.method === "PATCH")).toBe(false);
      // Backing out saves nothing.
      await user.click(within(dialog).getByRole("button", { name: "Keep it as it was" }));
      expect(requestLog.some((r) => r.method === "PATCH")).toBe(false);
      // Confirming sends the confirmation with the value.
      await user.click(screen.getByRole("button", { name: "Save settings" }));
      const again = await screen.findByRole("dialog", { name: /Wait only 20 seconds/ });
      await user.click(within(again).getByRole("button", { name: "Save 20 seconds" }));
      await waitFor(() => expect(requestLog.some((r) => r.method === "PATCH")).toBe(true));
      expect(requestLog.find((r) => r.method === "PATCH")?.body).toEqual({
        quiet_s: 20,
        quiet_s_confirmed: true,
      });
    });

    it("doesn't ask at or above the WORM default, or on a share that isn't WORM", async () => {
      const { user } = renderApp("/shares/3", { persona: "admin" });
      await user.click(await screen.findByText(/This matters most on NFSv3/));
      await user.click(screen.getByRole("radio", { name: /Choose a time/ }));
      const secs = screen.getByLabelText("Wait");
      await user.clear(secs);
      await user.type(secs, "60");
      await user.click(screen.getByRole("button", { name: "Save settings" }));
      await waitFor(() => expect(requestLog.some((r) => r.method === "PATCH")).toBe(true));
      expect(requestLog.find((r) => r.method === "PATCH")?.body).toEqual({ quiet_s: 60 });
      expect(screen.queryByRole("dialog", { name: /Wait only/ })).not.toBeInTheDocument();
    });

    it("says when the share's edge doesn't support it yet", async () => {
      renderApp("/shares/2", { persona: "admin" });
      const share = getDb().shares.find((s) => s.share_id === "2");
      if (!share) throw new Error("the mock has no share 2");
      share.quiet_supported = false;
      share.edge_name = "edge-old-1";
      expect(await screen.findByText("Not yet supported on its edge")).toBeVisible();
      expect(screen.getByText(/edge-old-1 hasn't reported/)).toBeVisible();
    });

    it("asks when WORM goes on over a period shorter than its default", async () => {
      const { user } = renderApp("/shares/1", { persona: "admin" });
      const share = getDb().shares.find((s) => s.share_id === "1");
      if (!share) throw new Error("the mock has no share 1");
      share.quiet_s = 20;
      await user.click(await screen.findByRole("button", { name: "Turn on WORM…" }));
      const dialog = await screen.findByRole("dialog", { name: /Turn on WORM/ });
      expect(
        within(dialog).getByText("Files here count as finished after 20 seconds"),
      ).toBeVisible();
      expect(within(dialog).getByText(/can't be deleted until its\s+retention ends/)).toBeVisible();
      await user.type(
        within(dialog).getByRole("textbox", { name: /Type the share name/ }),
        "long-term-storage",
      );
      const go = within(dialog).getByRole("button", { name: "Turn on WORM" });
      expect(go).toBeDisabled();
      await user.click(within(dialog).getByRole("checkbox", { name: /Keep 20 seconds anyway/ }));
      expect(go).toBeEnabled();
      await user.click(go);
      await waitFor(() =>
        expect(requestLog.some((r) => r.path === "/v1/shares/1/worm/enable")).toBe(true),
      );
      expect(requestLog.find((r) => r.path === "/v1/shares/1/worm/enable")?.body).toEqual({
        retention_days: 2555,
        confirm_name: "long-term-storage",
        window_hours: 24,
        quiet_s_confirmed: true,
      });
    });

    it("says nothing about the edge once it supports it", async () => {
      renderApp("/shares/2", { persona: "admin" });
      await screen.findByRole("group", { name: /How long a file must sit untouched/ });
      expect(screen.queryByText("Not yet supported on its edge")).not.toBeInTheDocument();
    });
  });

  it("enables WORM only with the typed share name, and shows the lock deadline", async () => {
    const { user } = renderApp("/shares/1", { persona: "admin" });
    await user.click(await screen.findByRole("button", { name: "Turn on WORM…" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Turn on WORM for long-term-storage",
    });
    expect(within(dialog).getByText(/It locks around/)).toBeInTheDocument();
    const go = within(dialog).getByRole("button", { name: "Turn on WORM" });
    const name = within(dialog).getByRole("textbox", { name: /Type the share name/ });
    await user.type(name, "long-term-storag");
    expect(go).toBeDisabled();
    await user.type(name, "e");
    expect(go).toBeEnabled();
    await user.click(go);
    await waitFor(() =>
      expect(requestLog.some((r) => r.path === "/v1/shares/1/worm/enable")).toBe(true),
    );
    const post = requestLog.find((r) => r.path === "/v1/shares/1/worm/enable");
    expect(post?.body).toEqual({
      retention_days: 2555,
      confirm_name: "long-term-storage",
      window_hours: 24,
    });
    expect(post?.headers["if-match"]).toBe('"14"');
    expect(post?.headers["idempotency-key"]).toMatch(UUIDV7_PATTERN);
    expect(await screen.findByText("In its test window")).toBeInTheDocument();
    expect(screen.getByText("Locks")).toBeInTheDocument();
  });

  it("asks for a fresh sign-in before WORM when the last one is old", async () => {
    const { user } = renderApp("/shares/1", { persona: "admin", signedInAgoMs: 3600_000 });
    await user.click(await screen.findByRole("button", { name: "Turn on WORM…" }));
    const dialog = await screen.findByRole("dialog", { name: /Turn on WORM/ });
    expect(within(dialog).queryByRole("button", { name: "Turn on WORM" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Sign in again to continue" }));
    expect(browserNav.assign).toHaveBeenCalledWith(
      "/auth/login?return_to=%2Fshares%2F1%3Fworm%3Denable&reauth=1",
    );
  });

  it("lists legal holds for an ArchiveAdmin", async () => {
    renderApp("/shares/1", { persona: "admin" });
    const holds = await screen.findByRole("list", { name: "Legal holds" });
    expect(within(holds).getByText("line_0999.segy")).toBeInTheDocument();
  });
});

describe("share access", () => {
  it("shows the three groups, and an Owner can change Readers but not Owners", async () => {
    const { user, container } = renderApp("/shares/1/access", { persona: "steward" });
    const owners = await screen.findByRole("region", { name: "Owners" });
    expect(within(owners).getByText("GG-Seismic-Owners")).toBeInTheDocument();
    expect(within(owners).queryByRole("button", { name: /Change/ })).not.toBeInTheDocument();
    const readers = screen.getByRole("region", { name: "Readers" });
    await user.click(within(readers).getByRole("button", { name: /Change readers group/ }));
    await user.type(
      within(readers).getByRole("searchbox", { name: "New Readers group" }),
      "GG-Geom",
    );
    await user.click(await within(readers).findByRole("button", { name: /GG-Geomatics/ }));
    await user.click(screen.getByRole("button", { name: "Save access" }));
    await waitFor(() => expect(requestLog.some((r) => r.method === "PUT")).toBe(true));
    const put = requestLog.find((r) => r.method === "PUT");
    expect(put?.body).toMatchObject({
      readers_group_sid: "S-1-5-21-3623811015-3361044348-30300820-1602",
    });
    await expectNoAxeViolations(container);
  });
});

describe("shares list", () => {
  it("flags a share whose checks have an error (decision 39)", async () => {
    const { db } = renderApp("/shares", { persona: "admin" });
    const scratch = db.shares.find((s) => s.name === "processing-scratch");
    if (!scratch) throw new Error("the mock has processing-scratch");
    scratch.checks.push({
      severity: "error",
      code: "host_trusted_restricted",
      message:
        "Folders on processing-scratch are restricted to AD groups, which sec=sys can't prove.",
      field: "host_trust_override",
      share_id: "2",
      fee: null,
    });
    const table = await screen.findByRole("table", { name: "Shares" });
    const row = (await within(table).findByRole("link", { name: "processing-scratch" })).closest(
      "tr",
    );
    const badge = within(row as HTMLElement).getByText("Needs attention");
    expect(badge).toHaveAttribute("title", expect.stringContaining("sec=sys can't prove"));
    const other = within(table).getByRole("link", { name: "long-term-storage" }).closest("tr");
    expect(within(other as HTMLElement).queryByText("Needs attention")).not.toBeInTheDocument();
  });
});
