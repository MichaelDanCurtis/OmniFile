import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { SearchIcon } from "../components/icons";
import { useSession } from "./session";
import { jumpTargets } from "./nav";

function isMac(): boolean {
  try {
    return /Mac|iPhone|iPad/.test(navigator.userAgent);
  } catch {
    return false;
  }
}

/**
 * The top bar's search box (WP17). The portal has no search of its own, so
 * this jumps to a page: the ones in the sidebar, and the few sub-pages the
 * pages already link to, filtered by the same permissions. ⌘K (Ctrl+K) puts
 * the cursor here from anywhere. An ARIA combobox: arrows move, Enter opens,
 * Escape closes.
 */
export function JumpBox() {
  const { me } = useSession();
  const navigate = useNavigate();
  const targets = useMemo(() => jumpTargets(me), [me]);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  const listId = `${id}-list`;
  const mac = useMemo(() => isMac(), []);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return targets;
    return targets.filter((t) =>
      `${t.label} ${t.group} ${t.keywords ?? ""}`.toLowerCase().includes(q),
    );
  }, [query, targets]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        input.current?.focus();
        input.current?.select();
        setOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (to: string) => {
    setOpen(false);
    setQuery("");
    input.current?.blur();
    void navigate(to);
  };
  const shown = open && matches.length > 0;
  const current = Math.min(active, Math.max(0, matches.length - 1));

  return (
    <div className="jump" data-open={shown || undefined}>
      <label htmlFor={id} className="sr-only">
        Go to a page
      </label>
      <SearchIcon />
      <input
        ref={input}
        id={id}
        type="text"
        role="combobox"
        autoComplete="off"
        spellCheck={false}
        placeholder="Go to a page"
        aria-expanded={shown}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={shown ? `${id}-opt-${current}` : undefined}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setOpen(true);
            setActive((current + 1) % Math.max(1, matches.length));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((current - 1 + matches.length) % Math.max(1, matches.length));
          } else if (e.key === "Enter") {
            const hit = matches[current];
            if (shown && hit) {
              e.preventDefault();
              go(hit.to);
            }
          } else if (e.key === "Escape") {
            if (open) {
              e.preventDefault();
              setOpen(false);
            } else {
              setQuery("");
              input.current?.blur();
            }
          }
        }}
      />
      <kbd className="jump-key" aria-hidden="true">
        {mac ? "⌘K" : "Ctrl K"}
      </kbd>
      <div id={listId} role="listbox" aria-label="Pages" className="jump-list" hidden={!shown}>
        {shown
          ? matches.map((t, i) => (
              // Keys are the combobox's (arrows, Enter); a click is the pointer's way in.
              // eslint-disable-next-line jsx-a11y/click-events-have-key-events
              <div
                key={t.to}
                id={`${id}-opt-${i}`}
                role="option"
                tabIndex={-1}
                aria-selected={i === current}
                className="jump-opt"
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setActive(i)}
                onClick={() => go(t.to)}
              >
                <span>{t.label}</span>
                <span className="jump-group">{t.group}</span>
              </div>
            ))
          : null}
      </div>
    </div>
  );
}
