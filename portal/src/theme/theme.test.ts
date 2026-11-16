import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { DotEngine, drift } from "./dots";

/**
 * WP17's theme: the palette's text pairs meet WCAG AA in both themes, the
 * fonts come from this origin only, and the dot layer keeps its motion
 * budget (stills under reduced motion, stops when hidden or off screen).
 */
const tokens = readFileSync("src/theme/tokens.css", "utf8");
const fonts = readFileSync("src/theme/fonts.css", "utf8");
const motion = readFileSync("src/theme/motion.css", "utf8");

function block(selector: string): Record<string, string> {
  const at = tokens.indexOf(selector);
  const body = tokens.slice(tokens.indexOf("{", at) + 1, tokens.indexOf("}", at));
  const out: Record<string, string> = {};
  for (const m of body.matchAll(/(--[a-z0-9-]+):\s*([^;]+);/g))
    out[m[1] ?? ""] = (m[2] ?? "").trim();
  return out;
}
const light = block(":root {");
const dark = { ...light, ...block(':root[data-theme="dark"]') };

const lum = (hex: string) => {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const [r = 0, g = 0, b = 0] = c.map((x) =>
    x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a: string, b: string) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p) as [number, number];
  return (x + 0.05) / (y + 0.05);
};

/** [foreground, background, minimum]: text 4.5:1, marks and control borders 3:1. */
const PAIRS: [string, string, number][] = [
  ["--ink", "--bg", 4.5],
  ["--ink", "--surface", 4.5],
  ["--ink-2", "--surface-3", 4.5],
  ["--muted", "--bg", 4.5],
  ["--muted", "--surface", 4.5],
  ["--muted", "--surface-3", 4.5],
  ["--link", "--surface", 4.5],
  ["--link", "--bg", 4.5],
  ["--accent-ink", "--accent", 4.5],
  ["--ok", "--ok-soft", 4.5],
  ["--warn", "--warn-soft", 4.5],
  ["--danger", "--danger-soft", 4.5],
  ["--danger", "--surface", 4.5],
  ["--ok", "--surface", 4.5],
  ["--warn", "--surface", 4.5],
  ["--signal", "--bg", 3],
  ["--signal", "--surface", 3],
  ["--control-line", "--surface", 3],
  ["--focus", "--bg", 3],
  ["--focus", "--surface", 3],
  ["--chart-1", "--surface", 3],
];

