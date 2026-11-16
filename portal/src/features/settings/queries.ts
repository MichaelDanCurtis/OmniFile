import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, idem, unwrap } from "../../api/client";
import type { components } from "../../api/schema.gen";

/**
 * Settings (portal-api.md 5.20) and API clients (5.21): ArchiveAdmin only.
 * Settings connect and check; nothing here sends or stores a key or secret.
 */

type S = components["schemas"];
export type SettingCheck = S["SettingCheck"];
export type StorageBackend = S["StorageBackend"];
export type BackendDraft = S["BackendDraft"];
export type SettingsTest = S["SettingsTest"];
export type ApiClient = S["ApiClient"];
export type ApiClientOwner = S["ApiClientOwner"];
export type ApiClientCreate = S["ApiClientCreate"];
export type ApiClientPatch = S["ApiClientPatch"];

export const sk = {
  storage: ["settings", "storage"] as const,
  checks: (id: string) => ["settings", "storage", id, "checks"] as const,
  aws: ["settings", "aws"] as const,
  trust: (arn: string) => ["settings", "aws", "trust", arn] as const,
  identity: ["settings", "identity"] as const,
  whoami: ["settings", "whoami"] as const,
  teams: ["settings", "teams"] as const,
  email: ["settings", "email"] as const,
  mover: ["settings", "storage-mover"] as const,
  alerts: ["settings", "alerts"] as const,
  prices: (kind: string) => ["settings", "prices", kind] as const,
  test: (id: string) => ["settings", "tests", id] as const,
  clients: ["settings", "api-clients"] as const,
  client: (id: string) => ["settings", "api-clients", id] as const,
  people: (q: string) => ["settings", "people", q] as const,
};

export function useStorageSettings() {
  return useQuery({
    queryKey: sk.storage,
    queryFn: () => unwrap(api.GET("/v1/settings/storage")),
  });
}

/** Live checks: they call out (DNS, ARM, STS), so they run only when asked. */
export function useBackendChecks(backendId: string, enabled: boolean) {
  return useQuery({
    queryKey: sk.checks(backendId),
    enabled,
    staleTime: 0,
    queryFn: () =>
      unwrap(
        api.GET("/v1/settings/storage/{backend_id}/checks", {
          params: { path: { backend_id: backendId } },
        }),
      ),
  });
}

export function useSaveBackend() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ backendId, draft }: { backendId?: string; draft: BackendDraft }) =>
      backendId
        ? unwrap(
            api.PUT("/v1/settings/storage/{backend_id}", {
              params: { path: { backend_id: backendId } },
              body: draft,
            }),
          )
        : unwrap(api.POST("/v1/settings/storage", { params: { header: idem() }, body: draft })),
    onSuccess: () => void client.invalidateQueries({ queryKey: sk.storage }),
  });
}

export function useAwsSettings() {
  return useQuery({ queryKey: sk.aws, queryFn: () => unwrap(api.GET("/v1/settings/aws")) });
}

export function useTrustPolicy(roleArn: string | null) {
  return useQuery({
    queryKey: sk.trust(roleArn ?? ""),
    enabled: !!roleArn,
    queryFn: () =>
      unwrap(
        api.GET("/v1/settings/aws/trust-policy", {
          params: { query: { role_arn: roleArn ?? "" } },
        }),
      ),
  });
}

export function useAwsTest() {
  return useMutation({
    mutationFn: (roleArn: string) =>
      unwrap(
        api.POST("/v1/settings/aws/test", {
          params: { header: idem() },
          body: { role_arn: roleArn },
        }),
      ),
  });
}

export function useTestCard() {
  return useMutation({
    mutationFn: () =>
      unwrap(api.POST("/v1/settings/teams/test-card", { params: { header: idem() }, body: {} })),
  });
}

export function useTestEmail() {
  return useMutation({
    mutationFn: () =>
      unwrap(api.POST("/v1/settings/email/test", { params: { header: idem() }, body: {} })),
  });
}

const POLL_FOR_MS = 30_000;

/** A test the worker runs: polled every second while pending, for up to 30 s. */
export function useSettingsTest(testId: string | null) {
  return useQuery({
    queryKey: sk.test(testId ?? ""),
    enabled: !!testId,
    refetchInterval: (q) => {
      const t = q.state.data;
      if (!t || t.state !== "pending") return false;
      return Date.now() - Date.parse(t.created_at) < POLL_FOR_MS ? 1000 : false;
    },
    queryFn: () =>
      unwrap(
        api.GET("/v1/settings/tests/{test_id}", { params: { path: { test_id: testId ?? "" } } }),
      ),
  });
}

export function useIdentitySettings() {
  return useQuery({
    queryKey: sk.identity,
    queryFn: () => unwrap(api.GET("/v1/settings/identity")),
  });
}

export function useWhoAmI() {
  return useQuery({
    queryKey: sk.whoami,
    queryFn: () => unwrap(api.GET("/v1/settings/identity/whoami")),
  });
}

export function useTeamsSettings() {
  return useQuery({ queryKey: sk.teams, queryFn: () => unwrap(api.GET("/v1/settings/teams")) });
}

export function useEmailSettings() {
  return useQuery({ queryKey: sk.email, queryFn: () => unwrap(api.GET("/v1/settings/email")) });
}

export function useStorageMover() {
  return useQuery({
    queryKey: sk.mover,
    queryFn: () => unwrap(api.GET("/v1/settings/storage-mover")),
  });
}

export function useAlerts() {
  return useQuery({ queryKey: sk.alerts, queryFn: () => unwrap(api.GET("/v1/settings/alerts")) });
}

