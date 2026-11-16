import { useState } from "react";
import { useRecall, useRecallOptions } from "../../../api/queries";
import type { FileDetail, PersonRef, Recall, RecallOptions } from "../../../api/types";
import { useSession } from "../../../app/session";
import { ClockIcon, PinIcon, RecallIcon, ShieldIcon } from "../../../components/icons";
import {
  ETA_CONFIDENCE_LABEL,
  accountLabel,
  formatBytes,
  formatDate,
  formatDateTime,
  priorityLabel,
  tierLabel,
} from "../../../lib/format";
import { needsRecall } from "../EntryBits";
import "./visual.css";
import { useFileJourney, type FileJourney as Journey } from "./queries";
import {
  STATIONS,
  TierLegend,
  className,
  providerText,
  stationHint,
  stationName,
  useReducedMotion,
  type Ladder,
  type Station,
} from "./tiers";

const DAY = 86_400_000;

/** Where along the track (0 = Edge … 4 = Archive) each recall step puts the copy coming back. */
export const RECALL_POSITION = { requested: 4, rehydrating: 2.5, downloading: 0.5, available: 0 };
const STEP_NAME = {
  requested: "Requested",
  rehydrating: "Rehydrating",
  downloading: "Downloading",
  available: "Available",
} as const;
type Step = keyof typeof STEP_NAME;

const MOVE: Record<string, string> = {
  tier_warm: "Moves to Cool",
  tier_cold: "Moves to Cold",
  archive: "Moves to Archive",
  stub: "Frees the gateway copy",
  prune_versions: "Prunes old versions",
};

/** "Moves to Cold on 21 Nov 2026, Archive on 18 Feb 2027." From the scheduler's own fields. */
export function nextMoves(f: FileDetail): string {
  if (needsRecall(f)) {
    // When the blob really went: a file with the same bytes can archive it before this file's date.
    const since = f.tier.tier_changed_at;
    return `Archived${since ? ` since ${formatDate(since)}` : ""}. Reading it needs a recall first.`;
  }
  const parts: string[] = [];
  const unchecked = f.read_back_failed_at
    ? `Its cloud copy could not be checked on ${formatDate(f.read_back_failed_at)}, so it stays on the file server until an administrator tries again. `
    : "";
  if (f.next_action && f.next_action_at) {
    parts.push(`${MOVE[f.next_action] ?? f.next_action} on ${formatDate(f.next_action_at)}`);
  }
  // A pin with override_archive keeps the file out of Archive past its archive date.
  const held = !!f.pin?.override_archive;
  if (f.archive_at && !held && f.next_action !== "archive" && f.tier.logical !== "archive") {
    parts.push(`${parts.length ? "Archive" : "Moves to Archive"} on ${formatDate(f.archive_at)}`);
  }
  const hold = f.move_hold
    ? `Its move to ${tierLabel(f.move_hold.to)} waits until ${formatDate(f.move_hold.until)}: it shares its storage with another file and couldn't get a copy of its own. It tries again then.`
    : "";
  const planned = parts.length ? `${parts.join(", ")}.` : hold ? "" : "No move planned.";
  const moves = unchecked + [hold, planned].filter(Boolean).join(" ");
  return held && f.pin
    ? `${moves} Its pin keeps it out of Archive ${f.pin.until ? `until ${formatDate(f.pin.until)}` : "until it is released"}.`
    : moves;
}

function position(f: FileDetail): number | null {
  if (f.state === "landing" || f.state === "uploading") return 0;
  const t = f.tier.logical;
  if (t === "smart") return 2;
  const i = STATIONS.indexOf(t as Station);
  if (i > 0) return i;
  return t ? null : 0; // an unknown tier has no place on this track
}

