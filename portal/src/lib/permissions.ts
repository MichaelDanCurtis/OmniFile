import type { Me, Share } from "../api/types";

/**
 * What the signed-in user may do, from their app roles and share roles
 * (portal-api.md 3.1 and 5.6). The UI hides what a role can't do; the server
 * enforces every rule anyway, and its 403s are shown when they happen.
 */
export function isArchiveAdmin(me: Me): boolean {
  return me.roles.includes("ArchiveAdmin");
}

/** Settings, Prices and alerts (portal-api.md 5.24): an ArchiveAdmin may hide costs from a role. */
export function canSeePrices(me: Me): boolean {
  return me.prices_visible !== false;
}

export function isDataSteward(me: Me): boolean {
  return me.roles.includes("DataSteward");
}

export function isShareOwner(me: Me, share: Pick<Share, "share_id" | "my_access">): boolean {
  return share.my_access.owner || me.owner_of.includes(share.share_id);
}

export function canPin(me: Me, share: Share): boolean {
  return isShareOwner(me, share) || isDataSteward(me);
}

/** Portal keep_online holds: Owners or DataStewards (portal-api.md 5.6). */
export function canKeepOnline(me: Me, share: Share): boolean {
  return canPin(me, share);
}

export function canOverrideArchive(me: Me, share: Share): boolean {
  return isShareOwner(me, share);
}

export function canRestoreVersion(_me: Me, share: Share): boolean {
  return share.my_access.owner || share.my_access.writer;
}

export function canEditOpenPolicy(me: Me, share: Share): boolean {
  return isShareOwner(me, share) || isArchiveAdmin(me);
}

export const canEditRewindDepth = canEditOpenPolicy;

export function canEditShareSecurity(me: Me): boolean {
  return isArchiveAdmin(me);
}

export function canEditAccess(me: Me, share: Share, group: "owners" | "writers" | "readers") {
  if (isArchiveAdmin(me)) return true;
  return group !== "owners" && isShareOwner(me, share);
}

export function canSeePolicies(me: Me): boolean {
  return isArchiveAdmin(me) || isDataSteward(me);
}

export function canSeeAudit(me: Me): boolean {
  return isArchiveAdmin(me) || me.owner_of.length > 0;
}

export function canSeeShareSettings(me: Me): boolean {
  return isArchiveAdmin(me) || me.owner_of.length > 0;
}
