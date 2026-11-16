import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Recall, RecallStep } from "../../../api/types";
import { example } from "../../../mocks/examples.gen";
import { server } from "../../../mocks/server";
import { expectNoAxeViolations } from "../../../test/axe";
import { renderApp } from "../../../test/render";
import { RECALL_POSITION } from "./FileJourney";
import { reduceMotion } from "./testing";

const DIR = "/files/1/101";

async function journeyOf(fileName: string) {
  const drawer = await screen.findByRole("complementary", { name: fileName });
  const section = await within(drawer).findByRole("region", { name: "Where it is, where it goes" });
  return { drawer, section, track: within(section).getByRole("list", { name: /Storage track/ }) };
}

afterEach(() => vi.unstubAllGlobals());

function recallIn(state: Recall["state"]): Recall {
  const order: RecallStep["step"][] = ["requested", "rehydrating", "downloading", "available"];
  const at = { requested: 0, rehydrating: 1, copying: 1, downloading: 2, available: 3 } as Record<
    string,
    number
  >;
  const reached = state === "failed" ? 1 : (at[state] ?? 0);
  return {
    ...example.Recall,
    recall_id: "501",
    file_id: "1001",
    state,
    eta: state === "failed" || state === "available" ? null : "2026-11-05T08:00:00.000Z",
    eta_confidence: state === "failed" || state === "available" ? "none" : "provider_sla",
    failure: state === "failed" ? "The storage provider couldn't finish the restore." : null,
    steps: order.map((step, i) => ({
      step,
      status:
        state === "failed"
          ? i < reached
            ? "done"
            : i === reached
              ? "failed"
              : "pending"
          : i < reached || state === "available"
            ? "done"
            : i === reached
              ? "current"
              : "pending",
      at: null,
    })),
  };
}

