import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { requestLog } from "../mocks/handlers";
import { server } from "../mocks/server";
import {
  ApiError,
  api,
  browserNav,
  idem,
  needsIdempotencyKey,
  setCsrfToken,
  setUnauthenticatedHandler,
  signInUrl,
  unwrap,
} from "./client";
import { UUIDV7_PATTERN, uuidv7 } from "./uuidv7";

describe("uuidv7", () => {
  it("is a version 7, variant 10 UUID", () => {
    for (let i = 0; i < 50; i++) expect(uuidv7()).toMatch(UUIDV7_PATTERN);
  });

  it("puts the millisecond timestamp in the first 48 bits", () => {
    // Keys never go back in time, so use an instant after any made so far.
    const at = Date.now() + 86_400_000;
    const id = uuidv7(at);
    expect(parseInt(id.replace(/-/g, "").slice(0, 12), 16)).toBe(at);
  });

  it("sorts in creation order, even within one millisecond", () => {
    const at = Date.now() + 2 * 86_400_000;
    const ids = Array.from({ length: 20 }, () => uuidv7(at));
    expect([...ids].sort()).toEqual(ids);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("needsIdempotencyKey", () => {
  it("is every POST under /v1 except the three previews", () => {
    expect(needsIdempotencyKey("POST", "/v1/recalls")).toBe(true);
    expect(needsIdempotencyKey("POST", "/v1/pins")).toBe(true);
    expect(needsIdempotencyKey("POST", "/v1/shares/1/worm/enable")).toBe(true);
    expect(needsIdempotencyKey("POST", "/v1/policies/preview")).toBe(false);
    expect(needsIdempotencyKey("POST", "/v1/pins/preview")).toBe(false);
    expect(needsIdempotencyKey("POST", "/v1/shares/7/settings/preview")).toBe(false);
    expect(needsIdempotencyKey("POST", "/auth/logout")).toBe(false);
    expect(needsIdempotencyKey("PATCH", "/v1/shares/1/settings")).toBe(false);
  });
});

describe("api client", () => {
  it("sends X-CSRF-Token on unsafe calls only, and an Idempotency-Key on POSTs", async () => {
    setCsrfToken("mock-csrf-admin");
    await unwrap(api.GET("/v1/shares"));
    await unwrap(
      api.POST("/v1/recalls", {
        params: { header: idem() },
        body: { file_id: "1002", priority: "standard" },
      }),
    );
    await unwrap(
      api.POST("/v1/policies/preview", { body: { dataset_id: "1", body: { archive_day: 365 } } }),
    );

    const get = requestLog.find((r) => r.method === "GET" && r.path.startsWith("/v1/shares"));
    const recall = requestLog.find((r) => r.path === "/v1/recalls");
    const preview = requestLog.find((r) => r.path === "/v1/policies/preview");
    expect(get?.headers["x-csrf-token"]).toBeUndefined();
    expect(recall?.headers["x-csrf-token"]).toBe("mock-csrf-admin");
    expect(recall?.headers["idempotency-key"]).toMatch(UUIDV7_PATTERN);
    expect(preview?.headers["x-csrf-token"]).toBe("mock-csrf-admin");
    expect(preview?.headers["idempotency-key"]).toBeUndefined();
  });

  it("adds an Idempotency-Key itself when a caller forgot one", async () => {
    setCsrfToken("mock-csrf-admin");
    server.use(
      http.post("*/v1/pins/:id/release", ({ request }) =>
        HttpResponse.json({ key: request.headers.get("Idempotency-Key") }),
      ),
    );
    const res = await fetch("http://portal.test/v1/pins/1/release", { method: "POST" });
    // Plain fetch bypasses the client: no key.
    expect(((await res.json()) as { key: string | null }).key).toBeNull();
    const viaClient = await api.POST("/v1/pins/{pin_id}/release", {
      // @ts-expect-error: the Idempotency-Key header is left out on purpose
      params: { path: { pin_id: "1" } },
    });
    expect((viaClient.data as unknown as { key: string }).key).toMatch(UUIDV7_PATTERN);
  });

  it("turns problem+json into an ApiError with the server's code", async () => {
    setCsrfToken("wrong");
    const error = await unwrap(
      api.POST("/v1/pins", {
        params: { header: idem() },
        body: { items: ["1"], tier: "hot", reason: "x" },
      }),
    ).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("csrf_failed");
    expect((error as ApiError).status).toBe(403);
  });

  it("starts sign-in again on 401 unauthenticated", async () => {
    const handler = vi.fn();
    setUnauthenticatedHandler(handler);
    server.use(
      http.get("*/v1/me", () =>
        HttpResponse.json(
          {
            type: "urn:omnifile:error:unauthenticated",
            title: "x",
            status: 401,
            code: "unauthenticated",
            retryable: false,
          },
          { status: 401, headers: { "Content-Type": "application/problem+json" } },
        ),
      ),
    );
    await expect(unwrap(api.GET("/v1/me"))).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledOnce();
  });

  it("builds same-origin sign-in URLs, with reauth=1 for re-authentication", () => {
    expect(signInUrl("/shares/1?worm=enable", true)).toBe(
      "/auth/login?return_to=%2Fshares%2F1%3Fworm%3Denable&reauth=1",
    );
    expect(signInUrl("https://evil.example/")).toBe("/auth/login?return_to=%2F");
    expect(vi.isMockFunction(browserNav.assign)).toBe(true);
  });
});
