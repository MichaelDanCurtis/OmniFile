import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, vi } from "vitest";
import { browserNav } from "../api/client";
import { resetDb } from "../mocks/handlers";
import { server } from "../mocks/server";

// Lazy chunks (app/lazy.tsx) are transformed on their first import, which can
// take over a second while the whole suite runs: give findBy* room for it.
configure({ asyncUtilTimeout: 3000 });

// jsdom has no <dialog> behaviour; give it the parts the Modal uses.
if (typeof HTMLDialogElement !== "undefined") {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
}
Element.prototype.scrollIntoView = function scrollIntoView() {};
// axe-core probes canvas for icon fonts; jsdom has none.
HTMLCanvasElement.prototype.getContext = (() =>
  null) as typeof HTMLCanvasElement.prototype.getContext;

beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
beforeEach(() => {
  resetDb();
  vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
