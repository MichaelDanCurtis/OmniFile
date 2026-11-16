import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requestLog } from "../../mocks/handlers";
import { expectNoAxeViolations } from "../../test/axe";
import { renderApp } from "../../test/render";

const KEY = /^ofk1\.[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$/;

function shownKey(): string {
  const dialog = screen.getByRole("dialog", { name: "Your new API key" });
  const box = within(dialog).getByRole("textbox", { name: /API key/ });
  return (box as HTMLTextAreaElement).value;
}

describe("API keys", () => {
  it("recommends Entra credentials above the keys", async () => {
    const { container } = renderApp("/settings/api-clients/3");
    expect(
      await screen.findByText(
        "Entra app credentials are the recommended way: nothing is stored or shared. Use an OmniFile API key only when the client can't hold an Entra credential.",
      ),
    ).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "API keys" });
    const row = within(table).getByRole("row", { name: /3f9c2a7b1d0e4c58/ });
    expect(within(row).getByText("10.30.1.44")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows a new key once, and it is gone after closing", async () => {
    const { user, client } = renderApp("/settings/api-clients/3");
    await user.click(await screen.findByRole("button", { name: "Create key" }));
    const dialog = screen.getByRole("dialog", { name: /Create a key for Migration planner/ });
    // Only the client's scopes are offered.
    expect(within(dialog).getAllByRole("checkbox")).toHaveLength(2);
    await user.click(within(dialog).getByRole("checkbox", { name: "Archive.Read" }));
    const days = within(dialog).getByRole("spinbutton", { name: "Expires after (days)" });
    await user.clear(days);
    await user.type(days, "30");
    await user.click(within(dialog).getByRole("button", { name: "Create key" }));
    const key = await waitFor(() => {
      const k = shownKey();
      expect(k).toMatch(KEY);
      return k;
    });
    expect(
      screen.getByText(/OmniFile keeps only a hash and can.t show it again/),
    ).toBeInTheDocument();
    const sent = requestLog.find((r) => r.method === "POST" && r.path.endsWith("/3/keys"));
    expect(sent?.body).toEqual({ scopes: ["Policy.Propose"], expires_in_days: 30 });
    // Never in the query or mutation caches, the URL or storage.
    const cached = JSON.stringify([
      client
        .getQueryCache()
        .getAll()
        .map((q) => q.state.data),
      client
        .getMutationCache()
        .getAll()
        .map((m) => m.state.data),
    ]);
    expect(cached).not.toContain(key);
    expect(window.location.href).not.toContain(key);
    const stored = Array.from({ length: window.localStorage.length }, (_, i) => {
      const name = window.localStorage.key(i) ?? "";
      return `${name}=${window.localStorage.getItem(name) ?? ""}`;
    });
    expect(stored.join("\n")).not.toContain(key);

    await user.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Your new API key" })).not.toBeInTheDocument(),
    );
    expect(document.body.textContent).not.toContain(key);
    expect(Array.from(document.querySelectorAll("textarea")).some((t) => t.value === key)).toBe(
      false,
    );
    // The new key is listed by its id only.
    const keyId = key.split(".")[1] ?? "";
    expect(
      await within(screen.getByRole("table", { name: "API keys" })).findByText(keyId),
    ).toBeInTheDocument();
  });

  it("rotates: a new key, and the old key's new expiry", async () => {
    const { user } = renderApp("/settings/api-clients/3");
    await user.click(await screen.findByRole("button", { name: "Rotate key 3f9c2a7b1d0e4c58" }));
    const dialog = screen.getByRole("dialog", { name: "Rotate key 3f9c2a7b1d0e4c58" });
    const overlap = within(dialog).getByRole("spinbutton", {
      name: "Old key keeps working for (hours)",
    });
    await user.clear(overlap);
    await user.type(overlap, "24");
    await user.click(within(dialog).getByRole("button", { name: "Rotate key" }));
    await waitFor(() => expect(shownKey()).toMatch(KEY));
    const shown = screen.getByRole("dialog", { name: "Your new API key" });
    expect(within(shown).getByText(/keeps\s+working until/)).toBeInTheDocument();
    expect(within(shown).getByText("3f9c2a7b1d0e4c58")).toBeInTheDocument();
    const sent = requestLog.find((r) => r.path.endsWith("/keys/3f9c2a7b1d0e4c58/rotate"));
    expect(sent?.body).toEqual({ expires_in_days: 90, overlap_hours: 24 });
    await user.click(within(shown).getByRole("button", { name: "Done" }));
    const table = screen.getByRole("table", { name: "API keys" });
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(3));
    expect(within(table).getAllByText("3f9c2a7b1d0e4c58")).toHaveLength(2);
  });

  it("revokes a key after a confirmation", async () => {
    const { user } = renderApp("/settings/api-clients/3");
    await user.click(await screen.findByRole("button", { name: "Revoke key 3f9c2a7b1d0e4c58" }));
    const dialog = screen.getByRole("dialog", { name: "Revoke key 3f9c2a7b1d0e4c58?" });
    await user.click(within(dialog).getByRole("button", { name: "Revoke key" }));
    const table = screen.getByRole("table", { name: "API keys" });
    const row = within(table).getByRole("row", { name: /3f9c2a7b1d0e4c58/ });
    expect(await within(row).findByText("Revoked")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Rotate key 3f9c2a7b1d0e4c58" }),
    ).not.toBeInTheDocument();
    expect(requestLog.some((r) => r.path.endsWith("/keys/3f9c2a7b1d0e4c58/revoke"))).toBe(true);
  });
});