function edgeText(f: FileDetail): string {
  switch (f.state) {
    case "resident":
      return "Whole file on the gateway";
    case "stub":
    case "recalling":
      return `Stub: first ${formatBytes(f.resident_bytes ?? 0)} kept`;
    case "landing":
    case "uploading":
      return "Arriving: being uploaded";
    case "remote":
      return "Not kept on the gateway";
    default:
      return f.state;
  }
}

function whoText(by: PersonRef | null | undefined, fallback: string): string {
  if (!by) return fallback;
  if (by.kind === "application") return `by ${by.app_id ?? "an application"}`;
  if (by.kind === "system") return "by OmniFile";
  return `by ${by.display_name ?? "someone"}`;
}

function currentStep(recall: Recall): Step | "failed" {
  if (recall.state === "available") return "available";
  if (recall.state === "failed") return "failed";
  const s = recall.steps.find((x) => x.status === "current");
  return s?.step ?? "requested";
}

/**
 * The file's journey, in the drawer: a track from the gateway's cache through
 * the backend's tiers, the file's place on it, its next move, any pin or hold,
 * and an active recall travelling back along the four steps.
 */
export function FileJourney({ file }: { file: FileDetail }) {
  const reduced = useReducedMotion();
  const journey = useFileJourney(file.file_id);
  const recall = useRecall(file.active_recall?.recall_id);
  const archived = needsRecall(file);
  const options = useRecallOptions(archived && !file.active_recall ? file.file_id : null);
  const j = journey.data;
  const ladder = j?.tiers;
  const at = position(file);
  const r = file.active_recall ? recall.data : undefined;
  const step = r ? currentStep(r) : null;
  const travel = step ? (step === "failed" ? 4 : RECALL_POSITION[step]) : null;
  const pinTier = j?.pin?.tier ?? file.pin?.tier ?? null;
  const smart = file.tier.logical === "smart";
  const keep = (j?.holds ?? []).filter((h) => h.kind === "keep_online");

  const native = file.tier.native_class;
  const account = j?.backend.account;
  const where =
    at === 0 && (file.state === "landing" || file.state === "uploading")
      ? "Arriving on the gateway; it goes to the cloud once the write is complete."
      : file.tier.logical
        ? `In ${tierLabel(file.tier.logical)}${native ? ` (${className(native)})` : ""}${
            account && account !== "single" ? `, ${accountLabel(account)}` : ""
          }.`
        : "No cloud copy yet.";

  return (
    <section className="drawer-section" aria-labelledby="journey-title">
      <h3 id="journey-title">Where it is, where it goes</h3>
      <p className="journey-now">
        <strong>{where}</strong> {edgeText(file)}.
      </p>
      <div
        className="journey"
        data-motion={reduced ? "reduce" : "full"}
        data-testid="journey"
        style={{ ["--n" as string]: STATIONS.length }}
      >
        <ol className="journey-track" aria-label="Storage track, from the gateway to Archive">
          {STATIONS.map((s, i) => (
            <StationItem
              key={s}
              station={s}
              ladder={ladder}
              here={at === i || (smart && i >= 1 && i <= 3)}
              smart={smart && i >= 1 && i <= 3}
              cached={i === 0 && file.state === "resident"}
              stub={i === 0 && (file.state === "stub" || file.state === "recalling")}
              pinned={pinTier === s}
              current={i === at ? (native ?? null) : null}
            />
          ))}
        </ol>
        {at !== null ? (
          <span
            className="journey-marker"
            data-tier={smart ? "smart" : (STATIONS[at] ?? "other")}
            style={{ ["--pos" as string]: at }}
            aria-hidden="true"
          />
        ) : null}
        {travel !== null ? (
          <span
            className="journey-travel"
            data-step={step}
            data-testid="journey-travel"
            style={{ ["--pos" as string]: travel }}
            aria-hidden="true"
          >
            <RecallIcon size={12} />
          </span>
        ) : null}
        {keep.length > 0 ? (
          <span className="journey-band" aria-hidden="true">
            Kept online
          </span>
        ) : null}
      </div>
      {at === null ? (
        <p className="muted">
          This backend's tier <span className="mono">{file.tier.logical}</span>
          {native ? ` (${native})` : ""} isn't on this track.
        </p>
      ) : null}

      <p className="journey-next">
        <span className="micro">Next</span> {nextMoves(file)}
        {file.tier.logical === "archive" && !file.tier.archived && file.tier.restore_expires_at
          ? ` A restored copy is readable until ${formatDateTime(file.tier.restore_expires_at)}.`
          : ""}
      </p>

      {r && step ? <RecallTrack recall={r} step={step} /> : null}
      {archived && !file.active_recall && options.data ? (
        <RecallTimes options={options.data} />
      ) : null}

      <PinsAndHolds file={file} journey={j} />
      <TierLegend label="Colour key for the track" />
    </section>
  );
}

