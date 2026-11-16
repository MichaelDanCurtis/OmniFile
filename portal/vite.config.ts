import react from "@vitejs/plugin-react";
import { msw } from "msw/vite";
import { defineConfig } from "vitest/config";

// `npm run dev:mock` serves the SPA against the MSW mock of portal-api.yaml
// (src/mocks/). `npm run dev` proxies the API to a running `web` app instead.
export default defineConfig(({ mode }) => {
  const mock = mode === "mock";
  const target = process.env.OMNIFILE_WEB_URL ?? "https://localhost:8443";
  const proxied = { target, changeOrigin: false, secure: false };
  return {
    plugins: [react(), ...(mock ? [msw({ mode: "worker-only" })] : [])],
    server: mock
      ? undefined
      : {
          proxy: {
            "/v1": proxied,
            "/auth": proxied,
            "/dev-idp": proxied,
            "/healthz": proxied,
          },
        },
    build: {
      outDir: "dist",
      sourcemap: false,
      // The `web` app serves dist/ with a strict CSP (portal-api.md 2.1):
      // no inline scripts or styles, so nothing may be inlined as data: JS.
      assetsInlineLimit: 0,
      // The main chunk stays under 600 kB: Settings, Migrations and the visual
      // views load as their own chunks (src/app/lazy.tsx), all from this origin.
      chunkSizeWarningLimit: 600,
    },
    test: {
      environment: "jsdom",
      environmentOptions: { jsdom: { url: "http://portal.test/" } },
      setupFiles: ["./src/test/setup.ts"],
      css: false,
      restoreMocks: true,
      testTimeout: 15000,
    },
  };
});
