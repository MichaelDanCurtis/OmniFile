import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

/**
 * The visual views' colour pairs, resolved from the design tokens in both
 * themes, against WCAG 2.x contrast: 4.5:1 for text, 3:1 for marks and icons.
 * jsdom computes no styles, so this reads the CSS itself.
 */
// vitest runs from portal/ (jsdom gives import.meta.url an http: scheme).
const tokens = readFileSync("src/theme/tokens.css", "utf8");
const visual = readFileSync("src/features/files/visual/visual.css", "utf8");

function block(css: string, selector: string): string {
  const at = css.indexOf(selector);
  if (at < 0) throw new Error(`no ${selector}`);
  const open = css.indexOf("{", at);
  return css.slice(open + 1, css.indexOf("}", open));
}

function vars(body: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of body.matchAll(/(--[a-z0-9-]+):\s*([^;]+);/g))
    out[m[1] ?? ""] = (m[2] ?? "").trim();
  return out;
}

const light = vars(block(tokens, ":root {"));
const dark = { ...light, ...vars(block(tokens, ':root[data-theme="dark"]')) };

function resolve(value: string, theme: Record<string, string>): string {
  const v = value.trim();
  const m = /^var\((--[a-z0-9-]+)\)$/.exec(v);
  if (m?.[1]) return resolve(theme[m[1]] ?? "", theme);
  if (v === "#fff") return "#ffffff";
  if (!/^#[0-9a-f]{6}$/i.test(v)) throw new Error(`can't resolve ${value}`);
  return v;
}

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

/** [rule, property for the foreground, property for the background, minimum]. */
const PAIRS: [string, string, string, number][] = [
  ['.journey-travel[data-step="failed"]', "color", "background", 3],
];

describe("visual views contrast", () => {
  for (const [rule, fgProp, bgProp, min] of PAIRS) {
    for (const [name, theme] of [
      ["light", light],
      ["dark", dark],
    ] as const) {
      it(`${rule} meets ${min}:1 in ${name}`, () => {
        const body = block(visual, rule);
        const get = (prop: string) =>
          new RegExp(`(?:^|\\n)\\s*${prop}:\\s*([^;]+);`).exec(body)?.[1] ?? "";
        expect(
          ratio(resolve(get(fgProp), theme), resolve(get(bgProp), theme)),
        ).toBeGreaterThanOrEqual(min);
      });
    }
  }

  it("keeps the travelling recall chip (accent ink on accent) at 3:1 or more", () => {
    for (const theme of [light, dark]) {
      expect(
        ratio(resolve("var(--accent-ink)", theme), resolve("var(--accent)", theme)),
      ).toBeGreaterThanOrEqual(3);
    }
  });

  it("keeps tier text (ink on soft) at 4.5:1 or more in both themes", () => {
    for (const theme of [light, dark]) {
      for (const t of ["hot", "warm", "cold", "archive", "smart"]) {
        const fg = resolve(`var(--tier-${t}-ink)`, theme);
        const bg = resolve(`var(--tier-${t}-soft)`, theme);
        expect(ratio(fg, bg)).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});