function StationItem({
  station,
  ladder,
  here,
  smart,
  cached,
  stub,
  pinned,
  current,
}: {
  station: Station;
  ladder: Ladder | undefined;
  here: boolean;
  smart: boolean;
  cached: boolean;
  stub: boolean;
  pinned: boolean;
  current: string | null;
}) {
  const hint = stationHint(station, ladder);
  const provider =
    station === "edge"
      ? "The OmniFile gateway's local disk"
      : current
        ? className(current)
        : providerText(ladder, station);
  return (
    <li
      className="journey-station"
      data-tier={station}
      data-here={here ? "" : undefined}
      data-smart={smart ? "" : undefined}
      data-cached={cached || stub ? (cached ? "whole" : "stub") : undefined}
      aria-current={here && !smart ? "location" : undefined}
      title={provider || undefined}
    >
      <span className="journey-dot" aria-hidden="true" />
      <span className="journey-name">
        {stationName(station)}
        {pinned ? <PinIcon size={11} /> : null}
      </span>
      {hint ? <span className="journey-hint">{hint}</span> : null}
      <span className="sr-only">
        {provider ? `, ${provider}` : ""}
        {here
          ? smart
            ? ", the file is in Smart, which moves it among Hot, Cool and Cold"
            : ", the file is here"
          : ""}
        {cached ? ", and the gateway keeps the whole file" : ""}
        {stub ? ", and the gateway keeps a stub" : ""}
        {pinned ? ", pinned to this tier" : ""}
      </span>
    </li>
  );
}

