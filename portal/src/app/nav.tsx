import type { ReactNode } from "react";
import type { Me } from "../api/types";
import {
  ActivityIcon,
  AnnexIcon,
  AuditIcon,
  EdgeIcon,
  FolderIcon,
  InboxIcon,
  MigrateIcon,
  PolicyIcon,
  RecallIcon,
  ShareIcon,
  ShieldIcon,
} from "../components/icons";
import {
  canSeeAudit,
  canSeePolicies,
  canSeeShareSettings,
  isArchiveAdmin,
  isDataSteward,
} from "../lib/permissions";

export interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  /** Words the ⌘K box also matches. */
  keywords?: string;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

/**
 * The sidebar's sections (WP17). Who sees what is unchanged from before:
 * each item has the same permission check it always had; a section whose
 * items are all hidden is not drawn.
 */
export function navGroups(me: Me): NavGroup[] {
  const policies = canSeePolicies(me);
  const admin = isArchiveAdmin(me);
  const groups: NavGroup[] = [
    {
      label: "Overview",
      items: [
        { to: "/files", label: "Files", icon: <FolderIcon />, keywords: "browse folders shares" },
        ...(admin ? [{ to: "/activity", label: "Activity", icon: <ActivityIcon /> }] : []),
      ],
    },
    {
      label: "Data",
      items: [
        ...(canSeeShareSettings(me)
          ? [{ to: "/shares", label: "Shares", icon: <ShareIcon /> }]
          : []),
        ...(policies
          ? [
              {
                to: "/policies",
                label: "Policies",
                icon: <PolicyIcon />,
                keywords: "tiers archive",
              },
            ]
          : []),
        ...(admin || isDataSteward(me)
          ? [{ to: "/annexes", label: "Annexes", icon: <AnnexIcon />, keywords: "nas" }]
          : []),
        ...(admin
          ? [{ to: "/migrations", label: "Migrations", icon: <MigrateIcon />, keywords: "s3" }]
          : []),
      ],
    },
    {
      label: "Operations",
      items: [
        { to: "/recalls", label: "Recalls", icon: <RecallIcon /> },
        ...(policies ? [{ to: "/proposals", label: "Proposals", icon: <InboxIcon /> }] : []),
        ...(canSeeAudit(me) ? [{ to: "/audit", label: "Audit", icon: <AuditIcon /> }] : []),
        ...(admin ? [{ to: "/edges", label: "Edges", icon: <EdgeIcon /> }] : []),
      ],
    },
    {
      label: "Admin",
      items: admin ? [{ to: "/settings", label: "Settings", icon: <ShieldIcon /> }] : [],
    },
  ];
  return groups.filter((g) => g.items.length > 0);
}

/** Pages the ⌘K box can open beyond the sidebar: the same links the pages already offer. */
export function jumpTargets(me: Me): (NavItem & { group: string })[] {
  const out = navGroups(me).flatMap((g) => g.items.map((i) => ({ ...i, group: g.label })));
  if (isArchiveAdmin(me)) {
    out.push(
      { to: "/migrations/new", label: "New migration", icon: null, group: "Data" },
      { to: "/migrations/agents", label: "Migration agents", icon: null, group: "Data" },
      { to: "/migrations/plans", label: "Migration plans", icon: null, group: "Data" },
    );
  }
  if (isArchiveAdmin(me) || isDataSteward(me)) {
    out.push({ to: "/annexes/new", label: "Create an Annex", icon: null, group: "Data" });
  }
  return out;
}

/** The section a path belongs to, for the breadcrumb: [group, item]. */
export function sectionOf(me: Me, path: string): [NavGroup, NavItem] | null {
  for (const g of navGroups(me)) {
    for (const i of g.items) {
      if (path === i.to || path.startsWith(`${i.to}/`)) return [g, i];
    }
  }
  return null;
}
