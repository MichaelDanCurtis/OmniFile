import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";

/**
 * Retries only what the server says is retryable (groups_pending, throttled,
 * unavailable), after the server's retry_after_s. Never retries a 4xx otherwise.
 */
export function makeQueryClient(options: { retry?: boolean } = {}): QueryClient {
  const retry = options.retry ?? true;
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: false,
        // groups_pending lasts until the worker has read an over-200-groups
        // user's AD groups (portal-api.md 3.3): keep trying for about a minute.
        retry: (count, error) =>
          retry &&
          error instanceof ApiError &&
          error.retryable &&
          count < (error.code === "groups_pending" ? 12 : 3),
        retryDelay: (_count, error) => (error instanceof ApiError ? error.retryAfterMs : 1000),
      },
      mutations: { retry: false },
    },
  });
}
