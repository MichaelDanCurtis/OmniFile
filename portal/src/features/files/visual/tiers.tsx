import { useEffect, useState } from "react";
import { isKnownTier, tierLabel } from "../../../lib/format";
import type { FileJourney } from "./queries";

/**
 * Shared by the three visual views (file journey, share map, policy timeline):
 * the stations of the track, the provider's names for its classes, the legend,
 * and the reduced-motion switch. Colours are the tier tokens of tokens.css, the
 * same everywhere, so a tier always looks the same.
 */

export type Station = "edge" | "hot" | "warm" | "cold" | "archive";
export const STATIONS: Station[] = ["edge", "hot", "warm", "cold", "archive"];

/** Provider class names as people read them (AGENTS.md, Backends table). Unknown: raw. */
const CLASS_NAME: Record<string, string> = {
  Hot: "Azure Hot",
  Cool: "Azure Cool",
  Cold: "Azure Cold",
  Archive: "Azure Archive",
  Smart: "Azure Smart tier",
  STANDARD: "S3 Standard",
  STANDARD_IA: "S3 Standard-IA",
  GLACIER_IR: "S3 Glacier Instant Retrieval",
  GLACIER: "S3 Glacier Flexible Retrieval",
  DEEP_ARCHIVE: "S3 Glacier Deep Archive",
  INTELLIGENT_TIERING: "S3 Intelligent-Tiering",
};

export function className(native: string | null | undefined): string {
  if (!native) return "";
  return Object.hasOwn(CLASS_NAME, native) ? (CLASS_NAME[native] ?? native) : native;
}

export type Ladder = FileJourney["tiers"];

/** The provider's classes behind a logical tier, from the backend's capabilities. */
export function classesOf(ladder: Ladder | undefined, tier: string) {
  return ladder?.find((t) => t.logical === tier)?.classes ?? [];
}

/** "S3 Glacier Flexible Retrieval (90-day minimum) or S3 Glacier Deep Archive (180-day minimum)". */
export function providerText(ladder: Ladder | undefined, tier: string): string {
  return classesOf(ladder, tier)
    .map(
      (c) =>
        `${className(c.native_class)}${c.minimum_days ? ` (${c.minimum_days}-day minimum)` : ""}`,
    )
    .join(" or ");
}

/** A station's short promise: how quickly it reads, and its billing minimum. */
export function stationHint(station: Station, ladder: Ladder | undefined): string | null {
  if (station === "edge") return "local, instant";
  if (!ladder || ladder.length === 0) return null; // capabilities unknown: say nothing
  const classes = classesOf(ladder, station);
  if (classes.length === 0) return "not offered";
  if (classes.some((c) => c.needs_restore)) return "hours to come back";
  const min = Math.min(...classes.map((c) => c.minimum_days ?? 0));
  return min > 0 ? `${min}-day minimum` : "instant";
}

export function stationName(station: Station): string {
  return station === "edge" ? "Edge cache" : tierLabel(station);
}

export const tierAttr = (tier: string | null | undefined) =>
  tier && (isKnownTier(tier) || tier === "edge") ? tier : "other";

/** True when the user asked for less motion: the views then draw the same information, still. */
export function useReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const get = () =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false;
  const [reduced, setReduced] = useState(get);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(query);
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}

/** The colour key, the same in every view. */
export function TierLegend({
  tiers = ["edge", "hot", "warm", "cold", "archive", "smart"],
  label = "Colour key",
}: {
  tiers?: string[];
  label?: string;
}) {
  return (
    <ul className="vis-legend" aria-label={label}>
      {tiers.map((t) => (
        <li key={t}>
          <span className="vis-swatch" data-tier={t} aria-hidden="true" />
          {t === "edge" ? "Edge cache" : tierLabel(t)}
        </li>
      ))}
    </ul>
  );
}
