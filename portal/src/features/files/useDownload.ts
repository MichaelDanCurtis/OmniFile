import { useCallback, useRef, useState } from "react";
import { browserNav } from "../../api/client";
import { getDownload, requestDownload } from "../../api/queries";
import type { Download } from "../../api/types";

export type DownloadStatus =
  | { kind: "idle" }
  | { kind: "preparing" }
  | { kind: "started"; url: string }
  | { kind: "needs_recall"; download: Download }
  | { kind: "slow" }
  | { kind: "failed"; message: string };

const POLL_MS = 1000;

/** Only an https: URL is followed or linked (defence in depth: the SAS URL is server-made). */
export function isSafeDownloadUrl(url: string): boolean {
  try {
    return new URL(url).protocol === "https:";
  } catch {
    return false;
  }
}
const GIVE_UP_MS = 30_000;

/**
 * Downloads go through the worker, which mints a 10-minute read URL
 * (portal-api.md 5.4): ask, poll every second for up to 30 s, then follow it.
 */
export function useDownload(options: { pollMs?: number; giveUpMs?: number } = {}) {
  const [status, setStatus] = useState<DownloadStatus>({ kind: "idle" });
  const generation = useRef(0);
  const pollMs = options.pollMs ?? POLL_MS;
  const giveUpMs = options.giveUpMs ?? GIVE_UP_MS;

  const start = useCallback(
    async (fileId: string, versionId: string) => {
      const mine = ++generation.current;
      setStatus({ kind: "preparing" });
      try {
        let download = await requestDownload(fileId, versionId);
        const startedAt = Date.now();
        while (download.state === "preparing") {
          if (generation.current !== mine) return;
          if (Date.now() - startedAt > giveUpMs) {
            setStatus({ kind: "slow" });
            return;
          }
          await new Promise((resolve) => setTimeout(resolve, pollMs));
          download = await getDownload(download.download_id);
        }
        if (download.state === "ready" && download.url && !isSafeDownloadUrl(download.url)) {
          setStatus({
            kind: "failed",
            message: "The download link wasn't a secure (https) address, so it wasn't opened.",
          });
        } else if (download.state === "ready" && download.url) {
          setStatus({ kind: "started", url: download.url });
          browserNav.assign(download.url);
        } else if (download.state === "needs_recall") {
          setStatus({ kind: "needs_recall", download });
        } else {
          setStatus({
            kind: "failed",
            message: download.failure ?? "The download link couldn't be made.",
          });
        }
      } catch (error) {
        setStatus({
          kind: "failed",
          message: error instanceof Error ? error.message : "The download failed.",
        });
      }
    },
    [pollMs, giveUpMs],
  );

  const reset = useCallback(() => {
    generation.current += 1;
    setStatus({ kind: "idle" });
  }, []);

  return { status, start, reset };
}
