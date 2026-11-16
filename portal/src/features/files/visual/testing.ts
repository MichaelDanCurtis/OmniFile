import { vi } from "vitest";

/** Tests only: answer `prefers-reduced-motion` as `on` (undo with vi.unstubAllGlobals()). */
export function reduceMotion(on: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: on && query.includes("reduce"),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  );
}
