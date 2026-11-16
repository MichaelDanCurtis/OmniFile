import { useEffect, useRef } from "react";
import { DotEngine, media, reducedMotion, type DotOptions } from "./dots";

/**
 * The dot layer (WP17): a field of tiny dots that drifts, ripples and leans
 * away from the pointer, or a glyph drawn in dots for a hero moment. It is
 * decoration: hidden from assistive technology unless `label` names it, and
 * whatever it shows is also on the page as text.
 *
 * It stops when the tab is hidden or the canvas scrolls away, and stands still
 * under prefers-reduced-motion. Use it sparingly: the page header, empty
 * states, the sign-in screen.
 */
export function DotField({
  className,
  pulseKey,
  pulseAt = [0.12, 0.5],
  label,
  ...opts
}: Partial<DotOptions> & {
  className?: string;
  /** A change starts a ripple (the shell passes the route). */
  pulseKey?: unknown;
  /** Where ripples start, as fractions of the canvas. */
  pulseAt?: [number, number];
  /** Name it for screen readers; otherwise it is aria-hidden. */
  label?: string;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const engine = useRef<DotEngine | null>(null);
  const { grid, dot, glyph, align, live, interactive, density, signal } = opts;

  useEffect(() => {
    const canvas = ref.current;
    const win = canvas?.ownerDocument.defaultView;
    if (!canvas || !win) return;
    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = canvas.getContext("2d");
    } catch {
      ctx = null;
    }
    if (!ctx) return; // jsdom, or a browser without canvas: the page is complete without it.
    const e = new DotEngine(canvas, ctx, {}, win);
    engine.current = e;
    e.readColours();
    e.resize();

    const cleanups: (() => void)[] = [];
    let alive = true;
    cleanups.push(() => {
      alive = false;
    });
    // A glyph drawn before Space Grotesk arrived used the fallback face: draw it again.
    void win.document.fonts.ready.then(() => {
      if (alive) e.refreshGlyph();
    });
    const onVisibility = () => e.setVisible(win.document.visibilityState !== "hidden");
    win.document.addEventListener("visibilitychange", onVisibility);
    cleanups.push(() => win.document.removeEventListener("visibilitychange", onVisibility));
    onVisibility();

    const motion = media(win, "(prefers-reduced-motion: reduce)");
    const onMotion = () => e.setReducedMotion(reducedMotion(win));
    motion?.addEventListener("change", onMotion);
    cleanups.push(() => motion?.removeEventListener("change", onMotion));

    const scheme = media(win, "(prefers-color-scheme: dark)");
    const onScheme = () => e.readColours();
    scheme?.addEventListener("change", onScheme);
    cleanups.push(() => scheme?.removeEventListener("change", onScheme));
    if (typeof MutationObserver !== "undefined") {
      const mo = new MutationObserver(onScheme);
      mo.observe(win.document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      });
      cleanups.push(() => mo.disconnect());
    }
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => e.resize());
      ro.observe(canvas);
      cleanups.push(() => ro.disconnect());
    }
    if (typeof IntersectionObserver !== "undefined") {
      const io = new IntersectionObserver((entries) => {
        for (const entry of entries) e.setOnScreen(entry.isIntersecting);
      });
      io.observe(canvas);
      cleanups.push(() => io.disconnect());
    }
    // The canvas takes no pointer events: the nearest [data-dot-host] (or its parent) passes them on.
    const host = canvas.closest<HTMLElement>("[data-dot-host]") ?? canvas.parentElement;
    if (host) {
      const move = (ev: PointerEvent) => {
        if (ev.pointerType === "mouse" || ev.pointerType === "pen") e.point(ev.clientX, ev.clientY);
      };
      const leave = () => e.point(null);
      host.addEventListener("pointermove", move, { passive: true });
      host.addEventListener("pointerleave", leave, { passive: true });
      cleanups.push(() => {
        host.removeEventListener("pointermove", move);
        host.removeEventListener("pointerleave", leave);
      });
    }
    return () => {
      for (const c of cleanups) c();
      e.stop();
      engine.current = null;
    };
  }, []);

  useEffect(() => {
    const o: Partial<DotOptions> = {};
    if (grid !== undefined) o.grid = grid;
    if (dot !== undefined) o.dot = dot;
    if (glyph !== undefined) o.glyph = glyph;
    if (align !== undefined) o.align = align;
    if (live !== undefined) o.live = live;
    if (interactive !== undefined) o.interactive = interactive;
    if (density !== undefined) o.density = density;
    if (signal !== undefined) o.signal = signal;
    engine.current?.setOptions(o);
  }, [grid, dot, glyph, align, live, interactive, density, signal]);

  const [px, py] = pulseAt;
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    engine.current?.pulse(px, py);
  }, [pulseKey, px, py]);

  return (
    <canvas
      ref={ref}
      className={className ? `dotfield ${className}` : "dotfield"}
      {...(label ? { role: "img", "aria-label": label } : { "aria-hidden": true })}
    />
  );
}
