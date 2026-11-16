import { useEffect, useRef, useState } from "react";
import { api, unwrap } from "../../../api/client";
import type { components } from "../../../api/schema.gen";

export type LiveEvent = components["schemas"]["LiveEvent"];
export type LiveActivity = components["schemas"]["LiveActivity"];
export type LiveEdge = components["schemas"]["LiveEdge"];

/** How long the server may hold a poll (portal-api.md 5.13), and the back-off after an error. */
export const LIVE_WAIT_S = 20;
const RETRY_MS = 5000;
/** The feed keeps this many events; older ones scroll off. */
export const KEEP = 200;
/** Points in the client-side sparklines (recalls in flight, edges online): one per poll. */
const HISTORY = 30;

export type FeedStatus = "connecting" | "live" | "paused" | "retrying" | "capped";

export interface Feed {
  events: LiveEvent[];
  /** Ids that arrived with the latest poll: they animate in. */
  fresh: ReadonlySet<string>;
  latest: LiveActivity | null;
  inflight: number[];
  online: number[];
  status: FeedStatus;
  error: unknown;
  /** How many events arrived with the latest poll, for the live region. */
  arrived: number;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    });
  });
}

/**
 * The live feed: a long-poll loop on `GET /v1/activity/live`. Each answer's
 * `cursor` is the next `since`; ids already shown are dropped (the server
 * re-sends 30 seconds of overlap). Paused, no request is open; on resume the
 * loop starts again from the same cursor, so what happened meanwhile (up to 15
 * minutes) comes in at once.
 */
export function useLiveFeed(paused: boolean): Feed {
  const cursor = useRef<string | null>(null);
  const seen = useRef<Set<string>>(new Set());
  const [feed, setFeed] = useState<Feed>({
    events: [],
    fresh: new Set(),
    latest: null,
    inflight: [],
    online: [],
    status: "connecting",
    error: null,
    arrived: 0,
  });

  useEffect(() => {
    if (paused) {
      setFeed((f) => ({ ...f, status: "paused", fresh: new Set() }));
      return;
    }
    const ctrl = new AbortController();
    // A call, not a property read: TypeScript would narrow `aborted` to false inside the loop.
    const stopped = () => ctrl.signal.aborted;
    void (async () => {
      while (!stopped()) {
        try {
          const since = cursor.current;
          const body = await unwrap(
            api.GET("/v1/activity/live", {
              params: { query: since ? { since, wait: LIVE_WAIT_S } : {} },
              signal: ctrl.signal,
            }),
          );
          if (stopped()) return;
          cursor.current = body.cursor;
          const incoming = body.events.filter((e) => !seen.current.has(e.id));
          for (const e of incoming) seen.current.add(e.id);
          setFeed((f) => {
            const events = [...incoming, ...f.events]
              .sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0))
              .slice(0, KEEP);
            if (seen.current.size > KEEP * 4) seen.current = new Set(events.map((e) => e.id));
            return {
              events,
              // The first answer is history, not news: nothing animates.
              fresh: f.latest ? new Set(incoming.map((e) => e.id)) : new Set(),
              latest: body,
              inflight: [...f.inflight, body.counters.recalls_in_flight].slice(-HISTORY),
              online: [...f.online, body.counters.edges_online].slice(-HISTORY),
              status: body.live.capped ? "capped" : "live",
              error: null,
              arrived: f.latest ? incoming.length : 0,
            };
          });
          if (body.live.next_poll_s > 0) await sleep(body.live.next_poll_s * 1000, ctrl.signal);
        } catch (error) {
          if (stopped()) return;
          setFeed((f) => ({ ...f, status: "retrying", error }));
          await sleep(RETRY_MS, ctrl.signal);
        }
      }
    })();
    return () => ctrl.abort();
  }, [paused]);

  return feed;
}
