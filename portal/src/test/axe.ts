import axe from "axe-core";
import { expect } from "vitest";

/**
 * Runs axe-core on the rendered page and fails on any violation.
 * Colour contrast is off here: jsdom computes no styles (css: false), so
 * contrast is held by the design tokens instead (checked in the report).
 */
export async function expectNoAxeViolations(container: Element = document.body) {
  const results = await axe.run(container, {
    rules: { "color-contrast": { enabled: false } },
    resultTypes: ["violations"],
  });
  const summary = results.violations.map(
    (v) =>
      `${v.id}: ${v.help}\n  ${v.nodes
        .map((n) => n.html)
        .slice(0, 3)
        .join("\n  ")}`,
  );
  expect(summary).toEqual([]);
}
