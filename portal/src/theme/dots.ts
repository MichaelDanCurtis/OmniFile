/**
 * The dot layer's engine (WP17): tiny dots on a fixed grid, drawn on one
 * canvas. Framework-free so it can be tested and measured on its own; the
 * React wrapper is DotField.tsx.
 *
 * Motion budget: it draws at most ~30 frames a second, only while the canvas
 * is on screen and the tab is visible; with prefers-reduced-motion it draws
 * one still frame and never moves. Each frame is one pass over the dots and
 * at most 16 fills (8 opacity steps x 2 colours), whatever the dot count.
 */

export interface DotOptions {
  /** Distance between dot centres, in CSS px. */
  grid: number;
  /** Dot diameter, in CSS px. */
  dot: number;
  /** Text to draw in dots over the field (a hero glyph). Empty: a plain field. */
  glyph: string;
  /** Where the glyph sits: start, center or end of the canvas. */
  align: "start" | "center" | "end";
  /** Keep drifting. False: the glyph assembles, then the layer rests until touched. */
  live: boolean;
  /** Dots near the pointer brighten and lean away from it. */
  interactive: boolean;
  /** Base opacity of the field's dots, 0 to 1. */
  density: number;
  /** Draw the glyph in the signal colour instead of ink. */
  signal: boolean;
}

export const DEFAULTS: DotOptions = {
  grid: 7,
  dot: 1.6,
  glyph: "",
  align: "center",
  live: true,
  interactive: true,
  density: 0.32,
  signal: false,
};

const FRAME_MS = 33;
const STEPS = 8;
const RIPPLE_LIFE = 2400;
const RIPPLE_SPEED = 0.32; // px per ms
const POINTER_R = 84;
const ASSEMBLE_MS = 900;
const TAU = Math.PI * 2;

interface Ripple {
  x: number;
  y: number;
  t0: number;
}

/** A media query, or null where there is no matchMedia (jsdom). */
export function media(win: Window, query: string): MediaQueryList | null {
  try {
    return "matchMedia" in win ? win.matchMedia(query) : null;
  } catch {
    return null;
  }
}

/** Reads `prefers-reduced-motion` safely. */
export function reducedMotion(win: Window = window): boolean {
  return media(win, "(prefers-reduced-motion: reduce)")?.matches ?? false;
}

/** A deterministic 0..1 hash, so the field looks the same on every load. */
function hash(i: number): number {
  const x = Math.sin(i * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
}

/** The drift: three slow interfering waves, 0..1. Pure, so tests can pin it. */
export function drift(x: number, y: number, t: number): number {
  const w =
    Math.sin(x * 0.021 + t * 0.00042) +
    Math.sin(y * 0.034 - t * 0.00031) +
    Math.sin((x - y * 0.6) * 0.013 + t * 0.00019);
  return (w + 3) / 6;
}

/** Which grid points fall inside `glyph` drawn at this size: a 0/1 mask. */
export function glyphMask(
  glyph: string,
  cols: number,
  rows: number,
  grid: number,
  align: DotOptions["align"],
  doc: Document = document,
): Uint8Array {
  const mask = new Uint8Array(cols * rows);
  if (!glyph) return mask;
  const w = cols * grid;
  const h = rows * grid;
  const off = doc.createElement("canvas");
  off.width = w;
  off.height = h;
  const g = off.getContext("2d", { willReadFrequently: true });
  if (!g) return mask;
  let size = Math.floor(h * 0.78);
  const face = (px: number) => `500 ${px}px "Space Grotesk", system-ui, sans-serif`;
  g.font = face(size);
  const fit = w * 0.92;
  const measured = g.measureText(glyph).width;
  if (measured > fit) {
    size = Math.floor((size * fit) / measured);
    g.font = face(size);
  }
  const tw = g.measureText(glyph).width;
  const x = align === "start" ? grid : align === "end" ? w - tw - grid : (w - tw) / 2;
  g.textBaseline = "middle";
  g.fillStyle = "#000";
  g.fillText(glyph, x, h / 2 + size * 0.04);
  // Thicken the strokes to about one grid step, so thin parts still land on a dot.
  g.strokeStyle = "#000";
  g.lineWidth = grid * 0.7;
  g.lineJoin = "round";
  g.strokeText(glyph, x, h / 2 + size * 0.04);
  const data = g.getImageData(0, 0, w, h).data;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const px = Math.min(w - 1, Math.round(c * grid + grid / 2));
      const py = Math.min(h - 1, Math.round(r * grid + grid / 2));
      if ((data[(py * w + px) * 4 + 3] ?? 0) > 110) mask[r * cols + c] = 1;
    }
  }
  return mask;
}