function RecallTrack({ recall, step }: { recall: Recall; step: Step | "failed" }) {
  const order: Step[] = ["requested", "rehydrating", "downloading", "available"];
  const n = step === "failed" ? -1 : order.indexOf(step);
  return (
    <div className="journey-recall" data-step={step}>
      <p>
        <RecallIcon size={14} />{" "}
        <strong>
          {step === "failed"
            ? "The recall failed."
            : step === "available"
              ? "Back on the gateway: open it as usual."
              : `Coming back: step ${n + 1} of 4, ${STEP_NAME[step]}.`}
        </strong>{" "}
        {step === "failed" || step === "available"
          ? (recall.failure ?? "")
          : recall.eta
            ? `Expected ${formatDateTime(recall.eta)} (${ETA_CONFIDENCE_LABEL[recall.eta_confidence ?? "none"]}).`
            : "No time is promised for this file."}
      </p>
      <ol className="journey-steps" aria-label="Recall steps">
        {recall.steps.map((s) => (
          <li
            key={s.step}
            data-status={s.status}
            aria-current={s.status === "current" ? "step" : undefined}
          >
            {STEP_NAME[s.step]}
            <span className="sr-only">
              {" "}
              {s.status === "done"
                ? "done"
                : s.status === "current"
                  ? "in progress"
                  : s.status === "failed"
                    ? "failed"
                    : "not yet"}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Before a recall: what each offered priority promises (never above the no-promise size). */
function RecallTimes({ options }: { options: RecallOptions }) {
  const offered = options.options.filter((o) => o.offered);
  if (offered.length === 0) return null;
  const big =
    options.no_promise_above_bytes !== null &&
    options.no_promise_above_bytes !== undefined &&
    options.size > options.no_promise_above_bytes;
  return (
    <p className="journey-times">
      <span className="micro">Coming back</span>{" "}
      {offered
        .map((o) =>
          o.eta
            ? `${priorityLabel(o.priority)}: by ${formatDateTime(o.eta)} (${ETA_CONFIDENCE_LABEL[o.eta_confidence ?? "none"]})`
            : `${priorityLabel(o.priority)}: no time promised`,
        )
        .join(" · ")}
      {big && offered.every((o) => !o.eta)
        ? `, as it is over ${formatBytes(options.no_promise_above_bytes)}.`
        : "."}
    </p>
  );
}

function PinsAndHolds({ file, journey }: { file: FileDetail; journey: Journey | undefined }) {
  const { me } = useSession();
  const pin = journey?.pin;
  const holds = journey?.holds ?? [];
  const [now] = useState(Date.now);
  if (!pin && !file.pin && holds.length === 0) return null;
  return (
    <ul className="journey-holds" aria-label="Pins and holds on this file">
      {pin ? (
        <li>
          <PinIcon size={14} />
          <span>
            Pinned to <strong>{tierLabel(pin.tier)}</strong>{" "}
            {pin.until ? `until ${formatDate(pin.until)}` : "until released"},{" "}
            {whoText(pin.set_by, "set by a share Owner or DataSteward")} on {formatDate(pin.set_at)}
            {pin.inherited_from ? ", on a folder above" : ""}.
            {pin.reason ? <span className="muted"> {pin.reason}</span> : null}
          </span>
        </li>
      ) : file.pin ? (
        <li>
          <PinIcon size={14} />
          <span>
            Pinned to <strong>{tierLabel(file.pin.tier)}</strong>{" "}
            {file.pin.until ? `until ${formatDate(file.pin.until)}` : "until released"}.
          </span>
        </li>
      ) : null}
      {holds.map((h) => {
        const total = h.until ? new Date(h.until).getTime() - new Date(h.set_at).getTime() : null;
        const left = h.until ? Math.max(0, new Date(h.until).getTime() - now) : null;
        const days = total ? Math.round(total / DAY) : null;
        const leftDays = left !== null ? Math.ceil(left / DAY) : null;
        const mine = h.set_by?.identity_id && h.set_by.identity_id === me.identity_id;
        return (
          <li key={h.hold_id}>
            {h.kind === "legal" ? <ShieldIcon size={14} /> : <ClockIcon size={14} />}
            <span className="stack" style={{ gap: 4 }}>
              <span>
                {h.kind === "legal" ? "Legal hold" : "Kept online, no tier change"}{" "}
                {h.until ? `until ${formatDate(h.until)}` : "until released"},{" "}
                {mine
                  ? "set by you"
                  : whoText(
                      h.set_by,
                      h.kind === "legal" ? "set by an ArchiveAdmin" : "set by someone else",
                    )}{" "}
                on {formatDate(h.set_at)}
                {h.inherited_from ? ", on a folder above" : ""}.
                {h.reason ? <span className="muted"> {h.reason}</span> : null}
              </span>
              {h.kind === "keep_online" && days && leftDays !== null ? (
                <span className="journey-meter">
                  <span
                    className="journey-meter-bar"
                    role="meter"
                    aria-label="Days of the hold left"
                    aria-valuemin={0}
                    aria-valuemax={days}
                    aria-valuenow={leftDays}
                    aria-valuetext={`${leftDays} of ${days} days left`}
                  >
                    <span style={{ width: `${Math.min(100, (leftDays / days) * 100)}%` }} />
                  </span>
                  <span className="muted tabular">
                    {leftDays} of {days} days left
                  </span>
                </span>
              ) : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
