import type { ReactNode } from "react";
import { ApiError, signIn } from "../api/client";
import { DotField } from "../theme/DotField";
import { AlertIcon, CheckIcon, ErrorIcon, InfoIcon } from "./icons";

/** Plain words for codes whose server detail is technical; otherwise the server's own detail. */
const PLAIN: Partial<Record<ApiError["code"], string>> = {
  precondition_failed:
    "Someone changed this since you opened it. Reload to see their changes, then try again.",
  precondition_required: "The page is out of date. Reload and try again.",
  csrf_failed: "Your session's security check failed. Reload the page and try again.",
  groups_pending: "We're still reading your AD groups. Try again in a few seconds.",
  throttled: "Too many requests at once. Wait a moment and try again.",
  network: "The portal couldn't reach the OmniFile service. Check your connection.",
  unavailable: "The OmniFile service is busy or restarting. Try again shortly.",
  internal:
    "Something went wrong on the server. Try again; if it keeps happening, tell your admin.",
};

export function problemText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "forbidden") {
      return error.problem?.detail ?? "Your share role doesn't allow this change.";
    }
    return PLAIN[error.code] ?? error.problem?.detail ?? error.message;
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}

export function Callout({
  tone = "info",
  title,
  children,
  role,
}: {
  tone?: "info" | "warn" | "danger" | "ok";
  title?: ReactNode;
  children?: ReactNode;
  role?: "alert" | "status";
}) {
  const Icon =
    tone === "danger"
      ? ErrorIcon
      : tone === "warn"
        ? AlertIcon
        : tone === "ok"
          ? CheckIcon
          : InfoIcon;
  return (
    <div className={`callout callout-${tone}`} role={role}>
      <Icon />
      <div className="stack" style={{ gap: 4 }}>
        {title ? <strong>{title}</strong> : null}
        {children ? <div>{children}</div> : null}
      </div>
    </div>
  );
}

/** An API error in plain words, with the one action that fixes it when there is one. */
export function ErrorCallout({ error, title }: { error: unknown; title?: string }) {
  if (error instanceof ApiError && error.code === "reauth_required") {
    return (
      <Callout tone="warn" title="Sign in again to continue" role="alert">
        <p>For this change, you need to have signed in within the last 10 minutes.</p>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => signIn({ reauth: true })}
          style={{ marginTop: 8 }}
        >
          Sign in again
        </button>
      </Callout>
    );
  }
  const checks = error instanceof ApiError ? (error.problem?.checks ?? []) : [];
  const errors = error instanceof ApiError ? (error.problem?.errors ?? []) : [];
  return (
    <Callout tone="danger" title={title ?? "That didn't work"} role="alert">
      <p>{problemText(error)}</p>
      {[...checks.map((c) => c.message), ...errors.map((e) => `${e.field}: ${e.message}`)].map(
        (line) => (
          <p key={line}>{line}</p>
        ),
      )}
    </Callout>
  );
}

/** Five dots that step in turn: a hardware-style "busy", still under reduced motion. */
export function DotLoader() {
  return (
    <span className="dot-loader" aria-hidden="true">
      <i />
      <i />
      <i />
      <i />
      <i />
    </span>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="empty loading" role="status" aria-live="polite">
      <DotLoader />
      <span className="loading-label">{label}</span>
    </div>
  );
}

/**
 * Nothing to show. A small dot-matrix glyph (a zero by default) assembles
 * above the title, then rests; it is decoration, so the title says it all.
 */
export function Empty({
  title,
  children,
  glyph = "0",
}: {
  title: string;
  children?: ReactNode;
  glyph?: string;
}) {
  return (
    <div className="empty">
      {glyph ? (
        <DotField
          className="empty-glyph"
          glyph={glyph}
          live={false}
          density={0.22}
          grid={5}
          dot={2}
        />
      ) : null}
      <h2>{title}</h2>
      {children}
    </div>
  );
}
