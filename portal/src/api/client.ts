import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema.gen";
import type { Problem, ProblemCode } from "./types";
import { uuidv7 } from "./uuidv7";

/**
 * The one client of the portal JSON API (service/openapi/portal-api.yaml).
 *
 * Every unsafe call carries the session's CSRF token, and every POST under
 * /v1 except the three previews carries an Idempotency-Key (UUIDv7)
 * (portal-api.md 2.1 and 4.2). The browser adds Origin itself on same-origin
 * POST, PUT, PATCH and DELETE, and sends the session cookie (SameSite=Strict).
 */

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const PREVIEW_ROUTES = [
  /^\/v1\/policies\/preview$/,
  /^\/v1\/pins\/preview$/,
  /^\/v1\/shares\/[^/]+\/settings\/preview$/,
];

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function needsIdempotencyKey(method: string, pathname: string): boolean {
  return (
    method === "POST" &&
    pathname.startsWith("/v1/") &&
    !PREVIEW_ROUTES.some((route) => route.test(pathname))
  );
}

export const securityHeaders: Middleware = {
  onRequest({ request }) {
    if (!UNSAFE.has(request.method)) return request;
    if (csrfToken) request.headers.set("X-CSRF-Token", csrfToken);
    const { pathname } = new URL(request.url);
    if (needsIdempotencyKey(request.method, pathname) && !request.headers.has("Idempotency-Key")) {
      request.headers.set("Idempotency-Key", uuidv7());
    }
    return request;
  },
};

/** Browser navigation, swappable in tests (jsdom can't navigate). */
export const browserNav = {
  assign: (url: string): void => {
    window.location.assign(url);
  },
};

export function currentPath(): string {
  return window.location.pathname + window.location.search;
}

export function signInUrl(returnTo: string = currentPath(), reauth = false): string {
  const params = new URLSearchParams({ return_to: returnTo.startsWith("/") ? returnTo : "/" });
  if (reauth) params.set("reauth", "1");
  return `/auth/login?${params.toString()}`;
}

/** Sends the browser to Entra sign-in (silent if Entra still has a session). */
export function signIn(options: { reauth?: boolean; returnTo?: string } = {}): void {
  browserNav.assign(signInUrl(options.returnTo, options.reauth ?? false));
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: ProblemCode | "network";
  readonly problem: Problem | null;

  constructor(status: number, problem: Problem | null, message?: string) {
    super(message ?? problem?.detail ?? problem?.title ?? `Request failed (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
    this.code = problem?.code ?? (status === 0 ? "network" : "internal");
  }

  get retryable(): boolean {
    return this.problem?.retryable ?? (this.status === 0 || this.status >= 500);
  }

  get retryAfterMs(): number {
    return (this.problem?.retry_after_s ?? 1) * 1000;
  }
}

function isProblem(value: unknown): value is Problem {
  return typeof value === "object" && value !== null && "code" in value && "status" in value;
}

/** Called on 401 unauthenticated from any route; the session provider sets it. */
let onUnauthenticated: () => void = () => undefined;
export function setUnauthenticatedHandler(handler: () => void): void {
  onUnauthenticated = handler;
}

/**
 * Unwraps an openapi-fetch result: returns data, or throws ApiError.
 * A 401 `unauthenticated` also starts sign-in again (portal-api.md 2.1 step 3).
 */
export async function unwrap<T>(
  request: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response };
  try {
    result = await request;
  } catch (cause) {
    throw new ApiError(0, null, cause instanceof Error ? cause.message : "Network error");
  }
  const { data, error, response } = result;
  if (response.ok) return data as T;
  const problem = isProblem(error) ? error : null;
  const apiError = new ApiError(response.status, problem);
  if (apiError.code === "unauthenticated") onUnauthenticated();
  return Promise.reject(apiError);
}

export function makeClient(baseUrl: string = window.location.origin) {
  const client = createClient<paths>({
    baseUrl,
    credentials: "same-origin",
    // Resolve fetch per call, so test interceptors installed later still apply.
    fetch: (request: Request) => globalThis.fetch(request),
  });
  client.use(securityHeaders);
  return client;
}

export const api = makeClient();

/** A fresh Idempotency-Key header. Make one per user action, not per retry. */
export function idem(): { "Idempotency-Key": string } {
  return { "Idempotency-Key": uuidv7() };
}