describe("file journey", () => {
  it("puts the file on the track with its next move, and passes axe", async () => {
    const { container } = renderApp(`${DIR}?file=1004`, { persona: "reader" });
    const { section, track } = await journeyOf("line_1045.segy");
    const stations = within(track).getAllByRole("listitem");
    expect(stations.map((s) => s.querySelector(".journey-name")?.textContent)).toEqual([
      "Edge cache",
      "Hot",
      "Cool",
      "Cold",
      "Archive",
    ]);
    // Cold is where the bytes are; the gateway also keeps the whole file.
    const here = within(track).getByRole("listitem", { current: "location" });
    expect(here).toHaveTextContent(/Cold/);
    expect(here).toHaveTextContent("the file is here");
    expect(stations[0]).toHaveTextContent("the gateway keeps the whole file");
    // The backend's own tiers: the hint and the provider class in a tooltip.
    await waitFor(() => expect(stations[2]).toHaveTextContent("30-day minimum"));
    expect(stations[2]).toHaveAttribute("title", "Azure Cool (30-day minimum)");
    expect(here).toHaveAttribute("title", "Azure Cold");
    expect(stations[4]).toHaveTextContent("hours to come back");
    expect(within(section).getByText(/In Cold \(Azure Cold\), live account\./)).toBeInTheDocument();
    expect(within(section).getByText(/Moves to Archive on/)).toBeInTheDocument();
    expect(within(section).getByRole("list", { name: "Colour key for the track" })).toBeVisible();
    await expectNoAxeViolations(container);
  });

  it("says Cool then Archive for a file with both moves ahead", async () => {
    renderApp(`${DIR}?file=1005`, { persona: "reader" });
    const { section } = await journeyOf("nav_2026.p190");
    expect(
      within(section).getByText(/Moves to Cold on .+, Archive on .+\./, { exact: false }),
    ).toBeInTheDocument();
  });

  it("shows a pin and a keep-online hold with who set them and until when", async () => {
    // As the DataSteward who set the pin: who and why are shown.
    renderApp(`${DIR}?file=1003`, { persona: "steward" });
    const { section } = await journeyOf("line_1044.segy");
    const holds = await within(section).findByRole("list", { name: "Pins and holds on this file" });
    await waitFor(() => expect(holds).toHaveTextContent(/Pinned to Hot until .+, by Sam Patel on/));
    expect(holds).toHaveTextContent("Reprocessing run for the 2026 velocity model");
    expect(within(section).getByRole("list", { name: /Storage track/ })).toHaveTextContent(
      "pinned to this tier",
    );
  });

  it("limits who-set-it to the roles that may see it (RBAC)", async () => {
    renderApp(`${DIR}?file=1003`, { persona: "reader" });
    let { section } = await journeyOf("line_1044.segy");
    const pins = await within(section).findByRole("list", { name: "Pins and holds on this file" });
    await waitFor(() => expect(pins).toHaveTextContent("set by a share Owner or DataSteward"));
    expect(pins).not.toHaveTextContent("Sam Patel");
    expect(pins).not.toHaveTextContent("Reprocessing run");

    // A keep-online hold the reader set from Teams: theirs, with the 14 days counting down.
    cleanup();
    renderApp(`${DIR}?file=1006`, { persona: "reader" });
    ({ section } = await journeyOf("survey_notes.docx"));
    const kept = await within(section).findByRole("list", { name: "Pins and holds on this file" });
    await waitFor(() =>
      expect(kept).toHaveTextContent(/Kept online, no tier change until .+, set by you/),
    );
    const meter = within(kept).getByRole("meter", { name: "Days of the hold left" });
    expect(meter).toHaveAttribute("aria-valuemax", "14");
    expect(meter).toHaveAttribute("aria-valuetext", "10 of 14 days left");
    expect(within(section).getByText("Kept online")).toBeInTheDocument();
  });

  it.each([
    ["requested", "step 1 of 4, Requested", RECALL_POSITION.requested],
    ["rehydrating", "step 2 of 4, Rehydrating", RECALL_POSITION.rehydrating],
    ["copying", "step 2 of 4, Rehydrating", RECALL_POSITION.rehydrating],
    ["downloading", "step 3 of 4, Downloading", RECALL_POSITION.downloading],
    ["available", "Back on the gateway", RECALL_POSITION.available],
    ["failed", "The recall failed.", 4],
  ] as const)("animates a recall that is %s along the track", async (state, text, pos) => {
    server.use(http.get("*/v1/recalls/:id", () => HttpResponse.json(recallIn(state))));
    renderApp(`${DIR}?file=1001`, { persona: "reader" });
    const { section } = await journeyOf("line_1042.segy");
    expect(
      await within(section).findByText(new RegExp(text.replace(/[()]/g, "\\$&"))),
    ).toBeVisible();
    const travel = within(section).getByTestId("journey-travel");
    expect(travel).toHaveAttribute("data-step", state === "copying" ? "rehydrating" : state);
    expect(travel.style.getPropertyValue("--pos")).toBe(String(pos));
    const steps = within(section).getByRole("list", { name: "Recall steps" });
    expect(within(steps).getAllByRole("listitem")).toHaveLength(4);
    if (state === "rehydrating" || state === "downloading" || state === "requested") {
      // The ETA comes from the recall, with its confidence.
      expect(within(section).getByText(/Expected .+\(provider estimate\)/)).toBeInTheDocument();
    }
  });

  it("draws the same information, still, with reduced motion", async () => {
    reduceMotion(false);
    renderApp(`${DIR}?file=1008`, { persona: "reader" });
    let { section } = await journeyOf("line_1040.segy");
    await within(section).findByText(/step 3 of 4/);
    const full = within(section).getByTestId("journey");
    expect(full).toHaveAttribute("data-motion", "full");
    const fullText = section.textContent;

    cleanup();
    reduceMotion(true);
    renderApp(`${DIR}?file=1008`, { persona: "reader" });
    ({ section } = await journeyOf("line_1040.segy"));
    await within(section).findByText(/step 3 of 4/);
    expect(within(section).getByTestId("journey")).toHaveAttribute("data-motion", "reduce");
    expect(within(section).getByTestId("journey-travel").style.getPropertyValue("--pos")).toBe(
      "0.5",
    );
    expect(section.textContent).toBe(fullText);
  });

  it("gives recall-options' ETAs before a recall, and no time above 10 GB", async () => {
    renderApp(`${DIR}?file=1002`, { persona: "reader" });
    let { section } = await journeyOf("line_1043.segy");
    expect(
      await within(section).findByText(/Standard: by .+\(provider estimate\) · High priority: by/),
    ).toBeInTheDocument();

    cleanup();
    renderApp(`${DIR}?file=1007`, { persona: "reader" });
    ({ section } = await journeyOf("line_0999.segy"));
    expect(
      await within(section).findByText(/Standard: no time promised, as it is over 10 GB\./),
    ).toBeInTheDocument();
  });

  it("shows S3's classes behind the logical names", async () => {
    server.use(
      http.get("*/v1/files/:id/journey", () =>
        HttpResponse.json({
          ...example.FileJourney,
          file_id: "1001",
          backend: {
            kind: "s3",
            role: "source",
            storage_layout: "two_account",
            account: null,
            migration_state: "migrating",
          },
          tiers: [
            {
              logical: "hot",
              classes: [{ native_class: "STANDARD", minimum_days: null, needs_restore: false }],
            },
            {
              logical: "warm",
              classes: [{ native_class: "STANDARD_IA", minimum_days: 30, needs_restore: false }],
            },
            {
              logical: "cold",
              classes: [{ native_class: "GLACIER_IR", minimum_days: 90, needs_restore: false }],
            },
            {
              logical: "archive",
              classes: [
                { native_class: "GLACIER", minimum_days: 90, needs_restore: true },
                { native_class: "DEEP_ARCHIVE", minimum_days: 180, needs_restore: true },
              ],
            },
          ],
          holds: [],
        }),
      ),
    );
    renderApp(`${DIR}?file=1004`, { persona: "reader" });
    const { track } = await journeyOf("line_1045.segy");
    const [, hot, cool, cold, archive] = within(track).getAllByRole("listitem");
    await waitFor(() => expect(hot).toHaveAttribute("title", "S3 Standard"));
    expect(cool).toHaveAttribute("title", "S3 Standard-IA (30-day minimum)");
    expect(cool?.querySelector(".journey-name")).toHaveTextContent("Cool");
    expect(archive).toHaveAttribute(
      "title",
      "S3 Glacier Flexible Retrieval (90-day minimum) or S3 Glacier Deep Archive (180-day minimum)",
    );
    expect(cold).toHaveTextContent("90-day minimum");
  });

  it("spans Hot, Cool and Cold for a file in Smart", async () => {
    const db = renderApp(`${DIR}?file=1006`, { persona: "reader" }).db;
    const f = db.files.get("1006");
    if (f) f.tier = { ...f.tier, logical: "smart", native_class: "Smart" };
    const { track } = await journeyOf("survey_notes.docx");
    const smart = track.querySelectorAll("[data-smart]");
    expect(smart).toHaveLength(3);
    expect(smart[0]).toHaveTextContent("the file is in Smart");
    expect(within(track).queryByRole("listitem", { current: "location" })).toBeNull();
  });

  it("doesn't promise Archive while a pin keeps the file out of it", async () => {
    const { db } = renderApp(`${DIR}?file=1003`, { persona: "steward" });
    const pin = db.pins.find((p) => p.pin_id === "801");
    if (pin) pin.override_archive = true;
    const { section } = await journeyOf("line_1044.segy");
    const next = within(section).getByText(/^Next/).closest("p");
    expect(next).toHaveTextContent(/Its pin keeps it out of Archive until /);
    expect(next).not.toHaveTextContent(/Archive on/);
  });

  it("shows an unknown backend's tier raw, never blank (decision 35)", async () => {
    renderApp("/files/4/400?file=4001", { persona: "admin" });
    const { section } = await journeyOf("core_scan_0007.tif");
    expect(
      within(section).getByText(/In capacity \(FabricPool capacity tier\)/),
    ).toBeInTheDocument();
    expect(within(section).getByText(/isn't on this track/)).toBeInTheDocument();
  });
});
