import { describe, expect, it } from "vitest";
import { restoredName, restoredTimestampedName, splitName, utcStamp } from "./restoreNames";

describe("restore names (decision 34)", () => {
  const at = new Date(Date.UTC(2026, 9, 6, 9, 30, 5));

  it("splits at the last dot", () => {
    expect(splitName("line_1042.segy")).toEqual({ stem: "line_1042", ext: ".segy" });
    expect(splitName("archive.tar.gz")).toEqual({ stem: "archive.tar", ext: ".gz" });
    expect(splitName("README")).toEqual({ stem: "README", ext: "" });
  });

  it("gives a leading-dot name no extension", () => {
    expect(splitName(".bashrc")).toEqual({ stem: ".bashrc", ext: "" });
    expect(restoredName(".bashrc")).toBe(".bashrc-restored");
    expect(restoredName(".config.json")).toBe(".config-restored.json");
  });

  it("stamps in UTC with no colons", () => {
    expect(utcStamp(at)).toBe("2026-10-06-093005Z");
    expect(restoredTimestampedName("line_1042.segy", at)).toBe(
      "line_1042-restored-2026-10-06-093005Z.segy",
    );
    expect(restoredName("line_1042.segy")).toBe("line_1042-restored.segy");
  });
});
