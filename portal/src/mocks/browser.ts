import { getResponse } from "msw";
import { setupWorker } from "msw/browser";
import { persistCloudMock } from "./cloud";
import type { Persona } from "./db";
import { handlers, resetDb } from "./handlers";

export const PERSONA_KEY = "omnifile.mockPersona";

/** Dev only (`npm run dev:mock`): serve the API from MSW in the browser. */
export async function startMockWorker(): Promise<void> {
  let persona: Persona = "admin";
  try {
    const stored = localStorage.getItem(PERSONA_KEY);
    if (stored === "admin" || stored === "steward" || stored === "reader") persona = stored;
  } catch {
    // storage blocked: use the default persona
  }
  // A Connect Azure sign-in reloads the page (mocks/cloud.ts): keep that state.
  persistCloudMock();
  resetDb({ persona });
  try {
    await setupWorker(...handlers).start({ quiet: true, onUnhandledFrame: "bypass" });
  } catch (error) {
    // Some embedded browsers and private windows refuse service workers.
    // Answer from the same handlers inside the page instead.
    console.warn("[mock] No service worker here; mocking fetch in the page.", error);
    const realFetch = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      return (await getResponse(handlers, request.clone())) ?? realFetch(request);
    };
  }
}
