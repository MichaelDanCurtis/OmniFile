import { NavLink, Outlet } from "react-router";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout } from "../../components/Feedback";
import { isArchiveAdmin } from "../../lib/permissions";

export const SETTINGS_PAGES = [
  { path: "storage", label: "Storage" },
  { path: "aws", label: "AWS" },
  { path: "azure", label: "Azure" },
  { path: "identity", label: "Identity" },
  { path: "teams", label: "Teams" },
  { path: "email", label: "Email fallback" },
  { path: "storage-mover", label: "Storage Mover" },
  { path: "prices-alerts", label: "Prices and alerts" },
  { path: "api-clients", label: "API clients" },
  { path: "filers", label: "Filers" },
  { path: "annex-defaults", label: "Annex defaults" },
] as const;

/**
 * Settings (portal-api.md 5.20 and 5.21): ArchiveAdmin only. They connect and
 * check: Azure through the service's managed identity and RBAC, AWS through
 * web-identity federation, Teams through the bot's identity. Nothing here
 * stores a key, a secret or a password.
 */
export function SettingsLayout() {
  usePageTitle("Settings");
  const { me } = useSession();
  if (!isArchiveAdmin(me)) {
    return (
      <div className="page">
        <h1>Settings</h1>
        <Callout tone="warn" role="alert">
          Settings are for ArchiveAdmins.
        </Callout>
      </div>
    );
  }
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="page-question">Is every connection set up, and does it work?</p>
          <p className="lede">
            Storage, AWS and Teams need no keys or secrets: the service uses its own managed
            identities.
          </p>
        </div>
      </div>
      <nav className="tabs" aria-label="Settings">
        {SETTINGS_PAGES.map((p) => (
          <NavLink key={p.path} to={`/settings/${p.path}`} className="tab">
            {p.label}
          </NavLink>
        ))}
      </nav>
      <Outlet />
    </div>
  );
}
