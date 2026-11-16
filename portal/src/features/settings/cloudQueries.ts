import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, browserNav, idem, unwrap } from "../../api/client";
import type { components } from "../../api/schema.gen";
import { sk } from "./queries";

/**
 * Connect Azure, Connect AWS and Deploy Storage Mover (docs/design/cloud-connect.md).
 * Nothing here holds a token: a sign-in is a top-level navigation to Entra, and
 * the service's callback uses the admin's token for one request and drops it.
 */

type S = components["schemas"];
export type ConnectStep = S["ConnectStep"];
export type AzureSettings = S["AzureSettings"];
export type AzureGrantPlan = S["AzureGrantPlan"];
export type AzureGrant = S["AzureGrant"];
export type AzureStorageAccount = S["AzureStorageAccount"];
export type AzureDiscovery = S["AzureDiscovery"];
export type AwsConnect = S["AwsConnect"];
export type AwsConnectEntry = S["AwsConnectEntry"];
export type AwsConnection = S["AwsConnection"];
export type AwsFetchResult = S["AwsFetchResult"];
export type CloudTask = S["CloudTask"];
export type MoverDeployment = S["MoverDeployment"];
export type StorageMoverSettings = S["StorageMoverSettings"];

/** How often tasks and Storage Mover's agents are read. Tests turn these down. */
export const cloudPolling = { taskMs: 1000, moverMs: 15_000 };

export const ck = {
  azure: ["settings", "azure"] as const,
  awsConnect: (region: string, dep: string) => ["settings", "aws", "connect", region, dep] as const,
  task: (id: string) => ["settings", "tasks", id] as const,
};

const SETTLED = new Set(["done", "capped", "failed"]);
export const isSettled = (state: string) => SETTLED.has(state);

export function useAzureSettings() {
  return useQuery({
    queryKey: ck.azure,
    queryFn: () => unwrap(api.GET("/v1/settings/azure")),
  });
}

/**
 * Starts a sign-in to Azure Resource Manager and sends the browser to Entra.
 * `browserNav.assign` is the one navigation helper; tests stub it.
 */
export function useAzureSignIn() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { purpose: "list" | "grant" | "remove"; grant_ids?: string[] }) =>
      unwrap(api.POST("/v1/settings/azure/sign-in", { params: { header: idem() }, body })),
    onSuccess: (s) => {
      void client.invalidateQueries({ queryKey: ck.azure });
      browserNav.assign(s.authorize_url);
    },
  });
}

function useAzureWrite<A>(fn: (args: A) => Promise<AzureSettings>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (s) => {
      client.setQueryData(ck.azure, s);
      void client.invalidateQueries({ queryKey: sk.mover });
    },
  });
}

export function useAzureChoice() {
  return useAzureWrite(
    (body: {
      subscription_id: string;
      resource_groups: string[];
      mover_resource_group: string | null;
    }) => unwrap(api.PUT("/v1/settings/azure/choice", { body })),
  );
}

export function useAzureDiscover() {
  return useAzureWrite(() =>
    unwrap(api.POST("/v1/settings/azure/discover", { params: { header: idem() }, body: {} })),
  );
}

export function useAzureConfirm() {
  return useAzureWrite(
    (body: {
      storage_accounts: string[];
      virtual_networks: string[];
      private_endpoints: string[];
    }) => unwrap(api.POST("/v1/settings/azure/confirm", { params: { header: idem() }, body })),
  );
}

export function useAwsConnect(region: string, deploymentId: string) {
  return useQuery({
    queryKey: ck.awsConnect(region, deploymentId),
    placeholderData: (previous) => previous,
    queryFn: () =>
      unwrap(
        api.GET("/v1/settings/aws/connect", {
          params: {
            query: {
              ...(region ? { region } : {}),
              ...(deploymentId ? { deployment_id: deploymentId } : {}),
            },
          },
        }),
      ),
  });
}

export interface AwsFetchBody {
  account_id: string;
  region: string;
  deployment_id: string;
  endpoints?: { s3?: string; sts?: string; sqs?: string };
}

export function useAwsFetch() {
  return useMutation({
    mutationFn: (body: AwsFetchBody) =>
      unwrap(api.POST("/v1/settings/aws/connect/fetch", { params: { header: idem() }, body })),
  });
}

export function useAwsOutputs() {
  return useMutation({
    mutationFn: (text: string) =>
      unwrap(
        api.POST("/v1/settings/aws/connect/outputs", {
          params: { header: idem() },
          body: { text },
        }),
      ),
  });
}

export interface AwsConfirmBody {
  account_id: string;
  region: string;
  deployment_id: string;
  outputs: Record<string, string>;
  buckets: string[];
}

export function useAwsConfirm() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AwsConfirmBody) =>
      unwrap(api.POST("/v1/settings/aws/connect/confirm", { params: { header: idem() }, body })),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["settings", "aws"] });
    },
  });
}

/** A task the worker runs (an AWS fetch, a Storage Mover deployment), polled until it settles. */
export function useCloudTask(taskId: string | null) {
  return useQuery({
    queryKey: ck.task(taskId ?? ""),
    enabled: !!taskId,
    refetchInterval: (q) => {
      const t = q.state.data;
      return t && isSettled(t.state) ? false : cloudPolling.taskMs;
    },
    queryFn: () =>
      unwrap(
        api.GET("/v1/settings/tasks/{task_id}", { params: { path: { task_id: taskId ?? "" } } }),
      ),
  });
}

export function useDeployMover() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; project: string; location: string }) =>
      unwrap(api.POST("/v1/settings/storage-mover/deploy", { params: { header: idem() }, body })),
    onSuccess: () => void client.invalidateQueries({ queryKey: sk.mover }),
  });
}

/** Storage Mover, read every 15 s while a deployment waits for its agent to come Online. */
export function useStorageMoverLive() {
  return useQuery({
    queryKey: sk.mover,
    refetchInterval: (q) => {
      const m = q.state.data;
      if (!m?.deployment) return false;
      return m.agents.some((a) => a.status === "Online") ? false : cloudPolling.moverMs;
    },
    queryFn: () => unwrap(api.GET("/v1/settings/storage-mover")),
  });
}

/** The AwsFetchResult a done aws_fetch task carries (CloudTask.result is an open object). */
export function fetchResultOf(task: CloudTask | undefined): AwsFetchResult | null {
  if (!task || task.kind !== "aws_fetch" || task.state !== "done") return null;
  const r = task.result as Partial<AwsFetchResult>;
  return typeof r.account_id === "string" && Array.isArray(r.buckets)
    ? (r as AwsFetchResult)
    : null;
}
