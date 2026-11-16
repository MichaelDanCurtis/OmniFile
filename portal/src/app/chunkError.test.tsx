import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "../test/render";
import { RELOAD_GUARD_MS, installPreloadRecovery, pageReload } from "./lazy";

// A page chunk that can't load (a network blip, or a stale tab after a deploy).
vi.mock("../features/migrations/MigrationsPage", () => {
  throw new Error("Failed to fetch dynamically imported module");
});
// A page in the main chunk that throws while rendering.
vi.mock("../features/audit/AuditPage", () => ({
  AuditPage: () => {
    throw new Error("boom");
  },
}));

afterEach(() => {
  vi.unstubAllGlobals();
  try {
    sessionStorage.clear();
  } catch {
    // none
  }
});

describe("a part that can't load", () => {
  it("says so, offers a reload, and keeps the nav", async () => {
    const reload = vi.spyOn(pageReload, "reload").mockImplementation(() => undefined);
    const { user } = renderApp("/migrations", { persona: "admin" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't load this page");
    expect(screen.getByRole("link", { name: /Files/ })).toBeVisible();
    expect(screen.queryByText(/Unexpected Application Error/)).not.toBeInTheDocument();
    await user.click(within(alert).getByRole("button", { name: "Reload" }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("keeps the Layout when a route throws (the root's error element)", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    renderApp("/audit", { persona: "admin" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't load this part of the portal");
    expect(screen.getByRole("link", { name: /Files/ })).toBeVisible();
    expect(document.title).toBe("Something went wrong · OmniFile Gateway");
  });
});

describe("vite:preloadError", () => {
  it("reloads once, then not again within the guard", () => {
    const reload = vi.spyOn(pageReload, "reload").mockImplementation(() => undefined);
    const off = installPreloadRecovery();
    const fire = () => {
      const e = new Event("vite:preloadError", { cancelable: true });
      window.dispatchEvent(e);
      return e.defaultPrevented;
    };
    expect(fire()).toBe(true);
    expect(fire()).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
    // After the guard, it may reload again.
    sessionStorage.setItem("omnifile.chunkReloadAt", String(Date.now() - RELOAD_GUARD_MS - 1));
    expect(fire()).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
    off();
  });

  it("never reloads by itself without session storage (no loop guard)", () => {
    const reload = vi.spyOn(pageReload, "reload").mockImplementation(() => undefined);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const off = installPreloadRecovery();
    window.dispatchEvent(new Event("vite:preloadError", { cancelable: true }));
    expect(reload).not.toHaveBeenCalled();
    off();
  });
});
