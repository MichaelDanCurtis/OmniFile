import { useState } from "react";
import type { ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation, useRouteError } from "react-router";
import { useDatasets } from "../api/queries";
import { MoonIcon, SignOutIcon, SunIcon } from "../components/icons";
import { Callout, Empty } from "../components/Feedback";
import { isArchiveAdmin, isDataSteward } from "../lib/permissions";
import { DotField } from "../theme/DotField";
import { Brandmark } from "./Brandmark";
import { JumpBox } from "./JumpBox";
import { MockPersonaSwitcher } from "./MockPersona";
import { navGroups, sectionOf } from "./nav";
import { useSession } from "./session";
import { applyTheme, storedTheme, type ThemeChoice } from "./theme";
import { LoadFailed } from "./lazy";
import { usePageTitle } from "./usePageTitle";

const THEME_NEXT: Record<ThemeChoice, ThemeChoice> = {
  system: "light",
  light: "dark",
  dark: "system",
};
const THEME_LABEL: Record<ThemeChoice, string> = {
  system: "Theme: match system",
  light: "Theme: light",
  dark: "Theme: dark",
};

function initials(name: string | null | undefined): string {
  return (name ?? "?")
    .split(/\s+/)
    .map((part) => part[0] ?? "")
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

function PendingCount() {
  const datasets = useDatasets();
  const count = (datasets.data?.items ?? []).reduce((n, d) => n + (d.pending_proposals ?? 0), 0);
  if (!count) return null;
  return (
    <span className="nav-count" aria-label={`${count} pending`}>
      {count}
    </span>
  );
}

/** Group / section, from the path, in the top bar. */
function TopCrumbs() {
  const { me } = useSession();
  const { pathname } = useLocation();
  const hit = sectionOf(me, pathname);
  if (!hit) return <span className="topcrumbs" />;
  const [group, item] = hit;
  const deeper = pathname !== item.to;
  return (
    <div className="topcrumbs">
      <ol aria-label="You are in">
        <li>{group.label}</li>
        <li>
          {deeper ? (
            <Link to={item.to}>{item.label}</Link>
          ) : (
            <span aria-current="page">{item.label}</span>
          )}
        </li>
      </ol>
    </div>
  );
}

/** `children` replaces the routed page: the root route's error element keeps the nav. */
export function Layout({ children }: { children?: ReactNode } = {}) {
  const { me, signOut } = useSession();
  const { pathname } = useLocation();
  const [theme, setTheme] = useState<ThemeChoice>(storedTheme);
  const groups = navGroups(me);
  const roleLabel = isArchiveAdmin(me)
    ? "Archive admin"
    : isDataSteward(me)
      ? "Data steward"
      : me.owner_of.length > 0
        ? "Share owner"
        : "Reader";

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="sidebar">
        <Link to="/files" className="brand" aria-label="OmniFile Gateway home">
          <Brandmark />
          <span>
            <span className="brand-name">OmniFile</span>
            <span className="brand-sub">Gateway portal</span>
          </span>
        </Link>
        <nav className="nav" aria-label="Main">
          {groups.map((g) => (
            <div className="nav-group" key={g.label}>
              <span className="nav-label">{g.label}</span>
              {g.items.map((i) => (
                <NavLink key={i.to} to={i.to}>
                  {i.icon} {i.label}
                  {i.to === "/proposals" ? <PendingCount /> : null}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          {import.meta.env.MODE === "mock" ? <MockPersonaSwitcher /> : null}
          <div className="whoami">
            <span className="avatar" aria-hidden="true">
              {initials(me.display_name)}
            </span>
            <span className="whoami-name" title={me.upn ?? undefined}>
              {me.display_name ?? me.upn}
            </span>
            <span className="whoami-role">{roleLabel}</span>
          </div>
          <div className="row">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => {
                const next = THEME_NEXT[theme];
                applyTheme(next);
                setTheme(next);
              }}
              aria-label={`${THEME_LABEL[theme]}. Change theme`}
              title={THEME_LABEL[theme]}
            >
              {theme === "dark" ? <MoonIcon /> : <SunIcon />}
              <span className="hide-narrow">
                {theme === "system" ? "Auto" : theme === "dark" ? "Dark" : "Light"}
              </span>
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => void signOut()}>
              <SignOutIcon />
              <span className="hide-narrow">Sign out</span>
            </button>
          </div>
        </div>
      </header>
      <main id="main" className="main" tabIndex={-1} data-dot-host="">
        {/* The one live dot layer: behind every page head, rippling on each navigation. */}
        <div className="backdrop">
          <DotField pulseKey={pathname} pulseAt={[0.2, 0.45]} density={0.5} />
        </div>
        <div className="topbar">
          <TopCrumbs />
          <JumpBox />
        </div>
        {me.groups_source === "pending" ? (
          <div className="page" style={{ paddingBottom: 0 }}>
            <Callout title="Your AD groups are being resolved" role="status">
              You're in more than 200 groups, so the portal reads them in the background. Files and
              folders appear here as soon as that's done; there's nothing you need to do.
            </Callout>
          </div>
        ) : null}
        {children ?? <Outlet />}
      </main>
    </div>
  );
}

/**
 * The root route's error element: a route that throws (or a page chunk that
 * can't load past its own boundary) leaves the portal and its nav standing.
 */
export function RouteError() {
  const error = useRouteError();
  usePageTitle("Something went wrong");
  console.warn("[portal] route error", error);
  return (
    <Layout>
      <div className="page">
        <LoadFailed />
      </div>
    </Layout>
  );
}

export function NotFound() {
  usePageTitle("Not found");
  return (
    <div className="page">
      <Empty title="Nothing here" glyph="404">
        <p>
          The page doesn't exist, or you can't see it. <Link to="/files">Go to your files</Link>.
        </p>
      </Empty>
    </div>
  );
}
