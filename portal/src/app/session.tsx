import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, use, useEffect, useState, type ReactNode } from "react";
import {
  ApiError,
  api,
  browserNav,
  setCsrfToken,
  setUnauthenticatedHandler,
  signIn,
  unwrap,
} from "../api/client";
import type { Me, Session } from "../api/types";
import { DotField } from "../theme/DotField";
import { Brandmark } from "./Brandmark";

interface SessionValue {
  session: Session;
  me: Me;
  signOut: () => Promise<void>;
}

const SessionContext = createContext<SessionValue | null>(null);

/** Re-authentication window for WORM and legal holds (portal-api.md 2.4). */
export const REAUTH_MAX_AGE_S = 600;

export function authIsRecent(session: Session, now: number = Date.now()): boolean {
  return now - new Date(session.auth_time).getTime() < REAUTH_MAX_AGE_S * 1000;
}

async function signOut(): Promise<void> {
  const result = await unwrap(api.POST("/auth/logout", {}));
  setCsrfToken(null);
  browserNav.assign(result.logout_url);
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: ["session"],
    queryFn: () => unwrap(api.GET("/auth/session")),
    retry: false,
    staleTime: 5 * 60_000,
    // While the user's AD groups are being resolved, look again every 5 s.
    refetchInterval: (q) => (q.state.data?.user.groups_source === "pending" ? 5000 : false),
  });
  const client = useQueryClient();
  const groupsSource = query.data?.user.groups_source;
  const [lastSource, setLastSource] = useState(groupsSource);
  if (groupsSource !== lastSource) {
    setLastSource(groupsSource);
    // Groups just resolved: what the user may read has changed, so re-read everything.
    if (lastSource === "pending") {
      void client.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "session" });
    }
  }

  useEffect(() => {
    setUnauthenticatedHandler(() => signIn());
  }, []);

  // Set before children render, so a query fired from a child's effect
  // (the debounced previews are POSTs) already carries the CSRF token.
  if (query.data) setCsrfToken(query.data.csrf_token);

  if (query.isPending) {
    return <FullPage title="Signing you in…" busy />;
  }
  if (query.isError) {
    const error = query.error;
    if (error instanceof ApiError && error.code === "unauthenticated") {
      return <FullPage title="Taking you to sign-in…" busy />;
    }
    return (
      <FullPage title="The portal can't reach the OmniFile service.">
        <p>{error.message}</p>
        <button type="button" className="btn btn-primary" onClick={() => void query.refetch()}>
          Try again
        </button>
      </FullPage>
    );
  }

  const value: SessionValue = { session: query.data, me: query.data.user, signOut };
  return <SessionContext value={value}>{children}</SessionContext>;
}

export function useSession(): SessionValue {
  const value = use(SessionContext);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}

function FullPage({
  title,
  busy,
  children,
}: {
  title: string;
  busy?: boolean;
  children?: ReactNode;
}) {
  return (
    <main className="fullpage" aria-busy={busy} data-dot-host="">
      <DotField className="fullpage-dots" glyph="OMNIFILE" density={0.26} />
      <div className="fullpage-card">
        <Brandmark large />
        <p className="fullpage-label">OmniFile Gateway</p>
        <h1>{title}</h1>
        {children}
      </div>
    </main>
  );
}
