import type { ReactNode } from "react";
import { NavLink } from "react-router";

/**
 * A page's head (WP17): the title, the one question the page answers, and
 * optionally a longer lede, the page's actions and its hero number. The dot
 * field behind it belongs to the shell (Layout), so it survives navigation.
 */
export function PageHead({
  title,
  question,
  lede,
  crumbs,
  actions,
  hero,
  ledeClassName,
}: {
  title: ReactNode;
  question?: ReactNode;
  lede?: ReactNode;
  crumbs?: ReactNode;
  actions?: ReactNode;
  hero?: ReactNode;
  ledeClassName?: string;
}) {
  return (
    <div className="page-head">
      <div className="page-head-main">
        {crumbs}
        <h1>{title}</h1>
        {question ? <p className="page-question">{question}</p> : null}
        {lede ? <p className={ledeClassName ? `lede ${ledeClassName}` : "lede"}>{lede}</p> : null}
      </div>
      {hero || actions ? (
        <div className="page-head-aside">
          {actions ? <div className="actions">{actions}</div> : null}
          {hero}
        </div>
      ) : null}
    </div>
  );
}

export type HeroTone = "neutral" | "ok" | "warn" | "signal";

/**
 * The page's one hero number, in Doto. Colour only when the number is a
 * status (`tone`). The label says what it counts, in plain words.
 */
export function HeroMetric({
  label,
  value,
  unit,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: string;
  unit?: string;
  sub?: ReactNode;
  tone?: HeroTone;
}) {
  return (
    <p className="hero-metric" data-tone={tone}>
      <span className="hero-label">{label}</span>
      <span className="hero-value">
        <span className="hero-num">{value}</span>
        {unit ? <span className="hero-unit">{unit}</span> : null}
      </span>
      {sub ? <span className="hero-sub">{sub}</span> : null}
    </p>
  );
}

/** Sub-pages of one area, drawn as tabs along the top (Migrations, Settings). */
export function Tabs({
  label,
  items,
}: {
  label: string;
  items: { to: string; label: string; end?: boolean }[];
}) {
  return (
    <nav className="tabs" aria-label={label}>
      {items.map((t) => (
        <NavLink key={t.to} to={t.to} end={t.end} className="tab">
          {t.label}
        </NavLink>
      ))}
    </nav>
  );
}

/** A row of filter pills, with a mono caps label for what they filter. */
export function Pills({ label, children }: { label?: string; children: ReactNode }) {
  return (
    <div className="pills">
      {label ? <span className="pills-label">{label}</span> : null}
      {children}
    </div>
  );
}