export function usePriceTable(kind: "azure_blob" | "s3") {
  return useQuery({
    queryKey: sk.prices(kind),
    queryFn: () =>
      unwrap(
        api.GET("/v1/price-tables/{backend_kind}", { params: { path: { backend_kind: kind } } }),
      ),
  });
}

export type PriceTable = S["PriceTable"];
export type PriceDisplay = S["PriceDisplay"];
export type PriceRole = PriceDisplay["show_to"][number];

/** Who sees prices (portal-api.md 5.24), with its ETag for the change. */
export function usePriceDisplay() {
  return useQuery({
    queryKey: ["settings", "price-display"],
    queryFn: async () => {
      const r = await api.GET("/v1/settings/prices");
      const body = await unwrap(Promise.resolve(r));
      return { display: body, etag: r.response.headers.get("ETag") ?? "" };
    },
  });
}

export function useSetPriceDisplay() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ show_to, etag }: { show_to: PriceRole[]; etag: string }) =>
      unwrap(
        api.PUT("/v1/settings/prices", {
          params: { header: { "If-Match": etag } },
          body: { show_to },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["settings", "price-display"] });
      // Me's prices_visible may have changed for this admin too.
      void client.invalidateQueries({ queryKey: ["session"] });
    },
  });
}

/** Store an override of the rates (§5.8), or put the list prices back (DELETE). */
export function useSavePriceTable(kind: "azure_blob" | "s3") {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ table, reset }: { table: PriceTable; reset?: boolean }) => {
      const header = { "If-Match": table.etag ?? '"0"' };
      const path = { backend_kind: kind };
      if (reset)
        return unwrap(api.DELETE("/v1/price-tables/{backend_kind}", { params: { path, header } }));
      const { overridden: _o, etag: _e, list: _l, id: _i, ...body } = table;
      return unwrap(api.PUT("/v1/price-tables/{backend_kind}", { params: { path, header }, body }));
    },
    onSuccess: (table) => client.setQueryData(sk.prices(kind), table),
  });
}

export function useApiClients() {
  return useQuery({
    queryKey: sk.clients,
    queryFn: () => unwrap(api.GET("/v1/settings/api-clients")),
  });
}

export function useApiClient(id: string | undefined) {
  return useQuery({
    queryKey: sk.client(id ?? ""),
    enabled: !!id,
    queryFn: () =>
      unwrap(
        api.GET("/v1/settings/api-clients/{api_client_id}", {
          params: { path: { api_client_id: id ?? "" } },
        }),
      ),
  });
}

export function usePeople(q: string) {
  return useQuery({
    queryKey: sk.people(q),
    enabled: q.trim().length >= 2,
    queryFn: () => unwrap(api.GET("/v1/settings/people", { params: { query: { q: q.trim() } } })),
  });
}

function useClientWrite<A>(fn: (args: A) => Promise<ApiClient>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (c) => {
      client.setQueryData(sk.client(c.api_client_id), c);
      void client.invalidateQueries({ queryKey: sk.clients, exact: true });
    },
  });
}

export function useRegisterClient() {
  return useClientWrite((body: ApiClientCreate) =>
    unwrap(api.POST("/v1/settings/api-clients", { params: { header: idem() }, body })),
  );
}

export function useEditClient() {
  return useClientWrite(({ id, etag, body }: { id: string; etag: string; body: ApiClientPatch }) =>
    unwrap(
      api.PATCH("/v1/settings/api-clients/{api_client_id}", {
        params: { path: { api_client_id: id }, header: { "If-Match": etag } },
        body,
      }),
    ),
  );
}

export function useRevokeClient() {
  return useClientWrite((id: string) =>
    unwrap(
      api.POST("/v1/settings/api-clients/{api_client_id}/revoke", {
        params: { path: { api_client_id: id }, header: idem() },
        body: {},
      }),
    ),
  );
}

export type ApiKey = S["ApiKey"];
export type ApiKeyIssued = S["ApiKeyIssued"];
export type ApiKeyCreate = S["ApiKeyCreate"];
export type ApiKeyRotate = S["ApiKeyRotate"];

/**
 * OmniFile-issued API keys (owner decision 2026-11-04). The key string comes
 * back once: callers copy it into component state and `reset()` the mutation
 * at once, and `gcTime: 0` drops a finished mutation, so the key is never kept
 * in TanStack's caches. Only the client (whose key list holds no secret) is
 * fetched again.
 */
function useKeyWrite<A, R>(clientId: string, fn: (args: A) => Promise<R>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    gcTime: 0,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: sk.client(clientId) });
      void client.invalidateQueries({ queryKey: sk.clients, exact: true });
    },
  });
}

export function useCreateKey(clientId: string) {
  return useKeyWrite(clientId, (body: ApiKeyCreate) =>
    unwrap(
      api.POST("/v1/settings/api-clients/{api_client_id}/keys", {
        params: { path: { api_client_id: clientId }, header: idem() },
        body,
      }),
    ),
  );
}

export function useRotateKey(clientId: string) {
  return useKeyWrite(clientId, ({ keyId, body }: { keyId: string; body: ApiKeyRotate }) =>
    unwrap(
      api.POST("/v1/settings/api-clients/{api_client_id}/keys/{key_id}/rotate", {
        params: { path: { api_client_id: clientId, key_id: keyId }, header: idem() },
        body,
      }),
    ),
  );
}

export function useRevokeKey(clientId: string) {
  return useKeyWrite(clientId, (keyId: string) =>
    unwrap(
      api.POST("/v1/settings/api-clients/{api_client_id}/keys/{key_id}/revoke", {
        params: { path: { api_client_id: clientId, key_id: keyId }, header: idem() },
        body: {},
      }),
    ),
  );
}