export class DotEngine {
  private ctx: CanvasRenderingContext2D;
  private opts: DotOptions;
  private dpr = 1;
  private w = 0;
  private h = 0;
  private cols = 0;
  private rows = 0;
  private xs = new Float32Array(0);
  private ys = new Float32Array(0);
  private seeds = new Float32Array(0);
  private mask: Uint8Array = new Uint8Array(0);
  private level = new Uint8Array(0);
  private dx = new Float32Array(0);
  private dy = new Float32Array(0);
  private ripples: Ripple[] = [];
  private pointer: { x: number; y: number } | null = null;
  private raf = 0;
  private last = 0;
  private born = 0;
  private visible = true;
  private onScreen = true;
  private still: boolean;
  private colours = { field: "#8b8984", ink: "#141414", signal: "#d71921" };
  /** Frames drawn so far: the perf check reads it. */
  frames = 0;

  constructor(
    private canvas: HTMLCanvasElement,
    ctx: CanvasRenderingContext2D,
    opts: Partial<DotOptions> = {},
    private win: Window = window,
  ) {
    this.ctx = ctx;
    this.opts = { ...DEFAULTS, ...opts };
    this.still = reducedMotion(win);
    this.born = this.now();
  }

  private now(): number {
    return this.win.performance.now();
  }

  setOptions(opts: Partial<DotOptions>): void {
    const before = this.opts;
    this.opts = { ...before, ...opts };
    if (before.glyph !== this.opts.glyph || before.grid !== this.opts.grid) {
      this.born = this.now();
      this.layout();
    }
    this.wake();
  }

  setReducedMotion(still: boolean): void {
    this.still = still;
    if (still) this.pointer = null;
    this.wake();
  }

  setVisible(visible: boolean): void {
    this.visible = visible;
    this.wake();
  }

  setOnScreen(onScreen: boolean): void {
    this.onScreen = onScreen;
    this.wake();
  }

  /** Re-read the colours from CSS (after a theme change). */
  readColours(): void {
    const css = this.win.getComputedStyle(this.canvas);
    const get = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
    this.colours = {
      field: get("--dot", this.colours.field),
      ink: get("--ink", this.colours.ink),
      signal: get("--signal", this.colours.signal),
    };
    this.wake();
  }

  /** Size the canvas to its box and lay the grid out again. */
  resize(): void {
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(0, Math.round(rect.width));
    const h = Math.max(0, Math.round(rect.height));
    const dpr = Math.min(2, this.win.devicePixelRatio || 1);
    if (w === this.w && h === this.h && dpr === this.dpr) return;
    this.w = w;
    this.h = h;
    this.dpr = dpr;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.layout();
    this.wake();
  }

  private layout(): void {
    const { grid } = this.opts;
    this.cols = Math.max(0, Math.floor(this.w / grid));
    this.rows = Math.max(0, Math.floor(this.h / grid));
    const n = this.cols * this.rows;
    const ox = (this.w - (this.cols - 1) * grid) / 2;
    const oy = (this.h - (this.rows - 1) * grid) / 2;
    this.xs = new Float32Array(n);
    this.ys = new Float32Array(n);
    this.seeds = new Float32Array(n);
    this.level = new Uint8Array(n);
    this.dx = new Float32Array(n);
    this.dy = new Float32Array(n);
    for (let r = 0; r < this.rows; r++) {
      for (let c = 0; c < this.cols; c++) {
        const i = r * this.cols + c;
        this.xs[i] = ox + c * grid;
        this.ys[i] = oy + r * grid;
        this.seeds[i] = hash(i);
      }
    }
    this.mask = this.opts.glyph
      ? glyphMask(
          this.opts.glyph,
          this.cols,
          this.rows,
          grid,
          this.opts.align,
          this.canvas.ownerDocument,
        )
      : new Uint8Array(n);
  }

  /** Draw the glyph again from the real face, once the fonts have loaded. */
  refreshGlyph(): void {
    if (!this.opts.glyph) return;
    this.layout();
    this.wake();
  }

  /** A ripple from (fx, fy), given as fractions of the canvas. Ignored when still. */
  pulse(fx = 0.15, fy = 0.5): void {
    if (this.still) return;
    this.ripples.push({ x: fx * this.w, y: fy * this.h, t0: this.now() });
    if (this.ripples.length > 4) this.ripples.shift();
    this.wake();
  }

