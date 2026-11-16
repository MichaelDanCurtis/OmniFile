import { useId, useState, type ReactNode } from "react";

/**
 * Detail a table cell would otherwise hide in a native `title=` (WP18, from #60's
 * review): a native tooltip can't be reached from the keyboard, and screen readers
 * rarely read it. Here a small button follows the content; it is in the tab order
 * and described by the detail, which shows on hover and on focus and goes away
 * with Escape (WCAG 1.4.13). The detail stays out of the reading order until shown,
 * so a cell's own screen-reader text isn't read twice.
 */
export function Tip({
  tip,
  children,
  label = "Details",
}: {
  tip: ReactNode;
  children?: ReactNode;
  /** The button's name, read before the detail. */
  label?: string;
}) {
  const id = useId();
  const [dismissed, setDismissed] = useState(false);
  return (
    <span className="tip" data-dismissed={dismissed || undefined}>
      {children}
      <button
        type="button"
        className="tip-btn"
        aria-label={label}
        aria-describedby={id}
        onKeyDown={(e) => {
          if (e.key === "Escape") setDismissed(true);
        }}
        onFocus={() => setDismissed(false)}
        onBlur={() => setDismissed(false)}
        onPointerEnter={() => setDismissed(false)}
      >
        <span aria-hidden="true">i</span>
      </button>
      <span role="tooltip" id={id} className="tip-pop">
        {tip}
      </span>
    </span>
  );
}