describe("theme tokens", () => {
  for (const [name, theme] of [
    ["light", light],
    ["dark", dark],
  ] as const) {
    for (const [fg, bg, min] of PAIRS) {
      it(`${fg} on ${bg} meets ${min}:1 in ${name}`, () => {
        expect(ratio(theme[fg] ?? "", theme[bg] ?? "")).toBeGreaterThanOrEqual(min);
      });
    }
  }

  it("defines the dark theme twice, identically (system and chosen)", () => {
    const at = tokens.indexOf(':root:not([data-theme="light"])');
    const body = tokens.slice(tokens.indexOf("{", at) + 1, tokens.indexOf("}", at));
    const media: Record<string, string> = {};
    for (const m of body.matchAll(/(--[a-z0-9-]+):\s*([^;]+);/g))
      media[m[1] ?? ""] = (m[2] ?? "").trim();
    expect(media).toEqual(block(':root[data-theme="dark"]'));
  });

  it("loads fonts from this origin only, woff2, with a swap fallback", () => {
    const urls = [...fonts.matchAll(/url\("([^"]+)"\)/g)].map((m) => m[1] ?? "");
    expect(urls.length).toBeGreaterThan(0);
    for (const u of urls) {
      expect(u).toMatch(/^@fontsource\/(space-grotesk|space-mono|doto)\/files\/.+\.woff2$/);
      expect(u).not.toMatch(/^(https?:)?\/\//);
    }
    expect(fonts.match(/font-display: swap;/g)?.length).toBe(urls.length);
    expect(fonts + tokens + motion).not.toMatch(/fonts\.googleapis|fonts\.gstatic|@import url\(/);
  });

  it("has no spring or bounce easing, and stills everything under reduced motion", () => {
    for (const m of tokens.matchAll(/cubic-bezier\(([^)]+)\)/g)) {
      const [, y1 = 0, , y2 = 0] = (m[1] ?? "").split(",").map(Number);
      expect(y1).toBeGreaterThanOrEqual(0);
      expect(y1).toBeLessThanOrEqual(1);
      expect(y2).toBeGreaterThanOrEqual(0);
      expect(y2).toBeLessThanOrEqual(1);
    }
    expect(motion).toMatch(
      /prefers-reduced-motion: reduce[\s\S]*animation-iteration-count: 1 !important/,
    );
  });
});

/** A 2D context that records what it is asked to do. */
function fakeCtx() {
  const calls = { arcs: 0, fills: 0, clears: 0 };
  const ctx = {
    setTransform: vi.fn(),
    clearRect: () => calls.clears++,
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    arc: () => calls.arcs++,
    fill: () => calls.fills++,
    fillStyle: "",
    globalAlpha: 1,
  };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, calls };
}

function fakeWin(reduced = false) {
  let raf = 0;
  const queued = new Map<number, FrameRequestCallback>();
  const win = {
    matchMedia: (q: string) => ({
      matches: reduced && q.includes("reduced-motion"),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
    devicePixelRatio: 1,
    performance: { now: () => 1000 },
    requestAnimationFrame: (cb: FrameRequestCallback) => {
      raf += 1;
      queued.set(raf, cb);
      return raf;
    },
    cancelAnimationFrame: (id: number) => queued.delete(id),
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
  };
  const step = (t: number) => {
    const cbs = [...queued.values()];
    queued.clear();
    for (const cb of cbs) cb(t);
  };
  return { win: win as unknown as Window, step, pending: () => queued.size };
}

function canvas(width: number, height: number) {
  const c = document.createElement("canvas");
  c.getBoundingClientRect = () => ({ width, height, left: 0, top: 0 }) as DOMRect;
  return c;
}

describe("the dot layer", () => {
  it("lays one dot per grid step and draws a frame in at most 16 fills", () => {
    const { ctx, calls } = fakeCtx();
    const { win } = fakeWin();
    const e = new DotEngine(canvas(140, 70), ctx, { grid: 7 }, win);
    e.resize();
    expect(e.stats().dots).toBe(20 * 10);
    e.draw(5000);
    expect(calls.arcs).toBeGreaterThan(0);
    expect(calls.arcs).toBeLessThanOrEqual(e.stats().dots);
    expect(calls.fills).toBeLessThanOrEqual(16);
  });

  it("drifts slowly: two frames 33 ms apart barely differ", () => {
    for (const [x, y] of [
      [10, 10],
      [300, 40],
      [77, 190],
    ] as const) {
      expect(Math.abs(drift(x, y, 1000) - drift(x, y, 1033))).toBeLessThan(0.05);
      expect(drift(x, y, 1000)).toBeGreaterThanOrEqual(0);
      expect(drift(x, y, 1000)).toBeLessThanOrEqual(1);
    }
  });

  it("keeps animating while visible, and stops when the tab is hidden or it scrolls away", () => {
    const { ctx } = fakeCtx();
    const { win, step, pending } = fakeWin();
    const e = new DotEngine(canvas(140, 70), ctx, {}, win);
    e.resize();
    expect(pending()).toBe(1);
    step(100);
    expect(pending()).toBe(1);
    e.setVisible(false);
    expect(pending()).toBe(0);
    e.setVisible(true);
    expect(pending()).toBe(1);
    e.setOnScreen(false);
    expect(pending()).toBe(0);
  });

  it("draws one still frame under reduced motion, and ignores the pointer and ripples", () => {
    const { ctx, calls } = fakeCtx();
    const { win, step, pending } = fakeWin(true);
    const e = new DotEngine(canvas(140, 70), ctx, {}, win);
    e.resize();
    step(100);
    expect(calls.clears).toBe(1);
    expect(pending()).toBe(0);
    e.pulse();
    e.point(50, 30);
    expect(pending()).toBe(0);
  });

  it("rests after a glyph assembles when it is not live", () => {
    const { ctx } = fakeCtx();
    const { win, step, pending } = fakeWin();
    const e = new DotEngine(canvas(140, 70), ctx, { live: false }, win);
    e.resize();
    // No glyph to assemble (jsdom has no 2D context to rasterize one): nothing left to do.
    step(100);
    expect(pending()).toBe(0);
    // A ripple wakes it.
    e.pulse();
    expect(pending()).toBe(1);
  });
});