  /** The pointer, in client coordinates; null when it leaves. */
  point(clientX: number | null, clientY?: number): void {
    if (this.still || !this.opts.interactive) return;
    const had = this.pointer !== null;
    if (clientX === null || clientY === undefined) {
      this.pointer = null;
    } else {
      const rect = this.canvas.getBoundingClientRect();
      const x = clientX - rect.left;
      const y = clientY - rect.top;
      const inside =
        x > -POINTER_R &&
        y > -POINTER_R &&
        x < rect.width + POINTER_R &&
        y < rect.height + POINTER_R;
      this.pointer = inside ? { x, y } : null;
    }
    // Far away before and after: nothing to redraw.
    if (had || this.pointer) this.wake();
  }

  /** True while there is something left to animate. */
  private busy(t: number): boolean {
    if (this.still) return false;
    if (this.opts.live) return true;
    if (this.pointer) return true;
    if (this.ripples.length) return true;
    return this.opts.glyph !== "" && t - this.born < ASSEMBLE_MS + 400;
  }

  private wake(): void {
    if (this.raf || !this.visible || !this.onScreen || this.w === 0 || this.h === 0) {
      if (this.raf && (!this.visible || !this.onScreen)) this.stop();
      return;
    }
    this.raf = this.win.requestAnimationFrame(this.tick);
  }

  private tick = (t: number): void => {
    this.raf = 0;
    if (!this.visible || !this.onScreen) return;
    if (t - this.last >= FRAME_MS || this.still) {
      this.last = t;
      this.draw(this.still ? 0 : this.now());
    }
    if (this.busy(this.now())) this.raf = this.win.requestAnimationFrame(this.tick);
  };

  stop(): void {
    if (this.raf) this.win.cancelAnimationFrame(this.raf);
    this.raf = 0;
  }

  /** One frame at time `t` (ms). Public for the tests and the perf check. */
  draw(t: number): void {
    const { ctx, xs, ys, seeds, mask, level, dx, dy } = this;
    const n = xs.length;
    const still = this.still;
    const base = this.opts.density;
    const age = t - this.born;
    this.ripples = this.ripples.filter((r) => t - r.t0 < RIPPLE_LIFE);
    const ripples = this.ripples;
    const p = this.pointer;
    for (let i = 0; i < n; i++) {
      const x = xs[i] ?? 0;
      const y = ys[i] ?? 0;
      const s = seeds[i] ?? 0;
      let a: number;
      let ox = 0;
      let oy = 0;
      if (mask[i]) {
        const appear = still ? 1 : Math.min(1, Math.max(0, (age - s * ASSEMBLE_MS) / 260));
        a = appear * (0.78 + 0.22 * drift(x, y, t * 1.6));
      } else {
        a = base * (0.25 + 0.75 * drift(x, y, t + s * 900));
      }
      for (const r of ripples) {
        const life = (t - r.t0) / RIPPLE_LIFE;
        const d = Math.hypot(x - r.x, y - r.y) - (t - r.t0) * RIPPLE_SPEED;
        a += Math.exp(-(d * d) / 260) * 0.5 * (1 - life);
      }
      if (p) {
        const ddx = x - p.x;
        const ddy = y - p.y;
        const dd = Math.hypot(ddx, ddy);
        if (dd < POINTER_R && dd > 0.01) {
          const k = (1 - dd / POINTER_R) ** 2;
          a += k * 0.55;
          ox = (ddx / dd) * k * 2.6;
          oy = (ddy / dd) * k * 2.6;
        }
      }
      level[i] = Math.max(0, Math.min(STEPS, Math.round(a * STEPS)));
      dx[i] = ox;
      dy[i] = oy;
    }
    const dpr = this.dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    const rad = this.opts.dot / 2;
    const glyphColour = this.opts.signal ? this.colours.signal : this.colours.ink;
    for (const inGlyph of [0, 1]) {
      ctx.fillStyle = inGlyph ? glyphColour : this.colours.field;
      for (let step = 1; step <= STEPS; step++) {
        let any = false;
        ctx.beginPath();
        for (let i = 0; i < n; i++) {
          if (level[i] !== step || (mask[i] ?? 0) !== inGlyph) continue;
          const x = (xs[i] ?? 0) + (dx[i] ?? 0);
          const y = (ys[i] ?? 0) + (dy[i] ?? 0);
          ctx.moveTo(x + rad, y);
          ctx.arc(x, y, rad, 0, TAU);
          any = true;
        }
        if (any) {
          ctx.globalAlpha = step / STEPS;
          ctx.fill();
        }
      }
    }
    ctx.globalAlpha = 1;
    this.frames++;
  }

  /** How many dots the layer has, and how many draw the glyph (tests). */
  stats(): { dots: number; glyph: number; running: boolean } {
    let g = 0;
    for (const m of this.mask) g += m;
    return { dots: this.xs.length, glyph: g, running: this.raf !== 0 };
  }
}
