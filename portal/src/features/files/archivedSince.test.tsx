import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { formatDate } from "../../lib/format";
import { example } from "../../mocks/examples.gen";
import { EntryTags, NextStep } from "./EntryBits";
import { nextMoves } from "./visual/FileJourney";

// A file with the same bytes archived the blob: the file's own date is still ahead.
const WENT = "2026-11-02T09:00:00.000Z";
const OWN_DATE = "2027-06-01";
const tier = { ...example.FileDetail.tier, tier_changed_at: WENT };

afterEach(cleanup);

describe("a move waiting because another file shares the storage", () => {
  const UNTIL = "2026-11-07T09:00:00.000Z";
  const cold = { ...tier, logical: "cold" as const, native_class: "Cold", archived: false };
  const pin = {
    pin_id: "9",
    tier: "hot" as const,
    until: null,
    override_archive: true,
    inherited_from: null,
  };
  const heldBack = {
    ...example.FileDetail,
    tier: cold,
    pin,
    archive_at: null,
    next_action: null,
    next_action_at: null,
    move_hold: { to: "hot" as const, until: UNTIL },
  };

  it("the journey says so, in plain words, and promises no other move", () => {
    const text = nextMoves(heldBack);
    expect(text).toContain(`Its move to Hot waits until ${formatDate(UNTIL)}`);
    expect(text).toContain("couldn't get a copy of its own");
    expect(text).not.toContain("Moves to");
    expect(text).not.toContain("No move planned");
  });

  it("the file list says so", () => {
    render(<NextStep entry={{ ...example.Entry, ...heldBack }} />);
    expect(screen.getByText(/Move to Hot waiting/)).toBeTruthy();
    expect(screen.getByText(`until ${formatDate(UNTIL)}`)).toBeTruthy();
    expect(screen.queryByText(/Moves to/)).toBeNull();
  });

  it("a file with no hold reads as before", () => {
    const text = nextMoves({
      ...example.FileDetail,
      tier: cold,
      archive_at: null,
      next_action: "tier_cold",
      next_action_at: UNTIL,
      move_hold: null,
    });
    expect(text).toBe(`Moves to Cold on ${formatDate(UNTIL)}.`);
  });
});

describe("how long a file has been archived", () => {
  it("the journey counts from when the blob went, not the file's own archive date", () => {
    const text = nextMoves({ ...example.FileDetail, tier, archive_at: OWN_DATE });
    expect(text).toBe(`Archived since ${formatDate(WENT)}. Reading it needs a recall first.`);
    expect(text).not.toContain(formatDate(OWN_DATE));
  });

  it("the journey names no date when it doesn't know one", () => {
    const text = nextMoves({
      ...example.FileDetail,
      tier: { ...tier, tier_changed_at: null },
      archive_at: OWN_DATE,
    });
    expect(text).toBe("Archived. Reading it needs a recall first.");
  });

  it("the file list counts from when the blob went", () => {
    render(<NextStep entry={{ ...example.Entry, tier, archive_at: OWN_DATE }} />);
    expect(screen.getByText(`Archived ${formatDate(WENT)}`)).toBeTruthy();
  });

  it("the file list says Archived with no clock, not its future date", () => {
    render(
      <NextStep
        entry={{ ...example.Entry, tier: { ...tier, tier_changed_at: null }, archive_at: OWN_DATE }}
      />,
    );
    expect(screen.getByText("Archived")).toBeTruthy();
    expect(screen.queryByText(/Archives/)).toBeNull();
  });
});

// The read-back was denied: a CRC that didn't match, or the worker gave up on it.
describe("a file whose cloud copy could not be checked", () => {
  const FAILED = "2026-11-02T09:00:00.000Z";

  it("the file list says so", () => {
    render(
      <EntryTags entry={{ ...example.Entry, active_recall: null, read_back_failed_at: FAILED }} />,
    );
    const tag = screen.getByText("Not checked");
    expect(tag.getAttribute("title")).toContain(formatDate(FAILED));
    expect(tag.getAttribute("title")).toContain("stays on the file server");
  });

  it("the journey says so, and what it means", () => {
    const text = nextMoves({
      ...example.FileDetail,
      read_back_failed_at: FAILED,
      tier: { ...example.FileDetail.tier, logical: "hot", archived: false },
    });
    expect(text).toContain(`could not be checked on ${formatDate(FAILED)}`);
    expect(text).toContain("stays on the file server");
  });

  it("a file checked as usual says nothing", () => {
    render(
      <EntryTags entry={{ ...example.Entry, active_recall: null, read_back_failed_at: null }} />,
    );
    expect(screen.queryByText("Not checked")).toBeNull();
  });
});
