# OmniFile portal

The browser front end of OmniFile Gateway (decision 32): a React + TypeScript SPA,
built with Vite, served as static files by the `web` app. It talks only to the
portal JSON API, `service/openapi/portal-api.yaml` (prose: `docs/contracts/portal-api.md`).

```sh
npm ci
npm run dev:mock      # the whole portal against an in-browser mock of the API
npm run dev           # against a running `web` app (OMNIFILE_WEB_URL, default https://localhost:8443)
npm run typecheck && npm run lint && npm test && npm run build
```

- **Toolchain.** Node 22 or later with npm 10.9 or later (`engines`). The lock is written
  with npm 11.19.0 (`packageManager`), on the Node in the repository's `.nvmrc`
  (`nvm use`). The `web` image and the workflows build on Node 22, so with npm 10. Run
  `npm ci` under both before committing a new lock.
- **The `overrides` entry.** `@vitest/mocker` takes `msw ^2` as an optional peer, and the
  portal uses msw 3. npm 11 accepts that. npm 10 wants an msw 2 that the lock doesn't hold,
  and `npm ci` refuses with "Missing: msw@2.15.0 from lock file". The override points that
  peer at the portal's own msw. The mocker only loads msw in Vitest's browser mode, which
  the portal doesn't use.

- `src/api/schema.gen.ts` and `src/mocks/examples.gen.ts` (the YAML's response
  `examples`, typed) are generated from the contract: run `npm run gen:client` after
  `portal-api.yaml` changes. CI (`npm run check:client`) fails when either is stale.
- Response fields marked `x-extensible-enum` (backend kind, tiers, migration state…)
  are open: they are typed as `string`, and the UI shows an unknown value raw
  (`src/lib/format.ts`), never blank (decision 35).
- `src/api/client.ts` adds `X-CSRF-Token` to every unsafe call and an
  `Idempotency-Key` (UUIDv7) to every `POST /v1/…` except the three previews.
- `src/mocks/` is the MSW mock used by `dev:mock` and the tests. It is not in the
  production bundle. In `dev:mock` the sidebar can switch between an ArchiveAdmin,
  a DataSteward who owns a share, and a Reader.
- Browser end-to-end tests live in `e2e/` and belong to the M4 integration lane.
