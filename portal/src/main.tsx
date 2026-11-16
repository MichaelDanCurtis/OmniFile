import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { installPreloadRecovery } from "./app/lazy";
import { applyStoredTheme } from "./app/theme";
import "./styles/app.css";

async function boot() {
  // `npm run dev:mock` only. MODE is replaced at build time, so the mock and
  // MSW are dropped from the production bundle.
  if (import.meta.env.MODE === "mock") {
    const { startMockWorker } = await import("./mocks/browser");
    await startMockWorker();
  }
  applyStoredTheme();
  // A stale tab after a deploy: reload once to fetch the current chunks (app/lazy.tsx).
  installPreloadRecovery();
  const root = document.getElementById("root");
  if (!root) throw new Error("#root missing");
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void boot();
