import {
  Component,
  Suspense,
  lazy,
  type ComponentType,
  type ErrorInfo,
  type LazyExoticComponent,
  type ReactNode,
} from "react";
import { Callout, Loading } from "../components/Feedback";
import { usePageTitle } from "./usePageTitle";

/**
 * Code splitting (the bundle stays under Vite's 600 kB warning): admin-only
 * pages and the visual views load as their own chunks, from the same origin,
 * so the CSP (script-src 'self', style-src 'self') is unchanged. Each chunk's
 * CSS loads with it as a stylesheet link, never inline.
 *
 * A chunk can fail to load: a network blip, or a stale tab after a deploy (the
 * old hashed file is gone). React.lazy keeps the rejection, so only a reload
 * helps. The part that failed says so and offers one; the rest of the portal,
 * nav included, stays. Vite's `vite:preloadError` also reloads once by itself
 * (`installPreloadRecovery`).
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function lazyNamed<M extends Record<K, ComponentType<any>>, K extends keyof M>(
  load: () => Promise<M>,
  name: K,
): LazyExoticComponent<M[K]> {
  return lazy(() => load().then((m) => ({ default: m[name] })));
}

/** The page reload, as an object so tests can watch it. */
export const pageReload = {
  reload: (): void => {
    window.location.reload();
  },
};

/** "Couldn't load this part": the callout a failed chunk (or a broken route) leaves. */
export function LoadFailed({ what = "this part of the portal" }: { what?: string }) {
  return (
    <Callout tone="danger" title={`Couldn't load ${what}`} role="alert">
      <p>The connection may have dropped, or the portal was updated since this tab opened.</p>
      <button
        type="button"
        className="btn btn-sm"
        style={{ marginTop: 8 }}
        onClick={() => pageReload.reload()}
      >
        Reload
      </button>
    </Callout>
  );
}

class ChunkBoundary extends Component<{ children: ReactNode; what?: string }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.warn("[portal] a lazily loaded part failed", error, info.componentStack);
  }

  override render() {
    return this.state.failed ? <LoadFailed what={this.props.what} /> : this.props.children;
  }
}

function Title({ title }: { title: string | undefined }) {
  usePageTitle(title);
  return null;
}

/**
 * A route's page while its chunk loads: a polite status, as every other loading
 * state. `title` names the tab before the chunk arrives. `inner` is for a page
 * inside a layout that already draws `.page` (Settings).
 */
export function LazyPage({
  children,
  label = "Loading the page…",
  title,
  inner = false,
}: {
  children: ReactNode;
  label?: string;
  title?: string;
  inner?: boolean;
}) {
  const loading = <Loading label={label} />;
  return (
    <ChunkBoundary what="this page">
      <Suspense
        fallback={
          <>
            {title ? <Title title={title} /> : null}
            {inner ? loading : <div className="page">{loading}</div>}
          </>
        }
      >
        {children}
      </Suspense>
    </ChunkBoundary>
  );
}

/** A view inside a page while its chunk loads: one quiet line, announced politely. */
export function LazyPart({ children, label }: { children: ReactNode; label: string }) {
  return (
    <ChunkBoundary what="this part of the page">
      <Suspense
        fallback={
          <p className="muted" role="status" aria-live="polite">
            {label}
          </p>
        }
      >
        {children}
      </Suspense>
    </ChunkBoundary>
  );
}

const RELOADED_KEY = "omnifile.chunkReloadAt";
/** At most one automatic reload a minute, so a chunk that's really gone can't loop. */
export const RELOAD_GUARD_MS = 60_000;

/**
 * Vite fires `vite:preloadError` when a dynamic import fails. Reload the page
 * once, which fetches the current index.html and its chunks. Without session
 * storage (blocked, private window) there is no guard, so no automatic reload:
 * the part's own Reload button is still there.
 */
export function installPreloadRecovery(target: Window = window): () => void {
  const onError = (event: Event) => {
    try {
      const last = Number(target.sessionStorage.getItem(RELOADED_KEY) ?? 0);
      if (Date.now() - last < RELOAD_GUARD_MS) return;
      target.sessionStorage.setItem(RELOADED_KEY, String(Date.now()));
    } catch {
      return;
    }
    event.preventDefault();
    pageReload.reload();
  };
  target.addEventListener("vite:preloadError", onError);
  return () => target.removeEventListener("vite:preloadError", onError);
}
