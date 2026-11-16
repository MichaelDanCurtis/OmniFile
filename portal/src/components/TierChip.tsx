import type { LogicalTier, TierState } from "../api/types";
import { TIER_ORDER, isKnownTier, tierLabel } from "../lib/format";
import { LockIcon } from "./icons";

/**
 * A tier as a chip with a four-cell thermal meter: how far down the strata
 * (Hot → Cool → Cold → Archive) the bytes are. Smart lights the top three.
 */
export function TierChip({
  tier,
  showNative = true,
}: {
  tier: TierState | LogicalTier | null | undefined;
  showNative?: boolean;
}) {
  if (!tier) return <span className="muted">—</span>;
  const state: TierState = typeof tier === "string" ? { logical: tier, archived: false } : tier;
  const logical = state.logical;
  if (!logical) return <span className="muted">—</span>;
  const label = tierLabel(logical);
  const native =
    showNative && state.native_class && state.native_class !== label ? state.native_class : null;
  const lit = logical === "smart" ? [0, 1, 2] : [TIER_ORDER.indexOf(logical)];
  const restored = typeof tier !== "string" && logical === "archive" && !state.archived;
  return (
    <span className="tier" data-tier={isKnownTier(logical) ? logical : "other"}>
      <span className="thermo" aria-hidden="true">
        {TIER_ORDER.map((t, i) => (
          <i key={t} data-on={lit.includes(i) ? "" : undefined} />
        ))}
      </span>
      <span>
        {label}
        {native ? <span className="tier-native"> · {native}</span> : null}
        {restored ? <span className="tier-native"> · restored copy</span> : null}
      </span>
      {state.archived ? (
        <>
          <LockIcon size={12} />
          <span className="sr-only">(needs a recall to read)</span>
        </>
      ) : null}
    </span>
  );
}
