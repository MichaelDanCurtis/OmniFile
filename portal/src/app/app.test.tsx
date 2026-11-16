import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { browserNav } from "../api/client";
import { example, examples } from "../mocks/examples.gen";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

describe("session and navigation", () => {
  it("sends a signed-out visitor to sign-in, returning to the same page", async () => {
    server.use(
      http.get("*/auth/session", () =>
        HttpResponse.json(
          {
            type: "urn:omnifile:error:unauthenticated",
            title: "Unauthenticated",
            status: 401,
            code: "unauthenticated",
            retryable: false,
          },
          { status: 401, headers: { "Content-Type": "application/problem+json" } },
        ),
      ),
    );
    renderApp("/files");
    expect(await screen.findByText("Taking you to sign-in…")).toBeInTheDocument();
    await waitFor(() => expect(browserNav.assign).toHaveBeenCalled());
    expect(browserNav.assign).toHaveBeenCalledWith(
      expect.stringMatching(/^\/auth\/login\?return_to=%2F/),
    );
  });

  it("shows each role only the screens it can use", async () => {
    renderApp("/files", { persona: "reader" });
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: /Files/ })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /Recalls/ })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: /Policies/ })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: /Audit/ })).not.toBeInTheDocument();
  });

  it("gives an ArchiveAdmin policies, proposals with a pending count, shares and audit", async () => {
    renderApp("/files", { persona: "admin" });
    const nav = await screen.findByRole("navigation", { name: "Main" });
    for (const name of [/Policies/, /Proposals/, /Shares/, /Audit/]) {
      expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    }
    expect(await within(nav).findByLabelText("2 pending")).toBeInTheDocument();
  });

  it("signs out through Entra's end-session URL", async () => {
    const { user } = renderApp("/files");
    await user.click(await screen.findByRole("button", { name: /Sign out/ }));
    await waitFor(() => expect(browserNav.assign).toHaveBeenCalledWith("/signed-out"));
  });

  it("cycles the theme between system, light and dark", async () => {
    const { user } = renderApp("/files");
    const toggle = await screen.findByRole("button", { name: /Theme: match system/ });
    await user.click(toggle);
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    await user.click(screen.getByRole("button", { name: /Theme: light/ }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    await user.click(screen.getByRole("button", { name: /Theme: dark/ }));
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });

  it("tells an over-200-groups user their groups are being resolved", async () => {
    renderApp("/files", { persona: "reader", groupsPending: true });
    expect(await screen.findByText("Your AD groups are being resolved")).toBeInTheDocument();
    expect(await screen.findByText(/still reading your AD groups/)).toBeInTheDocument();
  });

  it("builds the mock from the YAML examples", async () => {
    const { db } = renderApp("/files");
    expect(db.groups.some((g) => g.display_name === examples.GroupRef[0]?.display_name)).toBe(true);
    expect(db.audit.some((a) => a.audit_id === example.AuditRecord.audit_id)).toBe(true);
    expect(await screen.findByRole("list", { name: "Shares" })).toBeInTheDocument();
  });
});
