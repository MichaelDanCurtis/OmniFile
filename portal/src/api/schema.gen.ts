/**
 * GENERATED from service/openapi/portal-api.yaml by scripts/gen-client.mjs (types).
 * Do not edit. Run `npm run gen:client` after the contract changes;
 * CI runs `npm run check:client` and fails when this file is stale.
 */

export interface paths {
    "/auth/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Start Entra sign-in (auth code + PKCE). Redirects to the authorize endpoint. */
        get: operations["login"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/callback": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Entra redirects here. Redeems the code, creates the session, redirects to return_to. */
        get: operations["authCallback"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/session": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The current session, with the CSRF token. 401 when signed out. */
        get: operations["getSession"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/logout": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** End the session. Returns Entra's end-session URL for the SPA to visit. */
        post: operations["logout"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/me": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getMe"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/directory/groups": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** AD groups known to the catalog (named by the worker's nightly Graph sync). ArchiveAdmin or share Owners. */
        get: operations["searchGroups"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/shares": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listShares"];
        put?: never;
        /** A share of a dataset's root, on the dataset's edge, which renders and serves it (portal-api.md 5.16). */
        post: operations["addShare"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/shares/{share_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getShare"];
        put?: never;
        post?: never;
        /** The share stops being served; the dataset, its files and their versions stay (portal-api.md 5.16). */
        delete: operations["removeShare"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/shares/{share_id}/settings": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /** Open policy, rewind depth and maximum, NFS security. Each field has its own role (portal-api.md 5.6). */
        patch: operations["patchShareSettings"];
        trace?: never;
    };
    "/v1/shares/{share_id}/settings/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Checks for proposed settings (Kerberos note, host-trusted). Nothing is saved. No Idempotency-Key. */
        post: operations["previewShareSettings"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/shares/{share_id}/access": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** The share's Owners, Writers and Readers AD groups. Owners group by ArchiveAdmin only. */
        put: operations["putShareAccess"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/shares/{share_id}/worm/enable": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Turn WORM on (one-way once locked). ArchiveAdmin, re-auth within 10 minutes, typed share name. */
        post: operations["enableWorm"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/shares/{share_id}/worm/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Turn WORM off inside the validation window only. ArchiveAdmin, re-auth, typed share name. */
        post: operations["cancelWorm"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/shares/{share_id}/worm/extend": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Change retention days; once locked, only upwards. ArchiveAdmin. */
        post: operations["extendWorm"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/dirs/{dir_id}/children": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** A folder's readable children, by name (bytewise). With as_of, the folder as it was then. */
        get: operations["listChildren"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/files/{file_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getFile"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/files/{file_id}/versions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Versions inside the rewind depth (or retained by WORM) the caller may see, newest first. */
        get: operations["listVersions"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/files/{file_id}/recall-options": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getRecallOptions"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/files/{file_id}/versions/{version_id}/restore": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Restore an old version as the new current version, or as a copy. Owner or Writer of the share. */
        post: operations["restoreVersion"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/files/{file_id}/versions/{version_id}/downloads": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Ask for a short-lived download URL for one version (minted by the worker). */
        post: operations["requestDownload"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/version-restores/{restore_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getVersionRestore"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/downloads/{download_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getDownload"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/recalls": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listRecalls"];
        put?: never;
        /** Recall ahead of time. Starts or joins the one active recall of that content version. */
        post: operations["requestRecall"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/recalls/{recall_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The four-step recall view. */
        get: operations["getRecall"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/pins/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Warnings and fees for a tier pin, before it is applied. Nothing is saved. No Idempotency-Key. */
        post: operations["previewPins"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/pins": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listPins"];
        put?: never;
        /** Pin files or folders to a tier (decision 29). Share Owners or DataStewards, with read on every item. */
        post: operations["createPins"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/pins/{pin_id}/release": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["releasePin"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/holds": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listHolds"];
        put?: never;
        /** keep_online (Owners, DataStewards) or legal (ArchiveAdmin, re-auth) holds on files or folders. */
        post: operations["createHolds"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/holds/{hold_id}/release": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["releaseHold"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/datasets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listDatasets"];
        put?: never;
        /** A dataset on an edge, with its root folder (portal-api.md 5.16). */
        post: operations["addDataset"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/backends": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The storage backends a dataset can live on (portal-api.md 5.16). */
        get: operations["listBackends"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/datasets/{dataset_id}/policy": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getActivePolicy"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/datasets/{dataset_id}/policies": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listPolicyVersions"];
        put?: never;
        /** A new active policy version, authored and approved by the calling ArchiveAdmin. */
        post: operations["createPolicyVersion"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/policies/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Live timeline, first-year cost per TB and checks for a policy body. Pure; no Idempotency-Key. */
        post: operations["previewPolicy"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/policies/proposals": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listProposals"];
        put?: never;
        /** Submit a policy proposal. It changes nothing until an ArchiveAdmin approves it (anti-pattern 7). */
        post: operations["submitProposal"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/policies/proposals/{proposal_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getProposal"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/policies/proposals/{proposal_id}/approve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** ArchiveAdmin, not the proposer. Creates the active PolicyVersion with proposal_id. */
        post: operations["approveProposal"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/policies/proposals/{proposal_id}/reject": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["rejectProposal"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/policies/proposals/{proposal_id}/withdraw": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** The proposer only (identity or application). */
        post: operations["withdrawProposal"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/price-tables/{backend_kind}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getPriceTable"];
        /** Store an admin override. ArchiveAdmin. */
        put: operations["putPriceTable"];
        post?: never;
        /** Return to the built-in table. ArchiveAdmin. */
        delete: operations["deletePriceTableOverride"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/audit": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listAudit"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/source-scans": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Scan a source before registering it (cloud-connect.md 4) - a capped, read-only listing (or its S3 Inventory) by the worker, with prefixes, the storage-class mix, a restore estimate and suggested waves. Poll GET /v1/source-scans/{scan_id}. */
        post: operations["startSourceScan"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/source-scans/{scan_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** A scan, its progress, and when done, what it found. */
        get: operations["getSourceScan"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/sources": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listSources"];
        put?: never;
        /** Register data that already lives in a backend as a migration source (portal-api.md 5.10). */
        post: operations["registerSource"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/adopt": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Catalog data already moved into the dataset's own backend, at its original paths (portal-api.md 5.10). */
        post: operations["adoptData"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/sources/{source_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getSource"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/sources/{source_id}/retry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Resume a failed source where it stopped. */
        post: operations["retrySource"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/source-backends": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The backends data can migrate from, with their buckets, for the portal's New migration wizard (portal-api.md 5.10). ArchiveAdmin sessions only. */
        get: operations["listSourceBackends"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/files": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Catalog rows by path or id - the entry, and a folder's children (portal-api.md 5.11). Apps only - a signed-in user gets 403 forbidden. */
        get: operations["getFilesByPath"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/restores": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Provider restores (user recalls, migration waves, API), newest first. */
        get: operations["listRestores"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The catalog's ordered pull feed (portal-api.md 5.11). Works over private networking. */
        get: operations["getEvents"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/usage": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Bytes and monthly storage cost by tier, share and dataset, from the latest daily snapshot. */
        get: operations["getUsage"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/activity": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Recall and upload activity per day and in total, from the catalog (portal-api.md 5.13). */
        get: operations["getActivity"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/activity/live": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** What happened since `since`, newest first, with live counters, per-minute series and a strip of the edges; a long-poll with `wait` (portal-api.md 5.13). */
        get: operations["getLiveActivity"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/edges": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Every edge that isn't revoked, with what its heartbeat last reported (portal-api.md 5.14). */
        get: operations["listEdges"];
        put?: never;
        /** A new edge, pending, and its one-time enrolment bundle for the edge's setup (portal-api.md 5.15). */
        post: operations["addEdge"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/edges/appliance": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** What the Add edge wizard shows: the release's install image per platform, where they are published if the operator said so, and whether enrolment is configured (portal-api.md 5.15). */
        get: operations["getEdgeAppliance"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/edges/{edge_id}/bundle": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** A new enrolment bundle for an edge that hasn't enrolled; the previous one stops working (portal-api.md 5.15). */
        post: operations["newEdgeBundle"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/edges/{edge_id}/revoke": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** A lost or stolen filer stops working at once - its certificate is refused and it can't renew; its data stays (portal-api.md 5.15). ArchiveAdmin, re-auth within 10 minutes. One-way. */
        post: operations["revokeEdge"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/locations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listLocations"];
        put?: never;
        /** Move a prefix of a migrating dataset to its own backend - copy, verify every file, cut over (portal-api.md 5.12). */
        post: operations["requestLocation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/locations/{migration_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getLocation"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listMigrations"];
        put?: never;
        /** Start a migration of a prefix to the dataset's own backend, with the lifecycle's choices (portal-api.md 5.17). */
        post: operations["startMigration"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getMigration"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/cutover": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Flip a manual migration once every file is verified. The app that started it, or an ArchiveAdmin. */
        post: operations["cutoverMigration"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Stop a migration; nothing is deleted. The app that started it, or an ArchiveAdmin. */
        post: operations["cancelMigration"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/retry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Start a failed migration again. The app that started it, or an ArchiveAdmin. */
        post: operations["retryMigration"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/runs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** An external copier reports a run (display only; OmniFile verifies every file). The app that started the migration. */
        post: operations["reportMigrationRun"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/exclude": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Take objects that can never be verified out of the bar; they stay on the source. ArchiveAdmin, re-authenticated. */
        post: operations["excludeMigrationObjects"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/tombstones": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The cutover report - deletes made through OmniFile, never applied to the source (decision 40). The full list (owner, 2026-11-04) - for the app that started the migration, a registered API client with Archive.Adopt for one started in the portal, and a signed-in ArchiveAdmin with an AD identity. Every read is audited, one returning no rows too. An API client never reads another client's migration. */
        get: operations["getMigrationTombstones"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-console": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The migrations operations console - every migration's progress, throughput, ETA and health, and what needs attention (portal-api.md 5.22). ArchiveAdmin sessions only. */
        get: operations["getMigrationConsole"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-agents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Which Storage Mover agent, edge and job runs what, across migrations (portal-api.md 5.22). ArchiveAdmin sessions only. */
        get: operations["listMigrationAgents"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/agents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The agents, edge, restore waves, checksum jobs and verification of one migration (portal-api.md 5.22). ArchiveAdmin sessions only. */
        get: operations["getMigrationWork"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/stuck-copies": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Which objects the worker last counted as stranded or with no checksum, a page at a time (portal-api.md 5.22). ArchiveAdmin sessions only. Every read is audited. */
        get: operations["getMigrationStuckCopies"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/rerun": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Copy again - a new Storage Mover run at the engine's next step, which also copies again what failed its check. ArchiveAdmin sessions. */
        post: operations["rerunMigration"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migrations/{migration_id}/restore-again": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Restore again the restored S3 copies that expire within 24 hours unverified (in the engine's next restore wave). ArchiveAdmin sessions. */
        post: operations["restoreMigrationAgain"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-plans": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["listMigrationPlans"];
        put?: never;
        /** A staged plan - waves of prefixes with start times, windows, blackouts and dependencies. Approved at once for an ArchiveAdmin; a proposal from an API client (portal-api.md 5.22). */
        post: operations["createMigrationPlan"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-plans/split": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** A source's next-level prefixes with their counts, in N waves of about the same size (by bytes or count). */
        get: operations["splitMigrationSource"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-plans/{plan_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getMigrationPlan"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-plans/{plan_id}/approve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Approve a proposed plan (an ArchiveAdmin, not its proposer). */
        post: operations["approveMigrationPlan"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-plans/{plan_id}/reject": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Reject a proposed plan. */
        post: operations["rejectMigrationPlan"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-plans/{plan_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancel the waves not started; started ones go on. */
        post: operations["cancelMigrationPlan"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-plans/{plan_id}/waves/{seq}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /** Reschedule a wave not started (the schedule view's drag), checked against its dependencies and windows. */
        patch: operations["rescheduleMigrationWave"];
        trace?: never;
    };
    "/v1/migration-plans/{plan_id}/waves/{seq}/cutover": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** A manual wave's cutover, for its verified migrations only (decision 24). */
        post: operations["cutoverMigrationWave"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-plans/{plan_id}/waves/{seq}/retry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Retry a wave's failed migrations (back to pending); the only way to retry a plan's migration. */
        post: operations["retryMigrationWave"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/migration-plans/{plan_id}/waves/{seq}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancel a wave's migrations not cut over; nothing is deleted. The only way to cancel a plan's migration. */
        post: operations["cancelMigrationWave"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/recall-batches": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Recall paths, or a whole dataset, by a due date (portal-api.md 5.19). Apps with Recall.Request. */
        post: operations["createRecallBatch"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/recall-batches/{batch_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The batch and a page of its planned files. The app that made it only. */
        get: operations["getRecallBatch"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/files/{file_id}/journey": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The tiers of the backend that holds the file, which account holds it, and the pin and holds that apply (portal-api.md 5.22). Read on the file. */
        get: operations["getFileJourney"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/shares/{share_id}/map": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Files and bytes under the share, on the edge and per backend, account and tier, with recalls and migrations in flight (portal-api.md 5.22). */
        get: operations["getShareMap"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/shares/{share_id}/map/files": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The readable files, in readable folders, behind one node of the share map, by file_id (portal-api.md 5.22). At least one of edge, tier or recalling. */
        get: operations["listShareMapFiles"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/healthz": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["healthz"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/storage": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The storage backends, with what they are and what uses them (portal-api.md 5.22). */
        get: operations["getStorageSettings"];
        put?: never;
        /** Add a storage backend, checked by the same code as the admin CLI (`omnifile_service.admin`). Re-auth within 10 minutes. Idempotent by name. */
        post: operations["addStorageBackend"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/storage/{backend_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Edit a backend. Once data uses it, only S3 buckets may be added. Never turns private networking off. Re-auth within 10 minutes. */
        put: operations["editStorageBackend"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/storage/{backend_id}/checks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Live checks of one backend - private DNS, the service's RBAC role, the live/archive pair and change feed, or for S3 the role (portal-api.md 5.22). */
        get: operations["checkStorageBackend"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/aws": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The service's AWS settings - accounts, roles and the web-identity audience (decision 43). No keys anywhere. */
        get: operations["getAwsSettings"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/aws/trust-policy": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The exact trust policy to give the pasted role, federating the service's Entra identity (decision 43). Nothing is stored. */
        get: operations["getAwsTrustPolicy"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/aws/test": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** The worker calls AssumeRoleWithWebIdentity for the role with the service's identity and reports the result. Poll GET /v1/settings/tests/{test_id}. */
        post: operations["testAwsRole"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/identity": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The tenant, the app roles and the AD groups that hold them, and admin-consent status with its link. */
        get: operations["getIdentitySettings"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/identity/whoami": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Who the portal thinks you are, and the checks that follow from it (SID, groups, roles, re-auth). */
        get: operations["whoAmI"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/teams": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The Teams bot's health from recent cards, and the app package. */
        get: operations["getTeamsSettings"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/teams/package": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The Teams app package deploy/teams/build.sh made, when the service can reach it (`404 not_found` otherwise; the settings page says how to build it). */
        get: operations["getTeamsPackage"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/teams/test-card": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Send yourself a test card through the worker's notify path. Poll GET /v1/settings/tests/{test_id}. */
        post: operations["sendTestCard"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/email": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The fallback email's sender mailbox and recent sends. */
        get: operations["getEmailSettings"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/email/test": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Send yourself a test email from the sender mailbox, through the worker. Poll GET /v1/settings/tests/{test_id}. */
        post: operations["sendTestEmail"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/tests/{test_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** A test send or role assumption you asked for, and how it went. */
        get: operations["getSettingsTest"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/storage-mover": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Azure Storage Mover's agents and projects with their status, read-only, through the ARM API the migration engine uses. */
        get: operations["getStorageMoverSettings"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/azure": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Connect Azure (cloud-connect.md 1) - what the admin listed and chose, the grants planned and made, what was discovered and confirmed, and the steps still to do. */
        get: operations["getAzureSettings"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/azure/sign-in": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Start a delegated sign-in to Azure Resource Manager (auth code + PKCE). purpose list reads the tenants, subscriptions and resource groups; grant makes the planned grants; remove removes the named ones. The token is used in the callback and dropped. */
        post: operations["startAzureSignIn"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/azure/callback": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Entra redirects here after a Connect Azure sign-in. Redeems the code, checks the ID token is the admin who started it, does the operation's work with the access token, and redirects to /settings/azure. */
        get: operations["azureConnectCallback"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/azure/choice": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Choose the subscription and resource groups (from the latest listing), and optionally the resource group for Storage Mover. Returns the plan of grants; nothing is granted yet. */
        put: operations["putAzureChoice"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/azure/discover": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Read the storage accounts, VNets, private endpoints and storage movers in the granted resource groups, with the service's own identity. */
        post: operations["discoverAzure"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/azure/confirm": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Confirm which discovered resources OmniFile uses (by resource id). Audited. It doesn't register backends; Storage does. */
        post: operations["confirmAzure"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/aws/connect": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Connect AWS (cloud-connect.md 2) - the CloudFormation quick-create link with this deployment's values, the template's parameters, the steps, and the accounts connected so far. */
        get: operations["getAwsConnect"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/aws/connect/template": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The CloudFormation template (deploy/aws/cloudformation/omnifile-connect.yaml), for the console's "Upload a template file" when no quick-create link is configured. */
        get: operations["getAwsConnectTemplate"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/aws/connect/fetch": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** After the stack is made - the worker assumes the new role with its web identity and reads the account's buckets and the event queue. Read-only, credentials never stored. Poll GET /v1/settings/tasks/{task_id}. */
        post: operations["fetchAwsConnect"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/aws/connect/outputs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** The fallback - the stack's outputs pasted by hand (the console's Outputs tab, describe-stacks JSON, or Key=Value lines), turned into the account's settings entry. Nothing is called or stored. */
        post: operations["parseAwsOutputs"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/aws/connect/confirm": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Confirm the account's settings entry (from a fetch or pasted outputs) and the buckets. Recorded and audited; the entry is then set in the deployment (OMNIFILE_AWS_ACCOUNTS), never in the catalog (cloud-connect.md 2.3). */
        post: operations["confirmAwsConnect"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/storage-mover/deploy": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Create the Storage Mover and its project in the resource group chosen in Connect Azure, by the worker with its granted role (cloud-connect.md 3). Poll GET /v1/settings/tasks/{task_id}. */
        post: operations["deployStorageMover"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/tasks/{task_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** A task the worker runs for Settings (an AWS fetch, a Storage Mover deployment), and how it went. */
        get: operations["getCloudTask"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/alerts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The alert rules (observability.md 1), with what the catalog says of each now. Read-only. */
        get: operations["getAlertSettings"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/prices": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Which portal roles see prices (portal-api.md 5.24). ArchiveAdmin. */
        get: operations["getPriceDisplay"];
        /** Change which roles see prices. ArchiveAdmin, audited (portal-api.md 5.24). */
        put: operations["putPriceDisplay"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/people": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** People in the tenant the catalog knows (signed in once, or synced), for an API client's owner. */
        get: operations["searchPeople"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/api-clients": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Every registered API client, and how a client gets a token (portal-api.md 5.21). */
        get: operations["listApiClients"];
        put?: never;
        /** Register an application by its Entra client id, with its scopes and its owner. Re-auth within 10 minutes. No key is made or stored. */
        post: operations["registerApiClient"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/api-clients/{api_client_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getApiClient"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /** Rename, change scopes or owner, disable or enable. Disabling takes effect on the client's next call and needs no re-auth; anything else does. */
        patch: operations["editApiClient"];
        trace?: never;
    };
    "/v1/settings/api-clients/{api_client_id}/revoke": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Revoke a client for good; its calls are refused from the next one. Its proposals and audit records stay. */
        post: operations["revokeApiClient"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/api-clients/{api_client_id}/keys": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** An OmniFile API key for the client, shown once and kept only as a hash (portal-api.md 5.21). Entra app credentials stay the recommended way. Re-auth within 10 minutes. Never replayed from the idempotency store. */
        post: operations["createApiKey"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/api-clients/{api_client_id}/keys/{key_id}/rotate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** A new key with the same scopes; the old one keeps working until the overlap ends (7 days by default), then expires. Re-auth within 10 minutes. */
        post: operations["rotateApiKey"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/api-clients/{api_client_id}/keys/{key_id}/revoke": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Revoke a key at once; its next call is refused. Needs no re-auth. */
        post: operations["revokeApiKey"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/filers": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Registered NAS filers (portal-api.md 5.23). */
        get: operations["listFilers"];
        put?: never;
        /** Register a NAS (any NFS or SMB server) on an edge. ArchiveAdmin. */
        post: operations["addFiler"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/filers/{filer_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getFiler"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /** Which account places files on this NAS (`placement_sid`), or null to clear it. Every SMB filer needs it. ArchiveAdmin, freshly signed in, with If-Match, audited. Only the SID this filer's own edge currently reports, of a user or machine account. A 400 names which it wasn't - well_known, no_report, stale_report (over 6 hours), different_sid, not_an_account, not_smb. */
        patch: operations["patchFiler"];
        trace?: never;
    };
    "/v1/filers/{filer_id}/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The edge's last mount check of the filer. */
        get: operations["getFilerHealth"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The annexes the caller may see (portal-api.md 5.23). */
        get: operations["listAnnexes"];
        put?: never;
        /** An annex in Copy mode (the only mode in phase A1). */
        post: operations["addAnnex"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["getAnnex"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /** Settings, pause and resume, retire. The mode never changes. */
        patch: operations["patchAnnex"];
        trace?: never;
    };
    "/v1/annexes/{annex_id}/rulesets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The annex's rule set versions, newest first. */
        get: operations["listAnnexRulesets"];
        put?: never;
        /** Propose a rule set (it never applies anything). Checked (422 rules_check_failed), and given a dry run. Owners and ArchiveAdmins; apps with Archive.Adopt, naming its owner. */
        post: operations["proposeAnnexRuleset"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/rulesets/{version}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** One version, its body, its checks today and its dry run. */
        get: operations["getAnnexRuleset"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/rulesets/{version}/approve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Approve a proposal: an ArchiveAdmin if it adds or widens a removal, else an Owner; never its own person, nor anyone answering for its app. */
        post: operations["approveAnnexRulesetVersion"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/rulesets/{version}/reject": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Reject a proposal. The annex's Owners and ArchiveAdmins. */
        post: operations["rejectAnnexRulesetVersion"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/dry-runs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview a draft rule set (or the running one) over the edge's listing. Read-only; the edge answers within seconds. */
        post: operations["createAnnexDryRun"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/dry-runs/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** A preview's state and report. */
        get: operations["getAnnexDryRun"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/return-preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** The return rules (`recall_completed`) over the annex's removed files: per rule the files it takes (and may take, when it names requesters), per target the names taken today and the bytes needed against free space. Computed by the service. ArchiveAdmins and apps only. */
        post: operations["previewAnnexReturns"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/files/{file_id}/return-targets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Where the annex's return rules let the requester have this annexed file placed, default first, each with the requester's write check, today's name conflict and free space. An API client names the person (`requester`). */
        get: operations["getReturnTargets"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/placements": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Have an annexed file placed back on the NAS or in a share, at one of its return targets. Brings the bytes back from Archive first (`priority`). A second request for the same file and target joins the open one. */
        post: operations["requestPlacement"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/placements/{placement_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** A placement's progress and where the file landed. */
        get: operations["getPlacement"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/placements/{placement_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** The way out of a placement that can't go on: its requester's or an ArchiveAdmin's. One that has its `go` can't be cancelled (`409 placement_not_cancellable`), because the file may already be under its name. Audited. */
        post: operations["cancelPlacement"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/placements/{placement_id}/abandon": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** The way out of a placement stuck after its go, when the edge has said nothing new for a day (`409 placement_not_abandonable` otherwise). An ArchiveAdmin's, audited. It ends as `failed: abandoned`, whatever the edge reports later is refused and audited, and the next scan finds the file on the NAS, so the worst case is a recopy. */
        post: operations["abandonPlacement"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/removals/confirm": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** End the dry run: an ArchiveAdmin who reviewed it confirms, with a sign-in in the last 10 minutes, for the running rule set version (decision 59). Never the person who proposed the rules, nor anyone answerable for its app. */
        post: operations["confirmAnnexRemovals"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/removals": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The annex's removals and their steps, newest first, filtered by the files' ACLs. */
        get: operations["listAnnexRemovals"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The annex's timeline of events, newest first. */
        get: operations["listAnnexEvents"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/files": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The annex's copied files by path, as the NAS's ACLs let the caller read them. */
        get: operations["listAnnexFiles"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/timeline": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Arrivals, departures, changes, copies and verifications per UTC day. */
        get: operations["getAnnexTimeline"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/problems": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Reconciliation drift and the copier's problems. */
        get: operations["getAnnexProblems"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/files/{file_id}/purge": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Purge the kept copy of a file deleted on the NAS. ArchiveAdmin, with a sign-in in the last 10 minutes (`401 reauth_required` otherwise). */
        post: operations["purgeAnnexFile"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/files/{file_id}/clear-breaker": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** An Owner looked at a file the loop breaker pinned: it may leave the NAS again, and the breaker counts returns from now. An ArchiveAdmin or the annex's Owners. Audited. */
        post: operations["clearAnnexFileBreaker"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/files/{file_id}/copy-stop": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** A copy Azure's read-back keeps refusing: its edge doesn't copy the same bytes again, its own reconciliation included, until a version verifies or an ArchiveAdmin resumes it (`copy-resume`). A changed file is still copied. Only a file whose copy was refused (`409 copy_not_refused`). ArchiveAdmin, with a sign-in in the last 10 minutes. Audited as `annex.copy.stopped`. */
        post: operations["stopAnnexFileCopy"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/files/{file_id}/copy-resume": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Its edge may copy the file again. ArchiveAdmin, with a sign-in in the last 10 minutes. Audited as `annex.copy.resumed`, with `was_stopped`. */
        post: operations["resumeAnnexFileCopy"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/annexes/{annex_id}/files/{file_id}/copy-refused-ack": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** An ArchiveAdmin has seen the refusals: the file leaves the gauge `omnifile.annex.copies_refused`, so the alert `annex-copies-refused` quiets for it alone, until a version verifies, or until its copy is refused again while copying isn't stopped. It stays on Problems. With a sign-in in the last 10 minutes. Audited as `annex.copy.refused_ack`. */
        post: operations["acknowledgeAnnexFileCopyRefused"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/settings/annex-defaults": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** The quiet period and scan schedules new annexes start with. */
        get: operations["getAnnexDefaults"];
        /** Change the defaults. ArchiveAdmin. */
        put: operations["putAnnexDefaults"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        FilerCreate: {
            name: string;
            /** @enum {string} */
            vendor?: "generic" | "ontap" | "powerscale";
            host: string;
            /** @enum {string} */
            protocol: "smb" | "nfs3" | "nfs4";
            /** @description SMB share name, or NFS export path. */
            export: string;
            edge_id: components["schemas"]["Id"];
            /** @enum {string} */
            credential_mode?: "machine_account" | "service_keytab" | "sec_sys";
            /** @enum {string} */
            event_source?: "scan" | "smb_notify";
        };
        Filer: {
            filer_id: components["schemas"]["Id"];
            name: string;
            vendor: string;
            host: string;
            protocol: string;
            export: string;
            edge_id: components["schemas"]["Id"];
            credential_mode: string;
            /** @description sec=sys: the NAS trusts the edge's address. */
            host_trusted: boolean;
            event_source: string;
            health: {
                [key: string]: unknown;
            } | null;
            /** Format: date-time */
            health_checked_at: string | null;
            /** @description The account that writes placed files on this NAS. Every SMB filer needs it: the staged file's owner is whoever placed it, and at `go` only this account's entries count as the file's own owner's. Registered with `PATCH /v1/filers/{filer_id}`. */
            placement_sid?: string | null;
            /** @description What a person sees before registering it: what this filer's own edge reports from winbind about the account it places files with. For an ArchiveAdmin, who alone can act on it; null for anyone else, and on a filer that isn't SMB. `can_register` says whether the route would take `sid`, and `why_not` says why not (`no_report`, `stale_report`, `not_an_account`). It registers nothing by itself. */
            placement_account_report?: {
                sid: string | null;
                /** @description user, computer, group, ... */
                kind: string | null;
                /** @description The principal the edge holds. */
                name: string | null;
                /** @description Why the edge can't name its account. */
                error: string | null;
                /** Format: date-time */
                reported_at: string | null;
                can_register: boolean;
                why_not: string | null;
            } | null;
            /** @description Decision 65: whether the share or export limits access beyond the files' permissions, as an ArchiveAdmin answered (`PATCH /v1/filers/{filer_id}`), with who and when. Unanswered (`answered` false): its annexes' files are hidden. */
            access?: {
                answered: boolean;
                limited: boolean | null;
                groups: string[];
                confirmed_by: string | null;
                /** Format: date-time */
                confirmed_at: string | null;
            };
            /** @description Decision 64: since when the filer's NAS has shown no ACLs through its mount (the edge checks at every mount), and who acknowledged it. `blocks_grants`: flagged and not acknowledged, so group-readable placements and share copies of its files are refused. */
            acls?: {
                /** Format: date-time */
                hidden_since: string | null;
                acknowledged_by: string | null;
                /** Format: date-time */
                acknowledged_at: string | null;
                blocks_grants: boolean;
            };
            /** Format: date-time */
            created_at: string;
        };
        FilerHealth: {
            filer_id: components["schemas"]["Id"];
            /** Format: date-time */
            checked_at: string | null;
            ok: boolean | null;
            read_only?: boolean | null;
            readable?: boolean | null;
            sec?: string | null;
            error?: string | null;
            annexes: number;
        };
        AnnexSettings: {
            quiet_minutes?: number;
            quick_scan_minutes?: number;
            full_scan_minutes?: number;
            reconcile_days?: number;
        };
        AnnexDefaults: {
            quiet_minutes: number;
            quick_scan_minutes: number;
            full_scan_minutes: number;
            reconcile_days: number;
            /** @description Settings only. "Pause all removals" for every annex. */
            removals_paused?: boolean;
            /** @description Settings only. "Pause all placements" for every annex. */
            placements_paused?: boolean;
            /** @description Settings only. */
            removal_rate_per_hour?: number;
            /** @description Settings only. */
            grace_minutes?: number;
            /** @description Settings only. A removing rule set's dry run. */
            dry_run_days?: number;
            /** Format: date-time */
            updated_at?: string;
            updated_by?: string | null;
        };
        AnnexCreate: {
            name: string;
            filer_id: components["schemas"]["Id"];
            /** @description Under the filer's export; '' = all of it. */
            path?: string;
            /**
             * @description Copy runs at once. A removing mode copies at once under the Copy template and proposes its own rule set, which an ArchiveAdmin approves (and which starts in dry run). Custom takes `rules`.
             * @enum {string}
             */
            mode?: "copy" | "archive_after" | "move_now" | "custom";
            /** @description mode archive_after only. */
            archive_after_days?: number;
            rules?: components["schemas"]["AnnexRuleBody"];
            rationale?: string;
            dataset_id: components["schemas"]["Id"];
            dataset_path?: string;
            owners_sid: components["schemas"]["Sid"];
            /** @description Apps only. The person who owns the annex, in its Owners group. */
            owner?: components["schemas"]["Sid"];
            settings?: components["schemas"]["AnnexSettings"];
        };
        AnnexPatch: {
            /** @enum {string} */
            state?: "active" | "paused" | "retired";
            settings?: components["schemas"]["AnnexSettings"];
            deleted_retention_days?: number | null;
            /** @description Owners may pause; resuming is an ArchiveAdmin's, with a recent sign-in. */
            removals_paused?: boolean;
            /** @description The placements' kill switch: an ArchiveAdmin's, either way, audited. Nothing is placed by this annex or on its filer while it is on. */
            placements_paused?: boolean;
            pointer_files?: boolean;
            /** @description ArchiveAdmin. */
            grace_minutes?: number;
            /** @description ArchiveAdmin. */
            removal_rate_per_hour?: number;
            /** @description ArchiveAdmin. Removals only between these times (or null, any time). */
            removal_window?: null | {
                start: string;
                end: string;
                tz?: string;
            };
        };
        AnnexSettingsPut: {
            quiet_minutes?: number;
            quick_scan_minutes?: number;
            full_scan_minutes?: number;
            reconcile_days?: number;
            /** @description Resuming needs a recent sign-in. */
            removals_paused?: boolean;
            /** @description Nothing is placed while it is on. */
            placements_paused?: boolean;
            removal_rate_per_hour?: number;
            grace_minutes?: number;
            dry_run_days?: number;
        };
        AnnexRemoval: {
            removal_id: components["schemas"]["Id"];
            file_id: components["schemas"]["Id"];
            version_id: components["schemas"]["Id"];
            path: string;
            size?: number | null;
            rule?: string;
            ruleset_version?: number;
            state: string;
            /** Format: date-time */
            issued_at?: string;
            /** Format: date-time */
            hidden_at?: string | null;
            /** Format: date-time */
            deleted_at?: string | null;
            /** Format: date-time */
            done_at?: string | null;
            pointer_written?: boolean | null;
            cancel_reason?: string | null;
        };
        /** @description A rule set (annex.md section 4.1): `rules`, each `{id?, label?, trigger, conditions, actions}`. Triggers: arrived, changed, age, deleted_at_source. Conditions: path, extension, size_gte, size_lt, age_since_arrival_days_gt, modified_age_days_gt, accessed_age_days_gt, owner_sids, group_sids, content_type. Actions (one key each): exclude, copy_to, remove_from_source, remove_after_days, policy, tag, keep_copy. Every list is open. */
        AnnexRuleBody: {
            rules: ({
                id?: string;
                label?: string;
                trigger: string;
                conditions?: {
                    [key: string]: unknown;
                };
                actions?: {
                    [key: string]: unknown;
                }[];
            } & {
                [key: string]: unknown;
            })[];
        } & {
            [key: string]: unknown;
        };
        AnnexRuleCheck: {
            rule: string;
            check: string;
            message: string;
        };
        AnnexRuleset: {
            version: number;
            state: string;
            mode: string;
            removes: boolean;
            /** @description Adds or widens a removal, so only an ArchiveAdmin approves it. */
            needs_archive_admin: boolean;
            /** @description A3.4 (decision 85): the return rules that may replace a file on the NAS (a NAS target in this annex with overwrite_keep_version). The approval page marks them "replaces files on the NAS". Only the file's own name in its original folder is ever replaced (decision 82). */
            replaces_on_nas?: string[];
            owner_identity_id: components["schemas"]["Id"];
            owner_name?: string | null;
            proposed_via: string;
            proposed_by_app?: string | null;
            rationale?: string | null;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            decided_at?: string | null;
            decided_by_identity_id?: components["schemas"]["Id"] | null;
            decision_note?: string | null;
            dry_run_id?: components["schemas"]["Id"] | null;
            body?: components["schemas"]["AnnexRuleBody"];
        };
        AnnexRulesetDetail: components["schemas"]["AnnexRuleset"] & {
            checks: components["schemas"]["AnnexRuleCheck"][];
            dry_run?: components["schemas"]["AnnexDryRun"] | null;
            running_version?: number | null;
            /** @description The running rule set's body, to compare a proposal with (null for itself). */
            running_body?: components["schemas"]["AnnexRuleBody"] | null;
        };
        AnnexRulesetPropose: {
            body: components["schemas"]["AnnexRuleBody"];
            rationale?: string;
            /** @description Apps only. The person who owns the proposal, in the Owners group. */
            owner?: components["schemas"]["Sid"];
        };
        AnnexDecision: {
            note?: string;
        };
        /** @description A return location (annex.md section 4.1): `{kind: nas_folder, origin: true}`, `{kind: nas_folder, path}` (this annex's folder), `{kind: nas_folder, annex_id, path}`, `{kind: nas_folder, filer_id, path}` (relative to the filer's export), or `{kind: share_folder, share_id, path}`; `keep_relative_path` (default true) keeps the file's folders below the annex. */
        ReturnLocation: {
            kind: string;
            origin?: boolean;
            annex_id?: components["schemas"]["Id"];
            filer_id?: components["schemas"]["Id"];
            share_id?: components["schemas"]["Id"];
            path?: string;
            keep_relative_path?: boolean;
        };
        /** @description A resolved target: `nas` (a folder of a filer, relative to its export) or `share` (a folder of a share), and the file's name there. */
        PlacementTarget: {
            kind: string;
            folder: string;
            name: string;
            filer_id?: components["schemas"]["Id"];
            share_id?: components["schemas"]["Id"];
            origin?: boolean;
            /** @description Where it lands, as a person types it. */
            display?: string;
        };
        ReturnTargets: {
            file_id: components["schemas"]["Id"];
            annex_id: components["schemas"]["Id"];
            nas_state: string;
            /** @description The return rule that decided; null: none. */
            rule: string | null;
            ruleset_version: number | null;
            items: {
                location: components["schemas"]["ReturnLocation"];
                default: boolean;
                on_conflict: string;
                hold_days: number;
                /** @description The requester may have it placed there. */
                allowed: boolean;
                why_not?: string | null;
                target?: components["schemas"]["PlacementTarget"] | null;
                display?: string | null;
                /** @description As far as the catalog knows; null: it can't tell. */
                name_taken?: boolean | null;
                /** @description A3.4 (decision 83): the file is still on the NAS there, and an older version of it may replace it (POST /v1/placements with that version_id). */
                replace_older?: boolean;
                /** @description As the edge last saw it, for a NAS target. */
                free_bytes?: number | null;
                /** Format: date-time */
                space_seen_at?: string | null;
            }[];
        };
        PlacementCreate: {
            file_id: components["schemas"]["Id"];
            version_id?: components["schemas"]["Id"];
            /** @description `default`, or one of the file's return targets' locations (or a folder below one). */
            place_at: "default" | components["schemas"]["ReturnLocation"];
            /** @description Never harsher than the rule's. */
            on_conflict?: string;
            /** @description When the bytes are in Archive: one of the file's recall options. */
            priority?: string;
            /** @description Why do you need it? */
            note?: string;
            requester?: components["schemas"]["Sid"];
        };
        AnnexPlacement: {
            placement_id: components["schemas"]["Id"];
            annex_id: components["schemas"]["Id"];
            file_id: components["schemas"]["Id"];
            version_id: components["schemas"]["Id"];
            recall_id?: components["schemas"]["Id"] | null;
            requester_identity_id: components["schemas"]["Id"];
            request_via: string;
            note?: string | null;
            rule: string;
            ruleset_version?: number | null;
            target: components["schemas"]["PlacementTarget"];
            display?: string | null;
            chosen?: components["schemas"]["ReturnLocation"] | null;
            on_conflict: string;
            hold_days: number;
            state: string;
            /** @description placed, restored_name, already_there, replaced (the file there was replaced; it stays a version), replaced_old_kept (the file there was kept under a -replaced- name), overwrite_refused_<why> (an overwrite that didn't happen; a -restored name instead), or replace_unconfirmed (the edge said replaced without saying the old file went: nothing is claimed, and the next scan finds what is there). */
            outcome?: string | null;
            fail_reason?: string | null;
            fail_detail?: string | null;
            final_path?: string | null;
            /** @description A3.4: a replace at the origin, and what became of the file that was there. Null when none was looked at. */
            replace?: null | {
                state: string;
                /** @description Why it didn't (or wasn't offered). */
                refused: string | null;
                /** @description The version the replaced file was; it stays in the file's history. */
                replaced_version_id: components["schemas"]["Id"] | null;
                hidden_name: string | null;
                /** @description The old file's -replaced- name. */
                kept_as: string | null;
            };
            /** Format: date-time */
            requested_at: string;
            /** Format: date-time */
            updated_at?: string | null;
            /** Format: date-time */
            done_at?: string | null;
            joined?: boolean;
        };
        AnnexReturnPreview: {
            /** @description Removed files looked at. */
            files: number;
            /** @description It stopped at 200,000 files. */
            truncated: boolean;
            placed_nowhere: {
                files?: number;
                bytes?: number;
            };
            rules: {
                id: string;
                files: number;
                bytes: number;
                /** @description Files it takes for a requester it names. */
                may_take: number;
                targets: {
                    location: components["schemas"]["ReturnLocation"];
                    files: number;
                    bytes: number;
                    names_taken: number;
                    free_bytes?: number | null;
                }[];
            }[];
            checks: components["schemas"]["AnnexRuleCheck"][];
        };
        AnnexDryRunCreate: {
            /** @description A draft rule set; omitted or null previews the running one. */
            body?: components["schemas"]["AnnexRuleBody"] | null;
        };
        AnnexDryRun: {
            run_id: components["schemas"]["Id"];
            annex_id: components["schemas"]["Id"];
            ruleset_version?: number | null;
            state: string;
            /** Format: date-time */
            requested_at: string;
            /** Format: date-time */
            done_at?: string | null;
            error?: string | null;
            /** @description The run has a report the caller may not see: counts over the NAS listing are an ArchiveAdmin's until an annex's folders carry the NAS's ACLs. */
            report_hidden?: boolean;
            checks?: components["schemas"]["AnnexRuleCheck"][];
            /** @description The edge's preview: files and bytes per rule (with up to 20 sample paths for ArchiveAdmins and apps), what would be copied, excluded or left undecided, and when files would leave the NAS (now, per day, and how many can't be decided now). ArchiveAdmins and apps only, for now. */
            report?: {
                [key: string]: unknown;
            } | null;
        };
        Annex: {
            annex_id: components["schemas"]["Id"];
            name: string;
            mode: string;
            state: string;
            filer_id: components["schemas"]["Id"];
            filer_name: string;
            /** @description Where it is, as people write it. */
            unc: string;
            path: string;
            dataset_id: components["schemas"]["Id"];
            dataset_name: string;
            dataset_path: string;
            edge_id: components["schemas"]["Id"];
            owners_sid: components["schemas"]["Sid"];
            settings: components["schemas"]["AnnexDefaults"];
            deleted_retention_days?: number | null;
            ruleset_version: number | null;
            /** @description The newest rule set's state. An app's annex is `proposed` until one of its Owners or an ArchiveAdmin approves it; the edge starts it then. */
            ruleset_state?: string | null;
            host_trusted: boolean;
            pending_proposals?: number;
            /** @description What the removal pipeline (phase A2.2) will obey. `enabled` is false until it is built: nothing is removed from any NAS before then. */
            removals?: {
                paused?: boolean;
                /** Format: date-time */
                dry_run_until?: string | null;
                rate_per_hour?: number;
                window?: {
                    [key: string]: unknown;
                } | null;
                grace_minutes?: number;
                pointer_files?: boolean;
                enabled?: boolean;
                confirmed_version?: number | null;
                /** Format: date-time */
                confirmed_at?: string | null;
            };
            baseline: {
                /** Format: date-time */
                at: string | null;
                files: number | null;
                bytes: number | null;
            };
            /** Format: date-time */
            last_quick_scan_at?: string | null;
            /** Format: date-time */
            last_full_scan_at?: string | null;
            /** Format: date-time */
            last_reconcile_at?: string | null;
            /** Format: date-time */
            last_event_at?: string | null;
            /** Format: date-time */
            created_at?: string;
            entity_version: number;
        };
        AnnexDetail: components["schemas"]["Annex"] & {
            counts: {
                present: number;
                copied: number;
                verified: number;
                pending: number;
                gone: number;
                purged: number;
                bytes: number;
                bytes_verified: number;
                problems_30d: number;
            };
            filer: components["schemas"]["Filer"];
            can_manage: boolean;
            can_purge: boolean;
        };
        AnnexEvent: {
            event_id: components["schemas"]["Id"];
            /** Format: date-time */
            at: string;
            kind: string;
            path: string | null;
            file_id: components["schemas"]["Id"] | null;
            version_id?: components["schemas"]["Id"] | null;
            size?: number | null;
            /** Format: date-time */
            observed_at?: string | null;
            /** @description On the problems list only: a `copy_refused` problem whose file has since had a later version read back and verified. Absent otherwise. */
            resolved?: boolean;
            /** @description For an event without a catalog file (skipped, drift), only `reason` or `drift` unless the caller is an ArchiveAdmin or an app; its `path` is then null too. */
            details: {
                [key: string]: unknown;
            } | null;
        };
        AnnexFile: {
            file_id: components["schemas"]["Id"];
            path: string;
            size: number;
            mtime_ns?: components["schemas"]["Nanos"] | null;
            /** @description Decision 63: the folders above the file on the NAS, relative to the export ("" is its root), that don't let everyone through or haven't been read. Who may read the file is who its own ACL lets read and these let through. */
            folders_narrow?: string[];
            nas_state: string;
            copy_state: string;
            tier?: components["schemas"]["LogicalTier"] | null;
            /** Format: date-time */
            first_seen: string;
            /** Format: date-time */
            last_seen: string;
            /** Format: date-time */
            gone_since: string | null;
            /** Format: date-time */
            hold_until?: string | null;
            /** @description Pinned by the loop breaker until an Owner clears it. */
            pinned_by_breaker?: boolean;
            baseline?: boolean;
            /** Format: date-time */
            copied_at?: string | null;
            /** Format: date-time */
            verified_at?: string | null;
            version_id?: components["schemas"]["Id"] | null;
            /** Format: date-time */
            purge_requested_at?: string | null;
            /**
             * Format: date-time
             * @description A legal hold or WORM refused the last purge. Nothing is waiting: the file can be recalled and placed, and a purge can be asked for again.
             */
            purge_refused_at?: string | null;
            /** Format: date-time */
            purged_at?: string | null;
            /** @description Azure's read-back refused this many of the file's copies in a row, one per version; 0 once a version verifies. */
            copy_refusals?: number;
            /** Format: date-time */
            copy_refused_at?: string | null;
            /** @description An ArchiveAdmin stopped copying it again (`copy-stop`), until a version verifies or they resume it. */
            copy_stopped?: components["schemas"]["AnnexFileMark"] | null;
            /** @description An ArchiveAdmin acknowledged the refusals (`copy-refused-ack`): the alert is quiet for this file until a version verifies. */
            copy_refused_ack?: components["schemas"]["AnnexFileMark"] | null;
        };
        AnnexFileMark: {
            by: string;
            /** Format: date-time */
            at: string;
        };
        AnnexTimeline: {
            annex_id: components["schemas"]["Id"];
            baseline: {
                /** Format: date-time */
                at?: string | null;
                files?: number | null;
                bytes?: number | null;
            };
            days: {
                /** Format: date */
                date: string;
                arrived: number;
                left: number;
                changed: number;
                copied: number;
                verified: number;
                bytes_copied: number;
            }[];
        };
        AnnexProblems: {
            annex_id: components["schemas"]["Id"];
            /** @description Unresolved drift by kind. */
            drift_counts: {
                [key: string]: number;
            };
            /** @description Each drift names a NAS path: ArchiveAdmins and apps get the rows, others an empty list (drift_counts says how many). */
            drift: {
                drift_id: components["schemas"]["Id"];
                kind: string;
                path: string;
                expected?: {
                    [key: string]: unknown;
                } | null;
                observed?: {
                    [key: string]: unknown;
                } | null;
                /** Format: date-time */
                detected_at: string;
                /** Format: date-time */
                last_seen_at?: string;
                resolved: boolean;
            }[];
            events: components["schemas"]["AnnexEvent"][];
            /** @description Files still on the NAS whose copy Azure's read-back refuses, however long ago, stopped ones first: a stopped file stays here until a version verifies or it is resumed. ArchiveAdmins and apps see every one; others the files they may read. At most 200 are listed: `copies_refused_total` says how many there are. `at_bound`: refused `MAX_REFUSALS` (3) times in a row, so the service no longer asks for it. `alerting`: the alert is up for it (at the bound, or stopped whatever its count, and not acknowledged). */
            copies_refused?: {
                file_id: components["schemas"]["Id"];
                path: string;
                at_bound: boolean;
                alerting: boolean;
                copy_refusals: number;
                /** Format: date-time */
                copy_refused_at: string | null;
                copy_stopped: components["schemas"]["AnnexFileMark"] | null;
                copy_refused_ack: components["schemas"]["AnnexFileMark"] | null;
            }[];
            /** @description How many refused copies there are, of which `copies_refused` lists 200. */
            copies_refused_total?: number;
            /** @description Decision 63: an annexed file is hidden from everyone until its edge has read every folder above it, up to the export's root. `ready`: a folder pass has done so (`read_at`); before the first, every file is hidden. `folders`: the folders read so far, and `unreadable` those whose ACL can't be read, which hide what is below them. */
            read_set?: {
                ready: boolean;
                /** @description Decision 65: the filer's question (does its share or export limit access?) has been answered. Until it is, every file is hidden. */
                filer_answered: boolean;
                /** Format: date-time */
                read_at: string | null;
                folders: number;
                unreadable: number;
            };
        };
        /** @description 64-bit integer as a decimal string. */
        Id: string;
        /** Format: uuid */
        Uuid: string;
        Sid: string;
        /** @description Nanoseconds since the Unix epoch, as a decimal string. */
        Nanos: string;
        /** @description Opaque; null at the end. Valid for 1 hour. */
        NextCursor: string | null;
        Money: {
            /** @description Decimal; the low end of a range. */
            amount: string;
            /** @description Present for a range. */
            high?: string;
            currency: string;
        };
        /** @description Response-side, open (portal-api.md 1). Requests use PinTier. */
        LogicalTier: string;
        /** @enum {string} */
        PinTier: "hot" | "warm" | "cold" | "archive";
        /**
         * @description `superseded`: the file was replaced or deleted while it was being recalled, so the
         *     recall stopped (`Recall.superseded` says which). Not a failure.
         * @enum {string}
         */
        RecallState: "requested" | "rehydrating" | "copying" | "downloading" | "available" | "failed" | "cancelled" | "superseded";
        /** @enum {string} */
        ProposalState: "pending" | "approved" | "rejected" | "withdrawn" | "superseded";
        /** @enum {string} */
        Role: "ArchiveAdmin" | "DataSteward" | "User";
        Check: {
            /** @enum {string} */
            severity: "error" | "warning" | "info";
            /** @description See portal-api.md section 8. */
            code: string;
            message: string;
            /** @description Body field it concerns, e.g. cold_day or notify.owners. */
            field?: string | null;
            share_id?: components["schemas"]["Id"] | null;
            fee?: components["schemas"]["Money"] | null;
        };
        /**
         * @example {
         *       "sid": "S-1-5-21-1000-2000-3000-1102",
         *       "display_name": "Data readers",
         *       "sam_account_name": "data-readers"
         *     }
         */
        GroupRef: {
            sid: components["schemas"]["Sid"];
            display_name?: string | null;
            sam_account_name?: string | null;
        };
        /**
         * @example {
         *       "kind": "identity",
         *       "display_name": "Ada Admin",
         *       "app_id": null,
         *       "identity_id": "11"
         *     }
         */
        PersonRef: {
            /** @enum {string} */
            kind: "identity" | "application" | "system";
            display_name?: string | null;
            app_id?: string | null;
            /** @description kind identity: compare with Me.identity_id to recognise your own rows. The server still decides. */
            identity_id?: components["schemas"]["Id"] | null;
        };
        /**
         * @example {
         *       "object_id": "33333333-3333-4333-8333-333333333333",
         *       "identity_id": "17",
         *       "display_name": "Rita Reader",
         *       "upn": "rita@contoso.com",
         *       "sid": "S-1-5-21-1000-2000-3000-1003",
         *       "roles": [
         *         "User"
         *       ],
         *       "groups_source": "token",
         *       "groups_resolved_at": "2026-10-06T09:30:00.000Z",
         *       "owner_of": [],
         *       "writer_of": [],
         *       "reader_of": [
         *         "4"
         *       ]
         *     }
         */
        Me: {
            object_id: components["schemas"]["Uuid"];
            /** @description The catalog identity; null for a cloud-only user (no on-premises SID). */
            identity_id?: components["schemas"]["Id"] | null;
            display_name?: string | null;
            upn?: string | null;
            sid?: components["schemas"]["Sid"] | null;
            roles: components["schemas"]["Role"][];
            /**
             * @description pending: over 200 groups; ACL-checked routes answer 503 groups_pending until the worker has resolved them.
             * @enum {string}
             */
            groups_source: "token" | "graph" | "winbind" | "pending" | "none";
            /** Format: date-time */
            groups_resolved_at?: string | null;
            /** @description Share ids. */
            owner_of: components["schemas"]["Id"][];
            writer_of: components["schemas"]["Id"][];
            reader_of: components["schemas"]["Id"][];
            /** @description False when an ArchiveAdmin hid prices from all of this user's roles (section 5.24): every amount in this user's answers is then null, and the portal shows no costs. */
            prices_visible?: boolean;
        };
        /**
         * @example {
         *       "authenticated": true,
         *       "user": {
         *         "object_id": "33333333-3333-4333-8333-333333333333",
         *         "identity_id": "17",
         *         "display_name": "Rita Reader",
         *         "upn": "rita@contoso.com",
         *         "sid": "S-1-5-21-1000-2000-3000-1003",
         *         "roles": [
         *           "User"
         *         ],
         *         "groups_source": "token",
         *         "groups_resolved_at": "2026-10-06T09:30:00.000Z",
         *         "owner_of": [],
         *         "writer_of": [],
         *         "reader_of": [
         *           "4"
         *         ]
         *       },
         *       "csrf_token": "q8Jx0wJ3c1m2wq8Jx0wJ3c1m2wq8Jx0wJ3c1m2wq8J",
         *       "expires_at": "2026-10-06T19:30:00.000Z",
         *       "idle_expires_at": "2026-10-06T10:30:00.000Z",
         *       "auth_time": "2026-10-06T09:30:00.000Z"
         *     }
         */
        Session: {
            /** @constant */
            authenticated: true;
            user: components["schemas"]["Me"];
            csrf_token: string;
            /** Format: date-time */
            expires_at: string;
            /** Format: date-time */
            idle_expires_at: string;
            /** Format: date-time */
            auth_time: string;
        };
        WormState: {
            enabled: boolean;
            /** @enum {string|null} */
            mode?: "unlocked_test" | "locked" | null;
            retention_days?: number | null;
            /** Format: date-time */
            enabled_at?: string | null;
            /**
             * Format: date-time
             * @description When the policy locks; shown in the portal.
             */
            lock_deadline?: string | null;
            /** Format: date-time */
            locked_at?: string | null;
            /**
             * @description The backend container policy as last synced.
             * @enum {string}
             */
            policy_state?: "none" | "unlocked" | "locked";
        };
        /**
         * @example {
         *       "share_id": "4",
         *       "name": "long-term-storage",
         *       "dataset_id": "2",
         *       "dataset_name": "seismic",
         *       "edge_id": "1",
         *       "root_dir_id": "100",
         *       "protocols": [
         *         "nfs4",
         *         "smb"
         *       ],
         *       "nfs_sec": [
         *         "krb5p"
         *       ],
         *       "nfs_clients": [],
         *       "root_squash": true,
         *       "trust_level": "proven",
         *       "host_trust_override": false,
         *       "groups": {
         *         "owners": {
         *           "sid": "S-1-5-21-1000-2000-3000-1100",
         *           "display_name": "Geo data owners",
         *           "sam_account_name": "geo-owners"
         *         },
         *         "writers": null,
         *         "readers": {
         *           "sid": "S-1-5-21-1000-2000-3000-1102",
         *           "display_name": "Data readers",
         *           "sam_account_name": "data-readers"
         *         }
         *       },
         *       "on_open": "auto",
         *       "wait_threshold_minutes": 30,
         *       "quiet_s": null,
         *       "quiet_default_s": 10,
         *       "quiet_supported": true,
         *       "edge_name": "edge-houston-1",
         *       "rewind": {
         *         "depth_days": 30,
         *         "max_days": 90,
         *         "labels": {
         *           "daily_at_utc": "00:00",
         *           "hourly_for_hours": 24
         *         }
         *       },
         *       "worm": {
         *         "enabled": false,
         *         "mode": null,
         *         "retention_days": null,
         *         "enabled_at": null,
         *         "lock_deadline": null,
         *         "locked_at": null,
         *         "policy_state": "none"
         *       },
         *       "last_full_acl_scan_at": "2026-10-06T09:30:00.000Z",
         *       "my_access": {
         *         "owner": false,
         *         "writer": false,
         *         "reader": true,
         *         "can_read_root": true
         *       },
         *       "checks": [
         *         {
         *           "severity": "info",
         *           "code": "kerberos_throughput",
         *           "message": "krb5p costs about 77% of NFS IOPS.",
         *           "field": "nfs_sec",
         *           "share_id": "4",
         *           "fee": null
         *         }
         *       ],
         *       "etag": "\"7\""
         *     }
         */
        Share: {
            share_id: components["schemas"]["Id"];
            name: string;
            dataset_id: components["schemas"]["Id"];
            dataset_name?: string;
            edge_id?: components["schemas"]["Id"];
            root_dir_id: components["schemas"]["Id"];
            /** @description The folder the share serves, from the dataset's root: `/` for a root share, e.g. `/projects/2026` for a folder share. M6. */
            path?: string;
            protocols: ("nfs3" | "nfs4" | "smb")[];
            nfs_sec: ("sys" | "krb5" | "krb5i" | "krb5p")[];
            /** @description ArchiveAdmin only; empty for others. */
            nfs_clients?: string[];
            root_squash?: boolean;
            /** @enum {string} */
            trust_level: "proven" | "host_trusted";
            host_trust_override?: boolean;
            /** @description Ganesha's Export_Id on the edge; fixed for the share's life. M6. */
            nfs_export_id?: number;
            /** @description SMB3 encryption required. M6. */
            smb_encrypt?: boolean;
            groups: {
                owners: components["schemas"]["GroupRef"];
                writers?: components["schemas"]["GroupRef"] | null;
                readers?: components["schemas"]["GroupRef"] | null;
            };
            /** @enum {string} */
            on_open: "wait" | "fail" | "auto";
            wait_threshold_minutes?: number | null;
            /** @description How long a file must sit untouched before it counts as finished, in seconds; null for the share's default (decision 67). */
            quiet_s: number | null;
            /** @description The default a share with no quiet_s takes, as its edge last reported it in the heartbeat; until then 60 on a WORM share and 10 on any other. */
            quiet_default_s: number;
            /** @description The share's edge sets a quiet period per share: it has reported its defaults in the heartbeat. False for an edge that predates decision 67 or hasn't sent a heartbeat since. It doesn't say the edge has this share's latest value yet; that comes through the feed within seconds. */
            quiet_supported: boolean;
            /** @description The share's edge, by name. */
            edge_name?: string | null;
            rewind: {
                depth_days: number;
                max_days: number;
                labels?: {
                    [key: string]: unknown;
                };
            };
            worm: components["schemas"]["WormState"];
            /** Format: date-time */
            last_full_acl_scan_at?: string | null;
            my_access: {
                owner: boolean;
                writer: boolean;
                reader: boolean;
                can_read_root: boolean;
            };
            checks: components["schemas"]["Check"][];
            etag: string;
        };
        ShareSettingsPatch: {
            /** @enum {string} */
            on_open?: "wait" | "fail" | "auto";
            wait_threshold_minutes?: number | null;
            /** @description Seconds, or null for the share's default. Owners or ArchiveAdmin (decision 67). */
            quiet_s?: number | null;
            /** @description Required as true to set a WORM share's quiet_s below 60 s: a paused copy can leave a half-written version kept until retention ends. The audit records it. */
            quiet_s_confirmed?: boolean;
            rewind_depth_days?: number;
            rewind_depth_max_days?: number;
            nfs_sec?: ("sys" | "krb5" | "krb5i" | "krb5p")[];
            nfs_clients?: string[];
            root_squash?: boolean;
            host_trust_override?: {
                enabled: boolean;
                reason: string;
            };
        };
        ShareAccess: {
            owners_group_sid: components["schemas"]["Sid"];
            writers_group_sid?: components["schemas"]["Sid"] | null;
            readers_group_sid?: components["schemas"]["Sid"] | null;
        };
        WormEnable: {
            retention_days: number;
            /** @description Must equal the share name exactly. */
            confirm_name: string;
            /** @default 24 */
            window_hours?: number;
            /**
             * @description Required as true when the share's own quiet period is below 60 s: as a WORM share, a paused copy could leave a half-written version kept until retention ends. The audit records it (decision 67).
             * @default false
             */
            quiet_s_confirmed?: boolean;
        };
        TierState: {
            logical: components["schemas"]["LogicalTier"] | null;
            /** @description Hot, Cool, Cold, Archive, Smart; STANDARD, GLACIER, ... */
            native_class?: string | null;
            /** @description A read needs a restore. */
            archived: boolean;
            restore_state?: string | null;
            /** Format: date-time */
            restore_expires_at?: string | null;
            /** @description The blob has left Smart for good (decision 29). */
            explicit_tier_set?: boolean;
            /** Format: date-time */
            tier_changed_at?: string | null;
        };
        PinSummary: {
            pin_id: components["schemas"]["Id"];
            tier: components["schemas"]["PinTier"];
            /** Format: date-time */
            until?: string | null;
            override_archive?: boolean;
            /** @description The folder the pin is on */
            inherited_from?: components["schemas"]["Id"] | null;
        };
        Holds: {
            /**
             * Format: date-time
             * @description Latest active keep_online hold; "null" with keep_online true means until released.
             */
            keep_online_until?: string | null;
            keep_online?: boolean;
            legal: boolean;
            /** @description The active holds that apply, on the item or a folder above it. */
            items?: {
                hold_id: components["schemas"]["Id"];
                /** @enum {string} */
                kind: "keep_online" | "legal";
                /** Format: date-time */
                until?: string | null;
                inherited_from?: components["schemas"]["Id"] | null;
                set_by_identity_id?: components["schemas"]["Id"] | null;
            }[];
        };
        ActiveRecall: {
            recall_id: components["schemas"]["Id"];
            state: components["schemas"]["RecallState"];
            /** Format: date-time */
            eta?: string | null;
        };
        /**
         * @example {
         *       "file_id": "1042",
         *       "name": "line_1042.segy",
         *       "kind": "file",
         *       "symlink_target": null,
         *       "size": 197568495616,
         *       "mtime": "2026-02-11T14:03:22.000Z",
         *       "mtime_ns": "1770818602000000000",
         *       "state": "stub",
         *       "resident_bytes": 4194304,
         *       "version_id": "5501",
         *       "tier": {
         *         "logical": "archive",
         *         "native_class": "Archive",
         *         "archived": true,
         *         "restore_state": null,
         *         "restore_expires_at": null,
         *         "explicit_tier_set": true,
         *         "tier_changed_at": "2026-03-01T00:00:00.000Z"
         *       },
         *       "archive_at": "2026-03-01",
         *       "next_action": null,
         *       "next_action_at": null,
         *       "pin": null,
         *       "holds": {
         *         "keep_online": false,
         *         "keep_online_until": null,
         *         "legal": false,
         *         "items": []
         *       },
         *       "active_recall": {
         *         "recall_id": "88",
         *         "state": "rehydrating",
         *         "eta": null
         *       }
         *     }
         */
        Entry: {
            file_id: components["schemas"]["Id"];
            name: string;
            /** @enum {string} */
            kind: "file" | "dir" | "symlink";
            symlink_target?: string | null;
            size: number;
            /** Format: date-time */
            mtime?: string | null;
            mtime_ns?: components["schemas"]["Nanos"] | null;
            state: string;
            resident_bytes?: number;
            /** @description The content version. */
            version_id?: components["schemas"]["Id"] | null;
            tier: components["schemas"]["TierState"];
            /** Format: date */
            archive_at?: string | null;
            /**
             * @description Null while `move_hold` is set.
             * @enum {string|null}
             */
            next_action?: "tier_warm" | "tier_cold" | "archive" | "stub" | "prune_versions" | null;
            /**
             * Format: date-time
             * @description Null while `move_hold` is set.
             */
            next_action_at?: string | null;
            /** @description The file's pin wants another tier, and the move is waiting. A file that wants a different tier from the files sharing its storage gets its own copy (decision 66). This one couldn't, so it stays where it is and tries again within a day. Only a move to Archive may move shared storage over a file that differs. Null otherwise. */
            move_hold?: {
                to: components["schemas"]["LogicalTier"];
                /**
                 * Format: date-time
                 * @description When the move tries again.
                 */
                until: string;
            } | null;
            /**
             * Format: date-time
             * @description The cloud copy could not be checked: its read-back found other bytes, or kept failing until the worker gave up. The file stays on the file server, it won't become a stub, and nothing tries again without an administrator. Null otherwise.
             */
            read_back_failed_at?: string | null;
            pin?: components["schemas"]["PinSummary"] | null;
            holds: components["schemas"]["Holds"];
            active_recall?: components["schemas"]["ActiveRecall"] | null;
        };
        AsOfEntry: {
            file_id: components["schemas"]["Id"];
            name: string;
            /** @enum {string} */
            kind: "file" | "dir" | "symlink";
            /** @description The version valid at as_of. */
            version_id: components["schemas"]["Id"];
            content_version_id?: components["schemas"]["Id"] | null;
            /** @enum {string} */
            version_kind?: "content" | "metadata" | "rename";
            /** Format: date-time */
            committed_at: string;
            size?: number | null;
            mtime_ns?: components["schemas"]["Nanos"] | null;
            /** @description Of that version's primary location. */
            tier?: components["schemas"]["TierState"] | null;
            /** @description The file still exists now (not tombstoned). */
            live?: boolean;
            /** @description This version is still the current one. */
            current?: boolean;
        };
        PathSegment: {
            /** @description null for an ancestor the caller can't read. */
            file_id: components["schemas"]["Id"] | null;
            name: string | null;
        };
        DirHeader: {
            file_id: components["schemas"]["Id"];
            name?: string;
            share_id: components["schemas"]["Id"];
            /** @description From the share root. */
            path: components["schemas"]["PathSegment"][];
            rewind_depth_days?: number;
        };
        /**
         * @example {
         *       "file_id": "1042",
         *       "name": "line_1042.segy",
         *       "kind": "file",
         *       "symlink_target": null,
         *       "size": 197568495616,
         *       "mtime": "2026-02-11T14:03:22.000Z",
         *       "mtime_ns": "1770818602000000000",
         *       "state": "stub",
         *       "resident_bytes": 4194304,
         *       "version_id": "5501",
         *       "tier": {
         *         "logical": "archive",
         *         "native_class": "Archive",
         *         "archived": true,
         *         "restore_state": null,
         *         "restore_expires_at": null,
         *         "explicit_tier_set": true,
         *         "tier_changed_at": "2026-03-01T00:00:00.000Z"
         *       },
         *       "archive_at": "2026-03-01",
         *       "next_action": null,
         *       "next_action_at": null,
         *       "pin": null,
         *       "holds": {
         *         "keep_online": false,
         *         "keep_online_until": null,
         *         "legal": false,
         *         "items": []
         *       },
         *       "active_recall": {
         *         "recall_id": "88",
         *         "state": "rehydrating",
         *         "eta": null
         *       },
         *       "share_id": "4",
         *       "dataset_id": "2",
         *       "path": [
         *         {
         *           "file_id": "100",
         *           "name": ""
         *         },
         *         {
         *           "file_id": null,
         *           "name": null
         *         },
         *         {
         *           "file_id": "1042",
         *           "name": "line_1042.segy"
         *         }
         *       ],
         *       "blake3": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
         *       "crc64nvme": "0123456789abcdef",
         *       "owner_sid": "S-1-5-21-1000-2000-3000-1003",
         *       "uid": 10003,
         *       "gid": 10102,
         *       "mode": "0640",
         *       "written_at": "2026-02-11T14:05:00.000Z",
         *       "last_read_at": null,
         *       "retention_until": null,
         *       "legal_hold": false,
         *       "versions_count": 2,
         *       "rewind_depth_days": 30
         *     }
         */
        FileDetail: components["schemas"]["Entry"] & {
            share_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            path: components["schemas"]["PathSegment"][];
            blake3?: string | null;
            crc64nvme?: string | null;
            owner_sid?: components["schemas"]["Sid"] | null;
            uid?: number | null;
            gid?: number | null;
            mode?: string | null;
            /** Format: date-time */
            written_at?: string | null;
            /** Format: date-time */
            last_read_at?: string | null;
            /** Format: date-time */
            retention_until?: string | null;
            legal_hold?: boolean;
            versions_count?: number;
            rewind_depth_days?: number;
        };
        /**
         * @example {
         *       "version_id": "5501",
         *       "seq": 2,
         *       "kind": "content",
         *       "state": "committed",
         *       "content_version_id": "5501",
         *       "name": "line_1042.segy",
         *       "parent_id": "101",
         *       "committed_at": "2026-02-11T14:05:00.000Z",
         *       "superseded_at": null,
         *       "size": 197568495616,
         *       "mtime_ns": "1770818602000000000",
         *       "tier": {
         *         "logical": "archive",
         *         "native_class": "Archive",
         *         "archived": true,
         *         "restore_state": null,
         *         "restore_expires_at": null,
         *         "explicit_tier_set": true,
         *         "tier_changed_at": "2026-03-01T00:00:00.000Z"
         *       },
         *       "retention_until": null,
         *       "legal_hold": false,
         *       "restorable": true
         *     }
         */
        Version: {
            version_id: components["schemas"]["Id"];
            seq: number;
            /** @enum {string} */
            kind: "content" | "metadata" | "rename" | "tombstone";
            /** @enum {string} */
            state: "committed" | "pruned";
            content_version_id?: components["schemas"]["Id"] | null;
            name: string;
            parent_id?: components["schemas"]["Id"] | null;
            /** Format: date-time */
            committed_at: string;
            /** Format: date-time */
            superseded_at?: string | null;
            size?: number | null;
            mtime_ns?: components["schemas"]["Nanos"] | null;
            tier?: components["schemas"]["TierState"] | null;
            /** Format: date-time */
            retention_until?: string | null;
            legal_hold?: boolean;
            restorable?: boolean;
        };
        RecallOption: {
            priority: string;
            offered: boolean;
            /** @description Why not offered, e.g. High would not be faster (anti-pattern 5). */
            reason?: string | null;
            /**
             * Format: date-time
             * @description null above the no-promise size (10 GB on Azure).
             */
            eta?: string | null;
            /** @enum {string} */
            eta_confidence?: "provider_sla" | "measured" | "none";
            fee_estimate?: components["schemas"]["Money"] | null;
        };
        /**
         * @example {
         *       "file_id": "1042",
         *       "version_id": "5501",
         *       "size": 8000000000,
         *       "needs_restore": true,
         *       "online": false,
         *       "no_promise_above_bytes": 10000000000,
         *       "active_recall": null,
         *       "options": [
         *         {
         *           "priority": "standard",
         *           "offered": true,
         *           "reason": null,
         *           "eta": "2026-10-07T00:30:00.000Z",
         *           "eta_confidence": "provider_sla",
         *           "fee_estimate": {
         *             "amount": "0.15",
         *             "currency": "USD"
         *           }
         *         },
         *         {
         *           "priority": "high",
         *           "offered": false,
         *           "reason": "High is no faster than Standard with the account's priority backlog",
         *           "eta": null,
         *           "eta_confidence": "none",
         *           "fee_estimate": null
         *         }
         *       ]
         *     }
         */
        RecallOptions: {
            file_id: components["schemas"]["Id"];
            version_id: components["schemas"]["Id"];
            size: number;
            needs_restore: boolean;
            /** @description Readable now by range GET (no recall needed). */
            online?: boolean;
            no_promise_above_bytes?: number | null;
            active_recall?: components["schemas"]["ActiveRecall"] | null;
            options: components["schemas"]["RecallOption"][];
        };
        RecallRequest: {
            file_id: components["schemas"]["Id"];
            version_id?: components["schemas"]["Id"];
            /**
             * @description One of the `priority` values that GET /v1/files/{file_id}/recall-options
             *     lists as offered for this file (an open set, like RecallOption.priority;
             *     today standard or high). Anything else is 400 invalid_request
             *     (field priority), including an option that is listed but not offered.
             */
            priority: string;
            /** Format: date-time */
            due_by?: string;
        };
        RecallStep: {
            /** @enum {string} */
            step: "requested" | "rehydrating" | "downloading" | "available";
            /** @enum {string} */
            status: "done" | "current" | "pending" | "failed";
            /** Format: date-time */
            at?: string | null;
        };
        /**
         * @example {
         *       "recall_id": "88",
         *       "file_id": "1042",
         *       "version_id": "5501",
         *       "name": "line_1042.segy",
         *       "share_id": "4",
         *       "parent_id": "101",
         *       "state": "rehydrating",
         *       "priority": "standard",
         *       "eta": null,
         *       "eta_confidence": "none",
         *       "due_by": null,
         *       "requested_at": "2026-10-06T09:30:00.000Z",
         *       "completed_at": null,
         *       "failure": null,
         *       "steps": [
         *         {
         *           "step": "requested",
         *           "status": "done",
         *           "at": "2026-10-06T09:30:00.000Z"
         *         },
         *         {
         *           "step": "rehydrating",
         *           "status": "current",
         *           "at": "2026-10-06T09:31:02.000Z"
         *         },
         *         {
         *           "step": "downloading",
         *           "status": "pending",
         *           "at": null
         *         },
         *         {
         *           "step": "available",
         *           "status": "pending",
         *           "at": null
         *         }
         *       ],
         *       "requesters": {
         *         "count": 2,
         *         "me": true
         *       }
         *     }
         */
        Recall: {
            recall_id: components["schemas"]["Id"];
            file_id: components["schemas"]["Id"];
            version_id: components["schemas"]["Id"];
            /** @description null if the caller can no longer read the file. */
            name?: string | null;
            share_id?: components["schemas"]["Id"] | null;
            /** @description The file's folder; null if the caller can't read the file. */
            parent_id?: components["schemas"]["Id"] | null;
            state: components["schemas"]["RecallState"];
            priority: string;
            /** Format: date-time */
            eta?: string | null;
            /** @enum {string} */
            eta_confidence?: "provider_sla" | "measured" | "none";
            /** Format: date-time */
            due_by?: string | null;
            /** Format: date-time */
            requested_at: string;
            /** Format: date-time */
            completed_at?: string | null;
            /** @description Generic */
            failure?: string | null;
            /** @description Set when `state` is `superseded`, else null or absent. */
            superseded?: null | {
                reason: string;
                /** @description The reason in plain language. */
                text: string;
                /**
                 * @description Who replaced or deleted it: a display name, only for a caller who can
                 *     read the file, and only when the edge proved who it was. Else null.
                 */
                by?: string | null;
            };
            steps: components["schemas"]["RecallStep"][];
            requesters?: {
                count: number;
                me: boolean;
            };
            /** @description Only in POST /v1/recalls responses. */
            joined?: boolean;
        };
        PinPreviewRequest: {
            items: components["schemas"]["Id"][];
            tier: components["schemas"]["PinTier"];
            /** Format: date-time */
            until?: string;
            /** @default false */
            override_archive?: boolean;
        };
        /** @description PinPreviewRequest plus a reason. */
        PinRequest: {
            items: components["schemas"]["Id"][];
            tier: components["schemas"]["PinTier"];
            /** Format: date-time */
            until?: string;
            /** @default false */
            override_archive?: boolean;
            reason: string;
        };
        PinPreview: {
            items: {
                file_id: components["schemas"]["Id"];
                name?: string;
                checks: components["schemas"]["Check"][];
                fee?: components["schemas"]["Money"] | null;
                monthly_cost_change?: components["schemas"]["Money"] | null;
            }[];
            totals: {
                fee?: components["schemas"]["Money"] | null;
                monthly_cost_change?: components["schemas"]["Money"] | null;
                /** @description Files under the items (folders expanded) */
                files?: number;
            };
        };
        /**
         * @example {
         *       "pin_id": "31",
         *       "file_id": "101",
         *       "name": "survey-2026",
         *       "kind": "dir",
         *       "tier": "cold",
         *       "until": "2026-12-31T00:00:00.000Z",
         *       "override_archive": false,
         *       "reason": "Project paused",
         *       "set_by": {
         *         "kind": "identity",
         *         "display_name": "Ada Admin",
         *         "app_id": null,
         *         "identity_id": "11"
         *       },
         *       "set_at": "2026-10-06T09:30:00.000Z",
         *       "released_at": null
         *     }
         */
        Pin: {
            pin_id: components["schemas"]["Id"];
            file_id: components["schemas"]["Id"];
            name?: string;
            /** @enum {string} */
            kind?: "file" | "dir" | "symlink";
            tier: components["schemas"]["PinTier"];
            /** Format: date-time */
            until?: string | null;
            override_archive?: boolean;
            reason?: string | null;
            set_by?: components["schemas"]["PersonRef"];
            /** Format: date-time */
            set_at: string;
            /** Format: date-time */
            released_at?: string | null;
        };
        Skipped: {
            file_id: components["schemas"]["Id"];
            code: string;
            message: string;
        };
        PinCreateResult: {
            pins: components["schemas"]["Pin"][];
            skipped: components["schemas"]["Skipped"][];
        };
        HoldRequest: {
            items: components["schemas"]["Id"][];
            /** @enum {string} */
            kind: "keep_online" | "legal";
            /**
             * Format: date-time
             * @description keep_online only; absent means until released.
             */
            until?: string;
            reason: string;
        };
        /**
         * @example {
         *       "hold_id": "52",
         *       "file_id": "1042",
         *       "name": "line_1042.segy",
         *       "kind": "keep_online",
         *       "until": "2026-10-20T09:30:00.000Z",
         *       "reason": "Processing run",
         *       "set_by": {
         *         "kind": "identity",
         *         "display_name": "Ada Admin",
         *         "app_id": null,
         *         "identity_id": "11"
         *       },
         *       "set_at": "2026-10-06T09:30:00.000Z",
         *       "released_at": null
         *     }
         */
        Hold: {
            hold_id: components["schemas"]["Id"];
            file_id: components["schemas"]["Id"];
            name?: string;
            /** @enum {string} */
            kind: "keep_online" | "legal";
            /** Format: date-time */
            until?: string | null;
            reason: string;
            set_by?: components["schemas"]["PersonRef"];
            /** Format: date-time */
            set_at: string;
            /** Format: date-time */
            released_at?: string | null;
            /** @description With GET /v1/holds?file_id=: the folder above that file the hold is on (its file_id); null when the hold is on that file itself, and in every other listing. */
            inherited_from?: components["schemas"]["Id"] | null;
        };
        HoldCreateResult: {
            holds: components["schemas"]["Hold"][];
            skipped: components["schemas"]["Skipped"][];
        };
        VersionRestoreRequest: {
            /**
             * @description Decision 34. overwrite: the old bytes become a new current version (the
             *     previous current stays as an older version; allowed on WORM shares).
             *     restored: a new file "<stem>-restored<ext>" next to it.
             *     restored_timestamped: "<stem>-restored-<YYYY-MM-DD-HHMMSS>Z<ext>" (UTC).
             *     The extension starts at the last dot; a leading-dot name has none.
             *     A taken name is 409 name_taken.
             * @enum {string}
             */
            target: "overwrite" | "restored" | "restored_timestamped";
        };
        /**
         * @example {
         *       "restore_id": "9",
         *       "file_id": "1042",
         *       "version_id": "5400",
         *       "target": "restored_timestamped",
         *       "name": "line_1042-restored-2026-10-06-093000Z.segy",
         *       "state": "waiting_recall",
         *       "recall_id": "88",
         *       "result_file_id": null,
         *       "result_version_id": null,
         *       "failure": null,
         *       "requested_at": "2026-10-06T09:30:00.000Z",
         *       "updated_at": "2026-10-06T09:30:00.000Z"
         *     }
         */
        VersionRestore: {
            restore_id: components["schemas"]["Id"];
            file_id: components["schemas"]["Id"];
            version_id: components["schemas"]["Id"];
            /** @enum {string} */
            target: "overwrite" | "restored" | "restored_timestamped";
            /** @description The new file's name (restored targets); null for overwrite. */
            name?: string | null;
            /** @enum {string} */
            state: "requested" | "waiting_recall" | "copying" | "done" | "failed";
            recall_id?: components["schemas"]["Id"] | null;
            result_file_id?: components["schemas"]["Id"] | null;
            result_version_id?: components["schemas"]["Id"] | null;
            failure?: string | null;
            /** Format: date-time */
            requested_at: string;
            /** Format: date-time */
            updated_at?: string;
        };
        DownloadRequest: {
            /**
             * @default attachment
             * @enum {string}
             */
            disposition?: "attachment" | "inline";
        };
        /**
         * @example {
         *       "download_id": "12",
         *       "file_id": "1042",
         *       "version_id": "5400",
         *       "state": "ready",
         *       "url": "https://liveacct.privatelink.blob.core.windows.net/data/2/1042/5400?sv=2026-04-06&sig=REDACTED",
         *       "expires_at": "2026-10-06T09:40:00.000Z",
         *       "recall_options": null,
         *       "failure": null
         *     }
         */
        Download: {
            download_id: components["schemas"]["Id"];
            file_id: components["schemas"]["Id"];
            version_id: components["schemas"]["Id"];
            /** @enum {string} */
            state: "preparing" | "ready" | "needs_recall" | "failed" | "expired";
            /**
             * Format: uri
             * @description Read SAS on the private endpoint; set only when ready. Never logged.
             */
            url?: string | null;
            /** Format: date-time */
            expires_at?: string | null;
            recall_options?: components["schemas"]["RecallOptions"] | null;
            failure?: string | null;
        };
        NotifySettings: {
            /** @default true */
            requester?: boolean;
            /** @default false */
            owners?: boolean;
            watchers_group_sid?: components["schemas"]["Sid"] | null;
            /** @default true */
            email_fallback?: boolean;
        };
        /**
         * @description catalog.policy.body (policy/model.py) plus notify. Shape only; the
         *     rules (archive_day required, days increase, smart has no warm/cold)
         *     come back as checks from /v1/policies/preview. A body without
         *     archive_day is an M1 policy (stub_after_minutes), shown read-only.
         */
        PolicyBody: {
            /** @enum {string} */
            mode?: "scheduled" | "smart";
            /** @enum {string} */
            clock?: "last_read" | "written";
            warm_day?: number | null;
            cold_day?: number | null;
            archive_day?: number;
            /** @enum {string} */
            stub_at?: "archive" | "leaves_hot" | "never";
            resident_mb?: number | null;
            restub_after_hours?: number;
            /** @description M1 policies only. */
            stub_after_minutes?: number;
            notify?: components["schemas"]["NotifySettings"];
        };
        /**
         * @example {
         *       "policy_id": "7",
         *       "dataset_id": "2",
         *       "version": 3,
         *       "body": {
         *         "mode": "scheduled",
         *         "clock": "last_read",
         *         "warm_day": 30,
         *         "cold_day": 120,
         *         "archive_day": 365,
         *         "stub_at": "archive",
         *         "resident_mb": 4,
         *         "restub_after_hours": 24,
         *         "notify": {
         *           "requester": true,
         *           "owners": false,
         *           "watchers_group_sid": null,
         *           "email_fallback": true
         *         }
         *       },
         *       "state": "active",
         *       "proposal_id": "5",
         *       "approved_by": {
         *         "kind": "identity",
         *         "display_name": "Ada Admin",
         *         "app_id": null,
         *         "identity_id": "11"
         *       },
         *       "approved_at": "2026-10-06T09:30:00.000Z",
         *       "note": "Agreed with the survey team"
         *     }
         */
        PolicyVersion: {
            policy_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            version: number;
            body: components["schemas"]["PolicyBody"];
            /** @enum {string} */
            state: "active" | "superseded";
            proposal_id?: components["schemas"]["Id"] | null;
            approved_by: components["schemas"]["PersonRef"];
            /** Format: date-time */
            approved_at: string;
            note?: string | null;
        };
        PolicyCreate: {
            body: components["schemas"]["PolicyBody"];
            /** @description The active version this edit started from; null if none. */
            base_version: number | null;
            note?: string;
        };
        ShareOverride: {
            share_id: components["schemas"]["Id"];
            /** @enum {string} */
            on_open?: "wait" | "fail" | "auto";
            wait_threshold_minutes?: number | null;
        };
        CostAssumptions: {
            /** @default 1 */
            tb?: number;
            /** @default 1073741824 */
            mean_file_bytes?: number;
            /** @default 0 */
            recall_fraction_per_year?: number;
        };
        PolicyPreviewRequest: {
            dataset_id: components["schemas"]["Id"];
            body: components["schemas"]["PolicyBody"];
            share_overrides?: components["schemas"]["ShareOverride"][];
            assumptions?: components["schemas"]["CostAssumptions"];
        };
        TimelineSegment: {
            from_day: number;
            /** @description null: open-ended. */
            to_day?: number | null;
            logical_tier: components["schemas"]["LogicalTier"];
            native_class?: string | null;
            /** @description Which account holds it (two-account layouts). */
            account?: string | null;
            /**
             * @description On the edge.
             * @enum {string}
             */
            local: "resident" | "stub";
            note?: string | null;
        };
        CostItem: {
            item: string;
            tier?: components["schemas"]["LogicalTier"] | null;
            /** @description Null when prices are hidden from the caller (5.24). */
            amount: components["schemas"]["Money"] | null;
            note?: string | null;
        };
        RecallCost: {
            priority: string;
            /** @description Null when prices are hidden from the caller (5.24). */
            per_tb: components["schemas"]["Money"] | null;
            /** @description Provider's stated range (low, high). */
            hours?: number[];
        };
        PriceTableRef: {
            id: string;
            backend_kind: string;
            region?: string | null;
            /** Format: date */
            as_of: string;
            /** @description Some numbers are not yet checked against the provider's page. */
            verify: boolean;
            overridden: boolean;
        };
        PolicyPreview: {
            /** @description No error checks. */
            valid: boolean;
            body?: components["schemas"]["PolicyBody"];
            timeline: {
                segments: components["schemas"]["TimelineSegment"][];
                archive_day?: number | null;
                /** @description Days drawn: max(400, archive_day + 30). */
                horizon_days: number;
                /** @enum {string} */
                clock?: "last_read" | "written";
            };
            cost: {
                /** @description Null when prices are hidden from the caller (5.24). */
                per_tb_first_year: components["schemas"]["Money"] | null;
                /** @description Null when prices are hidden from the caller (5.24). */
                keep_hot_per_tb_first_year: components["schemas"]["Money"] | null;
                /** @description Null when prices are hidden from the caller (5.24). */
                saving?: components["schemas"]["Money"] | null;
                breakdown: components["schemas"]["CostItem"][];
                recall_per_tb?: components["schemas"]["RecallCost"][];
                assumptions?: components["schemas"]["CostAssumptions"];
                price_table: components["schemas"]["PriceTableRef"];
            };
            checks: components["schemas"]["Check"][];
        };
        ProposalCreate: {
            dataset_id: components["schemas"]["Id"];
            body: components["schemas"]["PolicyBody"];
            rationale: string;
            base_version?: number | null;
            /** @description What users see, per share. Applied to the shares only when an ArchiveAdmin approves (anti-pattern 7). */
            share_overrides?: components["schemas"]["ShareOverride"][];
        };
        /**
         * @example {
         *       "proposal_id": "5",
         *       "dataset_id": "2",
         *       "base_version": 2,
         *       "stale": false,
         *       "body": {
         *         "mode": "scheduled",
         *         "clock": "last_read",
         *         "warm_day": 30,
         *         "cold_day": 120,
         *         "archive_day": 180,
         *         "stub_at": "archive",
         *         "resident_mb": 4,
         *         "restub_after_hours": 24,
         *         "notify": {
         *           "requester": true,
         *           "owners": false,
         *           "watchers_group_sid": null,
         *           "email_fallback": true
         *         }
         *       },
         *       "rationale": "Nothing in this dataset was read after 90 days last year.",
         *       "proposed_by": {
         *         "kind": "application",
         *         "display_name": null,
         *         "app_id": "6f1c2d3e-4a5b-4c6d-8e7f-001122334455",
         *         "identity_id": null
         *       },
         *       "state": "pending",
         *       "decided_by": null,
         *       "decided_at": null,
         *       "decision_note": null,
         *       "created_at": "2026-10-06T09:30:00.000Z",
         *       "share_overrides": [
         *         {
         *           "share_id": "4",
         *           "on_open": "fail"
         *         }
         *       ],
         *       "api_client": {
         *         "api_client_id": "3",
         *         "display_name": "Migration planner",
         *         "app_id": "6f1c2d3e-4a5b-4c6d-8e7f-001122334455",
         *         "owner": {
         *           "kind": "identity",
         *           "display_name": "Ada Admin",
         *           "app_id": null,
         *           "identity_id": "11"
         *         }
         *       },
         *       "diff": [
         *         {
         *           "field": "archive_day",
         *           "active": 365,
         *           "proposed": 180
         *         }
         *       ]
         *     }
         */
        Proposal: {
            proposal_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            base_version?: number | null;
            /** @description The active policy changed since base_version, or stale_reason says why it can't be approved as it stands. */
            stale?: boolean;
            /** @description Pending proposals only: client_revoked, its API client was revoked (portal-api.md 5.21). */
            stale_reason?: string | null;
            body: components["schemas"]["PolicyBody"];
            rationale?: string | null;
            proposed_by: components["schemas"]["PersonRef"];
            state: components["schemas"]["ProposalState"];
            decided_by?: components["schemas"]["PersonRef"] | null;
            /** Format: date-time */
            decided_at?: string | null;
            decision_note?: string | null;
            /** Format: date-time */
            created_at: string;
            share_overrides?: components["schemas"]["ShareOverride"][];
            /** @description From an API client: which one, and its owner (portal-api.md 5.21). Null from a person, and for proposals made before clients were registered. */
            api_client?: components["schemas"]["ApiClientRef"] | null;
            /** @description GET of one proposal only. */
            preview?: components["schemas"]["PolicyPreview"];
            /** @description GET of one proposal only. Field-by-field against the active policy. */
            diff?: {
                field: string;
                active?: unknown;
                proposed?: unknown;
            }[];
        };
        /** @enum {string} */
        SourceState: "registered" | "seeding" | "seeded" | "failed";
        /** @description Where seeding reads the source's objects, by `kind` (catalog-seeding.md section 7). More kinds may follow (Azure Blob Inventory, a NAS snapshot listing). */
        SourceInventory: components["schemas"]["SourceInventoryS3Metadata"] | components["schemas"]["SourceInventoryS3Inventory"] | components["schemas"]["SourceInventoryListing"];
        /** @description The bucket's S3 Metadata tables in the account's `aws-s3` table bucket, in the backend's region: seeded from the live inventory table and kept current from the journal. Endpoints and credentials are the backend's, never the request's. */
        SourceInventoryS3Metadata: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "s3_metadata";
            table_bucket_arn: string;
            /** @description Default b_<bucket>. */
            namespace?: string;
            /** @description Default inventory. */
            table?: string;
            /** @description Default journal. */
            journal_table?: string;
        };
        /** @description A CSV S3 Inventory's manifest. */
        SourceInventoryS3Inventory: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "s3_inventory";
            bucket: string;
            manifest_key: string;
        };
        /** @description List the prefix, even if the bucket has S3 Metadata tables. */
        SourceInventoryListing: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "listing";
        };
        SourceGroups: {
            owners: components["schemas"]["Sid"];
            writers?: components["schemas"]["Sid"] | null;
            readers?: components["schemas"]["Sid"] | null;
        };
        SourceCreate: {
            dataset_id: components["schemas"]["Id"];
            /** @description A backend an ArchiveAdmin configured. */
            backend_id?: components["schemas"]["Id"];
            /** @description A bucket of that backend. */
            container?: string;
            /** @description Instead of backend_id and container (POST /v1/sources only): the bucket and its region, to find the backend an ArchiveAdmin configured for it. */
            backend?: components["schemas"]["InlineBackend"];
            /** @description '' (the whole bucket) or ending in '/'. */
            prefix?: string;
            /** @description The dataset folder the objects appear under; '' = the root. */
            path?: string;
            /** @description Absent or null - the bucket's S3 Metadata tables if its backend names them, else list the prefix. */
            inventory?: components["schemas"]["SourceInventory"] | null;
            groups: components["schemas"]["SourceGroups"];
        } & (unknown | unknown);
        /**
         * @example {
         *       "source_id": "3",
         *       "kind": "source",
         *       "dataset_id": "2",
         *       "path": "seismic/raw",
         *       "backend_id": "7",
         *       "container": "acme-seismic",
         *       "prefix": "raw/2019/",
         *       "inventory": {
         *         "kind": "s3_inventory",
         *         "bucket": "acme-inventory",
         *         "manifest_key": "acme-seismic/daily/2026-10-14T01-00Z/manifest.json"
         *       },
         *       "groups": {
         *         "owners": "S-1-5-21-10-20-30-1101",
         *         "writers": null,
         *         "readers": "S-1-5-21-10-20-30-1103"
         *       },
         *       "state": "seeding",
         *       "snapshot_at": "2026-10-14T01:00:00.000Z",
         *       "counts": {
         *         "objects_seen": 15000,
         *         "files_created": 14980,
         *         "dirs_created": 212,
         *         "already_present": 0,
         *         "skipped": 20
         *       },
         *       "last_error": null,
         *       "active_migrations": 0,
         *       "created_at": "2026-10-14T09:00:00.000Z",
         *       "updated_at": "2026-10-14T09:02:10.000Z",
         *       "seeded_at": null
         *     }
         */
        Source: {
            source_id: components["schemas"]["Id"];
            /** @description source - to migrate from another backend; adopt - already in the dataset's own backend. */
            kind: string;
            dataset_id: components["schemas"]["Id"];
            path: string;
            backend_id: components["schemas"]["Id"];
            container: string;
            prefix: string;
            inventory: components["schemas"]["SourceInventory"] | null;
            groups: components["schemas"]["SourceGroups"];
            state: components["schemas"]["SourceState"];
            /**
             * Format: date-time
             * @description The inventory's creation time
             */
            snapshot_at: string | null;
            counts: {
                objects_seen: number;
                files_created: number;
                dirs_created: number;
                already_present: number;
                /** @description Keys that are drift (unmappable */
                skipped: number;
            };
            last_error: string | null;
            /** @description Migrations of this source that aren't cancelled; 0 means one can be started from it. */
            active_migrations: number;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            updated_at: string;
            /** Format: date-time */
            seeded_at: string | null;
        };
        /**
         * @description A backend a source can come from, as the setup wizard configured it. Never its endpoints, roles or credentials.
         * @example {
         *       "backend_id": "7",
         *       "name": "aws-us-east-1",
         *       "kind": "s3",
         *       "region": "us-east-1",
         *       "private_network": true,
         *       "buckets": [
         *         "acme-seismic",
         *         "acme-wells"
         *       ],
         *       "metadata_tables": [
         *         "acme-seismic"
         *       ]
         *     }
         */
        SourceBackend: {
            backend_id: components["schemas"]["Id"];
            name: string;
            /** @description v1 migrates from S3 only (decision 23). */
            kind: string;
            region: string | null;
            private_network: boolean;
            /** @description The buckets it serves, without OmniFile's own operations buckets. Null when it lists none (any bucket). */
            buckets: string[] | null;
            /** @description Buckets with S3 Metadata tables configured; seeding reads those by default. */
            metadata_tables: string[];
        };
        /**
         * @example {
         *       "file_id": "41",
         *       "dataset_id": "2",
         *       "path": "/seismic/raw/line_1042.segy",
         *       "name": "line_1042.segy",
         *       "kind": "file",
         *       "size": 197568495616,
         *       "mtime": "1760000000000000000",
         *       "state": "remote",
         *       "tier": "archive",
         *       "stored": {
         *         "backend_id": "7",
         *         "container": "acme-seismic",
         *         "object_key": "raw/line_1042.segy",
         *         "backend_version_id": "3HL4kqtJlcpXroDTDmjVBH40Nrjfkd",
         *         "storage_class": "DEEP_ARCHIVE"
         *       },
         *       "restore": {
         *         "state": null,
         *         "expires_at": null
         *       },
         *       "archive_at": "2027-04-01",
         *       "next_action": null,
         *       "next_action_at": null,
         *       "hashes": {
         *         "blake3": null,
         *         "crc64nvme": null,
         *         "md5": null
         *       },
         *       "duplicates": {
         *         "count": 0,
         *         "file_ids": []
         *       },
         *       "holds": [],
         *       "origin": "s3_inventory",
         *       "entity_version": "3",
         *       "deleted_at": null
         *     }
         */
        PartnerFile: {
            file_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            /** @description Dataset-relative */
            path: string;
            name: string;
            /** @enum {string} */
            kind: "file" | "dir" | "symlink";
            size: number;
            mtime: components["schemas"]["Nanos"] | null;
            /** @description On the edge. */
            state: string;
            tier: components["schemas"]["LogicalTier"] | null;
            /** @description Where the current bytes are; null for folders and files not uploaded yet. */
            stored: null | {
                backend_id: components["schemas"]["Id"];
                container: string;
                object_key: string;
                backend_version_id: string | null;
                /** @description The provider's own class name. */
                storage_class: string | null;
            };
            restore: {
                state: string | null;
                /** Format: date-time */
                expires_at: string | null;
            };
            /**
             * Format: date
             * @description The archive date the policy gives it.
             */
            archive_at: string | null;
            next_action: string | null;
            /** Format: date-time */
            next_action_at: string | null;
            hashes: {
                blake3: string | null;
                crc64nvme: string | null;
                /** @description Only where the provider's ETag is one. */
                md5: string | null;
            };
            /** @description Other live files with the same BLAKE3 (whole-file dedupe); up to 10 ids. */
            duplicates: {
                count: number;
                file_ids: components["schemas"]["Id"][];
            };
            /** @description Active holds on the file or a folder above it. */
            holds: {
                hold_id: components["schemas"]["Id"];
                kind: string;
                on_file_id: components["schemas"]["Id"];
                /** Format: date-time */
                until: string | null;
                /** Format: date-time */
                set_at: string;
            }[];
            origin: string;
            entity_version: components["schemas"]["Id"];
            /**
             * Format: date-time
             * @description Set for a deleted entry (read by file_id); path is its last one.
             */
            deleted_at: string | null;
        };
        /**
         * @example {
         *       "restore_id": "9",
         *       "dataset_id": "2",
         *       "file_id": "41",
         *       "path": "/seismic/raw/line_1042.segy",
         *       "backend_id": "7",
         *       "container": "acme-seismic",
         *       "object_key": "raw/line_1042.segy",
         *       "backend_version_id": null,
         *       "mechanism": "s3_restore_object",
         *       "speed": "bulk",
         *       "state": "in_progress",
         *       "triggered_by": "user_recall",
         *       "migrate_on_complete": true,
         *       "bytes": 197568495616,
         *       "requested_at": "2026-10-14T09:00:00.000Z",
         *       "eta": "2026-10-16T09:00:00.000Z",
         *       "completed_at": null,
         *       "expires_at": null,
         *       "failure": null
         *     }
         */
        PartnerRestore: {
            restore_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            file_id: components["schemas"]["Id"];
            path: string;
            backend_id: components["schemas"]["Id"];
            container: string;
            object_key: string;
            backend_version_id: string | null;
            mechanism: string;
            speed: string;
            state: string;
            triggered_by: string;
            /** @description Decision 15. */
            migrate_on_complete: boolean;
            bytes: number | null;
            /** Format: date-time */
            requested_at: string;
            /** Format: date-time */
            eta: string | null;
            /** Format: date-time */
            completed_at: string | null;
            /**
             * Format: date-time
             * @description An S3 restored copy's expiry.
             */
            expires_at: string | null;
            failure: string | null;
        };
        /**
         * @description Catalog events are thin: read the entity (GET /v1/files, /v1/recalls/...) for its state. Lifecycle events (migration.*, recall.state, restore.state, proposal.decided) carry their facts in data.
         * @example {
         *       "cursor": "88213.40551",
         *       "kind": "file.upsert",
         *       "dataset_id": "2",
         *       "entity": "file",
         *       "entity_id": "41",
         *       "entity_version": "4",
         *       "at": "2026-10-14T09:02:10.000Z",
         *       "file": {
         *         "path": "/seismic/raw/line_1042.segy",
         *         "kind": "file",
         *         "deleted": false
         *       },
         *       "data": null
         *     }
         * @example {
         *       "cursor": "88990.41020",
         *       "kind": "migration.cut_over",
         *       "dataset_id": "2",
         *       "entity": "migration",
         *       "entity_id": "5",
         *       "entity_version": null,
         *       "at": "2026-10-15T10:02:00.000Z",
         *       "file": null,
         *       "data": {
         *         "migration_id": "5",
         *         "prefix": "raw/2019/",
         *         "from_backend_id": "7",
         *         "to_backend_id": "1",
         *         "mode": "serve",
         *         "state": "cut_over",
         *         "source_deletes_pending": 4
         *       }
         *     }
         */
        PartnerEvent: {
            cursor: string;
            /** @description Catalog events are <entity>.<op>. Lifecycle events are typed; migration.handed_off is reserved (decision 35). */
            kind: string;
            dataset_id: components["schemas"]["Id"];
            entity: string;
            entity_id: components["schemas"]["Id"];
            entity_version: components["schemas"]["Id"] | null;
            /** Format: date-time */
            at: string;
            /** @description File events only - the file's current path (its last one if deleted), so the event resolves; GET /v1/files?file_id= reads its state. */
            file: null | {
                path: string;
                /** @enum {string} */
                kind: "file" | "dir" | "symlink";
                deleted: boolean;
            };
            /** @description Lifecycle events only (null for catalog events). migration.* - migration_id, prefix, from_backend_id, to_backend_id, mode, state, and on migration.cut_over source_deletes_pending; recall.state - recall_id, file_id, version_id, state, eta, and superseded_reason (replaced or deleted) when state is superseded; restore.state - restore_id, file_id, state, triggered_by, migrate_on_complete, expires_at; proposal.decided - proposal_id, state, proposed_by_app, decided_at. */
            data: {
                [key: string]: unknown;
            } | null;
        };
        EnrolmentBundle: {
            edge_id: components["schemas"]["Id"];
            name: string;
            /** @description The one-time code the edge's setup takes (control-api.md section 2). Never stored; shown once. */
            bundle: string;
            /** Format: date-time */
            expires_at: string;
        };
        Edge: {
            edge_id: components["schemas"]["Id"];
            name: string;
            /** @description pending or active (open enum) */
            state: string;
            /** @description silent (no heartbeat for 15 minutes), attention (guard not armed, under 10% of the data volume free, or the break-glass account used and not sealed again), ok, or pending (not enrolled). Open enum. */
            status: string;
            software_version: string | null;
            kernel: string | null;
            /** @description armed, degraded or down (open enum) */
            guard_state: string | null;
            /** Format: date-time */
            last_heartbeat_at: string | null;
            /** Format: date-time */
            certificate_not_after: string | null;
            /** @description The heartbeat's gauges as last sent (control-api.md 4.7): outbox_depth, fills_running, recalls_waiting, data_free_bytes, data_capacity_bytes, fsal_connections, feed_lag_events, recovery_used (1 while the break-glass account was used and not sealed again), and others an edge may add. */
            health: {
                [key: string]: number;
            };
        };
        Activity: {
            /** Format: date-time */
            from: string;
            /** Format: date-time */
            to: string;
            dataset_id: components["schemas"]["Id"] | null;
            days: {
                /** Format: date */
                date: string;
                uploads: {
                    files: number;
                    /** Format: int64 */
                    bytes: number;
                };
                recalls: {
                    started: number;
                    available: number;
                    failed: number;
                };
            }[];
            uploads: {
                files: number;
                /** Format: int64 */
                bytes: number;
                /** @description Committed primary copies waiting for their read-back */
                unverified: number;
                /** @description Files still landing or uploading on an edge */
                landing: number;
            };
            recalls: {
                open: number;
                available: number;
                failed: number;
                /** Format: int64 */
                bytes: number;
                time_to_available_s: {
                    p50: number | null;
                    p90: number | null;
                };
            };
        };
        LiveEvent: {
            /** @description Stable: the page drops an id it already shows. */
            id: string;
            /** Format: date-time */
            at: string;
            kind: string;
            /** @description recall: the state reached; tier: moved or refused; heartbeat: the guard state; migration: started, verified, cut_over, failed or cancelled; annex: the annex event's kind (arrived, copied, verified, left...). Open. */
            state: string | null;
            edge_id: components["schemas"]["Id"] | null;
            edge_name: string | null;
            dataset_id: components["schemas"]["Id"] | null;
            share_id: components["schemas"]["Id"] | null;
            share_name: string | null;
            /** @description Null unless the caller may read the file. */
            file_id: components["schemas"]["Id"] | null;
            /** @description Null unless the caller may read the file. */
            name: string | null;
            /** @description From the share's root; an unreadable folder above shows as …. Null unless readable. */
            path: string | null;
            /** @description A file is involved that the caller may not read: no name, path or id. */
            hidden: boolean;
            /** Format: int64 */
            bytes: number | null;
            person: null | {
                identity_id: components["schemas"]["Id"];
                display_name: string | null;
            };
            /** @description Per kind: recall_id, priority, via; migration_id, mode; annex_id, annex_name; from, to; software_version, outbox_depth. */
            detail: {
                [key: string]: unknown;
            };
        };
        LiveEdge: {
            edge_id: components["schemas"]["Id"];
            name: string;
            /** @description As in Edge: ok, attention, silent, pending (open). */
            status: string;
            /** Format: date-time */
            last_heartbeat_at: string | null;
            uploads_15m: number;
            /** Format: int64 */
            bytes_15m: number;
            recalls_open: number;
            outbox_depth: number | null;
        };
        LiveActivity: {
            /**
             * Format: date-time
             * @description The next poll's `since`.
             */
            cursor: string;
            /** @description Newest first. Each source is read from `since` less 30 seconds, so a late commit still shows. */
            events: components["schemas"]["LiveEvent"][];
            counters: {
                /** @description Content versions committed by edges in the current minute. */
                uploads_per_min: number;
                uploads_15m: number;
                /**
                 * Format: int64
                 * @description Bytes uploaded plus bytes recalled (made available) in the last 15 minutes.
                 */
                bytes_moved_15m: number;
                recalls_in_flight: number;
                /** @description Enrolled edges heard from in the last 15 minutes. */
                edges_online: number;
                /** @description Enrolled edges. */
                edges_total: number;
            };
            series: {
                /** Format: date-time */
                from: string;
                step_s: number;
                /** @description Per minute */
                uploads: number[];
                bytes_moved: number[];
            };
            edges: components["schemas"]["LiveEdge"][];
            live: {
                waited: boolean;
                /** @description This replica holds as many polls as it allows: it answered at once. */
                capped: boolean;
                /** @description Seconds to wait before the next poll; never under 2. */
                next_poll_s: number;
            };
        };
        EdgeAppliance: {
            /** @description The release (VERSION) this service is; null for a build that isn't one. */
            release: string | null;
            images: {
                platform: string;
                /** @description appliance/convert.sh's name for it: omnifile-edge_<release>.<vhdx|ova|qcow2|vhd>. */
                file: string | null;
                /** @description Only when the operator set OMNIFILE_APPLIANCE_IMAGES_URL. Otherwise null: the files are in the release folder's appliance-files/. */
                url: string | null;
            }[];
            /** @description The SHA256SUMS file next to them */
            checksums: string | null;
            enrolment_configured: boolean;
            code_ttl_s: number;
        };
        /**
         * @example {
         *       "computed_at": "2026-10-15T02:00:00.000Z",
         *       "datasets": [
         *         {
         *           "dataset_id": "2",
         *           "objects": 3,
         *           "bytes": 3221225472,
         *           "stored": [
         *             {
         *               "backend_id": "1",
         *               "backend_kind": "azure_blob",
         *               "role": "primary",
         *               "tier": "hot",
         *               "storage_class": "Hot",
         *               "objects": 2,
         *               "bytes": 2147483648,
         *               "monthly_cost": {
         *                 "amount": "0.05",
         *                 "currency": "USD"
         *               }
         *             },
         *             {
         *               "backend_id": "7",
         *               "backend_kind": "s3",
         *               "role": "primary",
         *               "tier": "archive",
         *               "storage_class": "DEEP_ARCHIVE",
         *               "objects": 1,
         *               "bytes": 1073741824,
         *               "monthly_cost": null
         *             }
         *           ],
         *           "monthly_cost": [
         *             {
         *               "amount": "0.05",
         *               "currency": "USD"
         *             }
         *           ]
         *         }
         *       ],
         *       "shares": [
         *         {
         *           "share_id": "4",
         *           "dataset_id": "2",
         *           "name": "seismic",
         *           "files": 3,
         *           "bytes": 3221225472,
         *           "by_tier": [
         *             {
         *               "tier": "hot",
         *               "files": 2,
         *               "bytes": 2147483648
         *             },
         *             {
         *               "tier": "archive",
         *               "files": 1,
         *               "bytes": 1073741824
         *             }
         *           ]
         *         }
         *       ],
         *       "prices": [
         *         {
         *           "id": "azure_blob/eastus/2026-10-06",
         *           "backend_kind": "azure_blob",
         *           "region": "eastus",
         *           "as_of": "2026-10-06",
         *           "verify": true,
         *           "overridden": false
         *         }
         *       ],
         *       "unpriced": [
         *         "s3: DEEP_ARCHIVE"
         *       ]
         *     }
         */
        Usage: {
            /** Format: date-time */
            computed_at: string | null;
            datasets: {
                dataset_id: components["schemas"]["Id"];
                objects: number;
                bytes: number;
                /** @description Every committed copy (what the provider bills), older versions and recall copies included. */
                stored: {
                    backend_id: components["schemas"]["Id"];
                    backend_kind: string;
                    role: string;
                    tier: components["schemas"]["LogicalTier"] | null;
                    storage_class: string | null;
                    objects: number;
                    bytes: number;
                    /** @description Storage only; null when the price table has no gb_month for the class. */
                    monthly_cost: components["schemas"]["Money"] | null;
                }[];
                /** @description One sum per currency */
                monthly_cost: components["schemas"]["Money"][];
            }[];
            shares: {
                share_id: components["schemas"]["Id"];
                dataset_id: components["schemas"]["Id"];
                name: string;
                files: number;
                /** @description Current bytes of the live files under the share (its deepest covering share). */
                bytes: number;
                by_tier: {
                    /** @description null - not uploaded yet. */
                    tier: components["schemas"]["LogicalTier"] | null;
                    files: number;
                    bytes: number;
                }[];
            }[];
            prices: components["schemas"]["PriceTableRef"][];
            /** @description Classes the price tables don't price; an ArchiveAdmin can set them (5.8). */
            unpriced: string[];
        };
        LocationCreate: {
            dataset_id: components["schemas"]["Id"];
            /** @description An S3 key prefix inside one of the dataset's sources: '' or ending in '/'. */
            prefix: string;
            /** @description The dataset's own backend (v1 moves S3 to Azure, decision 23). */
            to_backend_id: components["schemas"]["Id"];
            /**
             * @description hand_off is reserved (decision 35) and refused.
             * @default serve
             * @enum {string}
             */
            mode?: "serve" | "hand_off";
            /**
             * @description omnifile - the service runs Storage Mover; external - Prizmanyx copies and the service only verifies and cuts over.
             * @default omnifile
             * @enum {string}
             */
            copy?: "omnifile" | "external";
        };
        /**
         * @example {
         *       "migration_id": "5",
         *       "dataset_id": "2",
         *       "source_id": "3",
         *       "prefix": "raw/2019/",
         *       "to_backend_id": "1",
         *       "to_container": "live/data",
         *       "target_root": "migrated/acme-seismic/",
         *       "mode": "serve",
         *       "copy": "omnifile",
         *       "state": "verifying",
         *       "run": {
         *         "job": "omnifile-migration-5",
         *         "run_id": "run-17",
         *         "state": "succeeded",
         *         "counters": {
         *           "scanned": 15000,
         *           "transferred": 14200,
         *           "failed": 0,
         *           "unsupported": 800,
         *           "no_transfer_needed": 0
         *         },
         *         "started_at": "2026-10-15T08:00:00.000Z"
         *       },
         *       "files": {
         *         "total": 15000,
         *         "verified": 14100,
         *         "failed": 2
         *       },
         *       "journal_checked_through": "2026-10-15T09:40:00.000Z",
         *       "verified_at": null,
         *       "cut_over_at": null,
         *       "last_error": null,
         *       "requested_by": "app:prizmanyx-app",
         *       "created_at": "2026-10-15T07:59:00.000Z",
         *       "updated_at": "2026-10-15T09:41:00.000Z"
         *     }
         */
        Location: {
            migration_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            source_id: components["schemas"]["Id"];
            prefix: string;
            to_backend_id: components["schemas"]["Id"];
            to_container: string;
            /** @description Copies land at target_root + the S3 key. */
            target_root: string;
            mode: string;
            copy: string;
            state: string;
            /** @description The latest Storage Mover run (null for external copies). */
            run: null | {
                job: string | null;
                run_id: string;
                state: string | null;
                counters: {
                    [key: string]: number;
                };
                /** Format: date-time */
                started_at: string | null;
            };
            files: {
                total: number | null;
                verified: number;
                /** @description Copies that failed their CRC64-NVME check (removed and copied again). */
                failed: number;
            };
            /** Format: date-time */
            journal_checked_through: string | null;
            /** Format: date-time */
            verified_at: string | null;
            /** Format: date-time */
            cut_over_at: string | null;
            last_error: string | null;
            requested_by: string;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            updated_at: string;
        };
        MigrationCreate: {
            dataset_id: components["schemas"]["Id"];
            /** @description An S3 key prefix inside one of the dataset's seeded sources: '' or ending in '/'. */
            prefix: string;
            /** @description Default and only value in v1: the dataset's own backend (decision 23). */
            to_backend_id?: components["schemas"]["Id"];
            /**
             * @description hand_off is reserved (decision 35) and refused.
             * @default serve
             * @enum {string}
             */
            mode?: "serve" | "hand_off";
            /**
             * @description auto - flip once every file is verified; manual - wait at verified for POST .../cutover.
             * @default auto
             * @enum {string}
             */
            cutover?: "auto" | "manual";
            /**
             * @description auto - the service runs Storage Mover; external - Prizmanyx copies to target_root + the S3 key and reports its runs.
             * @default auto
             * @enum {string}
             */
            copier?: "auto" | "external";
        };
        /**
         * @example {
         *       "migration_id": "5",
         *       "dataset_id": "2",
         *       "source_id": "3",
         *       "prefix": "raw/2019/",
         *       "from_backend_id": "7",
         *       "to_backend_id": "1",
         *       "to_container": "live/data",
         *       "target_root": "migrated/acme-seismic/",
         *       "mode": "serve",
         *       "cutover": "manual",
         *       "copier_kind": "external",
         *       "state": "verified",
         *       "files": {
         *         "total": 15000,
         *         "verified": 14998,
         *         "failed": 2,
         *         "excluded": 2
         *       },
         *       "copier": {
         *         "job_ref": null,
         *         "last_run": {
         *           "run_ref": "pz-run-17",
         *           "state": "succeeded",
         *           "counters": {
         *             "copied": 15000
         *           },
         *           "started_at": "2026-10-15T08:00:00.000Z"
         *         },
         *         "runs_reported": 3
         *       },
         *       "journal_checked_through": "2026-10-15T09:40:00.000Z",
         *       "verified_at": "2026-10-15T09:41:00.000Z",
         *       "cutover_requested_at": null,
         *       "cut_over_at": null,
         *       "cancelled_at": null,
         *       "source_deletes_pending": 4,
         *       "last_error": null,
         *       "created_by": "app:prizmanyx-app",
         *       "created_at": "2026-10-15T07:59:00.000Z",
         *       "updated_at": "2026-10-15T09:41:00.000Z"
         *     }
         */
        Migration: {
            migration_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            source_id: components["schemas"]["Id"];
            prefix: string;
            from_backend_id: components["schemas"]["Id"];
            to_backend_id: components["schemas"]["Id"];
            to_container: string;
            /** @description Copies land at target_root + the S3 key. */
            target_root: string;
            mode: string;
            cutover: string;
            /** @description Reserved for later: datasync, netapp_copy_sync, qumulo_shift, snapmirror (decision 35). */
            copier_kind: string;
            state: string;
            files: {
                /** @description Live files under the prefix */
                total: number | null;
                verified: number;
                /** @description Copies that failed their CRC64-NVME check (removed and copied again). */
                failed: number;
                /** @description Objects an ArchiveAdmin excluded; they stay on the source. */
                excluded: number;
            };
            /** @description Copies whose check won't end by itself, as the worker's sweep last counted them. Numbers only, and given_up is at most 5 minutes old; the other two are looked at when a prefix stalls, after an Exclude, and at least hourly otherwise. A new run revives none of them. Which objects they are is on /v1/migrations/{migration_id}/stuck-copies. */
            stuck_copies?: {
                /** @description Their check kept failing and went to the dead letters. An administrator sends them again. */
                given_up: number;
                /** @description Nothing is queued to check them */
                stranded: number;
                /** @description Their S3 objects have no checksum on record and nothing is computing one. Exclude them */
                no_checksum: number;
            };
            copier: {
                /** @description The Storage Mover job (null for an external copier). */
                job_ref: string | null;
                last_run: null | {
                    run_ref: string;
                    state: string | null;
                    counters: {
                        [key: string]: number;
                    };
                    /** Format: date-time */
                    started_at: string | null;
                };
                /** @description Runs an external copier reported. */
                runs_reported: number;
            };
            /** Format: date-time */
            journal_checked_through: string | null;
            /** Format: date-time */
            verified_at: string | null;
            /** Format: date-time */
            cutover_requested_at: string | null;
            /** Format: date-time */
            cut_over_at: string | null;
            /** Format: date-time */
            cancelled_at: string | null;
            /** @description Rows of the cutover report (GET .../tombstones). */
            source_deletes_pending: number;
            last_error: string | null;
            /** @description The staged plan's wave that owns it (portal-api.md 5.22): its cutover, cancel and retry go through the plan's routes (409 otherwise). */
            plan?: null | {
                plan_id: components["schemas"]["Id"];
                seq: number;
            };
            /** @description The audit actor that started it: app:<client id> or user:<identity id>. */
            created_by: string;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            updated_at: string;
        };
        MigrationRunReport: {
            run_ref: string;
            /** @enum {string} */
            state: "running" | "succeeded" | "failed";
            /** Format: date-time */
            started_at: string;
            /** Format: date-time */
            finished_at?: string | null;
            counters?: {
                [key: string]: number;
            };
        };
        MigrationExclude: {
            object_keys: string[];
            reason: string;
        };
        Tombstone: {
            file_id: components["schemas"]["Id"];
            /** @description The file's last path in the dataset. */
            path: string;
            source: {
                backend_id: components["schemas"]["Id"];
                container: string;
                key: string;
                backend_version_id: string | null;
                etag: string | null;
                /** @description True for an unversioned object: a delete by key could remove newer bytes, so check the ETag first (DeleteObject with If-Match). */
                verify_etag_before_delete: boolean;
            };
            /** Format: date-time */
            deleted_at: string;
            deleted_by: {
                kind: string;
                edge_id: components["schemas"]["Id"] | null;
                /** @description The catalog identity of the user the edge reported making the delete (control-api.md section 4.1 actor). Null when the edge could not tell who (its reconcile pass found the delete, or it sent no actor), or the user is not a catalog identity. */
                identity_id: components["schemas"]["Id"] | null;
                /** @description How sure the service is of who: proven (Kerberos NFS on a Kerberos-only share, or SMB on a proven share), claimed (NFS sec=sys, a local process), or unknown. */
                proof: string;
            };
        };
        /**
         * @example {
         *       "migration_id": "5",
         *       "state": "cut_over",
         *       "cut_over_at": "2026-10-15T10:02:00.000Z",
         *       "source_deletes_pending": 1,
         *       "applied_by_omnifile": false,
         *       "items": [
         *         {
         *           "file_id": "41",
         *           "path": "/seismic/raw/2019/line_1042.segy",
         *           "source": {
         *             "backend_id": "7",
         *             "container": "acme-seismic",
         *             "key": "raw/2019/line_1042.segy",
         *             "backend_version_id": null,
         *             "etag": "\"9b2cf535f27731c974343645a3985328\"",
         *             "verify_etag_before_delete": true
         *           },
         *           "deleted_at": "2026-10-15T09:12:00.000Z",
         *           "deleted_by": {
         *             "kind": "edge",
         *             "edge_id": "1",
         *             "identity_id": null,
         *             "proof": "unknown"
         *           }
         *         }
         *       ],
         *       "next_cursor": null
         *     }
         */
        TombstoneReport: {
            migration_id: components["schemas"]["Id"];
            state: string;
            /** Format: date-time */
            cut_over_at: string | null;
            /** @description Every row of the report (0 items are returned to an ArchiveAdmin with no AD identity). */
            source_deletes_pending: number;
            /**
             * @description OmniFile never applies these to the source (decision 40).
             * @constant
             */
            applied_by_omnifile: false;
            items: components["schemas"]["Tombstone"][];
            next_cursor: components["schemas"]["NextCursor"];
        };
        /**
         * @example {
         *       "migration_id": "7",
         *       "kind": "no_checksum",
         *       "count": 1,
         *       "counted_at": "2026-10-15T10:02:00.000Z",
         *       "list_limit": 10000,
         *       "items": [
         *         {
         *           "object_key": "raw/2019/sse_c.segy"
         *         }
         *       ],
         *       "next_cursor": null
         *     }
         */
        StuckCopies: {
            migration_id: components["schemas"]["Id"];
            /** @enum {string} */
            kind: "stranded" | "no_checksum";
            /** @description All of this kind when the worker last looked: the same number as stuck_copies, read from its count and never counted here. */
            count: number;
            /**
             * Format: date-time
             * @description When the worker last looked, at least hourly while the prefix waits on a check; null if it hasn't.
             */
            counted_at: string | null;
            /** @description The worker lists at most this many of each kind, the first by key. When count is higher, excluding them lets the next ones show at the next sweep. */
            list_limit: number;
            /** @description S3 keys, for the Exclude action. Objects excluded since the worker last looked are left out. None for an ArchiveAdmin with no AD identity. */
            items: {
                object_key: string;
            }[];
            next_cursor: components["schemas"]["NextCursor"];
        };
        /** @description One migration in the console - a Migration plus bytes, throughput, ETA, health and the next planned wave. */
        ConsoleRow: components["schemas"]["Migration"] & {
            dataset_name: string;
            /**
             * Format: date-time
             * @description When the worker last counted it (every 5 minutes); null before the first count.
             */
            counted_at: string | null;
            /** @description Copies that failed their check whose file has no verified copy yet (files.failed only ever grows). */
            failing_now: number;
            bytes: {
                /** @description Bytes of the live files under the prefix. */
                total: number;
                /** @description Of those */
                verified: number;
            };
            /** @description From the worker's samples of the last hour (at least 10 minutes apart); null when nothing was verified in them. */
            throughput: null | {
                bytes_per_s: number;
                files_per_h: number;
                basis: string;
            };
            /** @description at is null when the data can't support a time: nothing verified in the last hour, or objects still in an S3 archive class. */
            eta: {
                /** Format: date-time */
                at: string | null;
                basis: string;
            };
            health: string;
            restores_expiring: number;
            /** @description Objects still in GLACIER or DEEP_ARCHIVE with no finished restore. */
            archived_waiting: number;
            next_wave: null | {
                plan_id: components["schemas"]["Id"];
                plan_name: string;
                seq: number;
                name: string;
                /** Format: date-time */
                start_at: string;
                waiting_reason: string | null;
            };
        };
        AttentionItem: {
            kind: string;
            migration_id: components["schemas"]["Id"] | null;
            source_id?: components["schemas"]["Id"];
            agent?: string;
            message: string;
            /** @description The fixes on offer - rerun (Copy again), exclude (re-auth), restore_again, retry, settings (a Settings check), agents (the Agents view), wait. */
            actions: string[];
        };
        MigrationConsole: {
            /** Format: date-time */
            computed_at: string;
            migrations: components["schemas"]["ConsoleRow"][];
            attention: components["schemas"]["AttentionItem"][];
        };
        /** @description A Storage Mover agent as the worker last read it through ARM. */
        MoverAgent: {
            name: string;
            /** @description Storage Mover's agentStatus (Online, Offline, ...), as given. */
            status: string | null;
            version: string | null;
            host: string | null;
            arc_resource_id: string | null;
            /** Format: date-time */
            last_status_at: string | null;
            error: string | null;
            /** Format: date-time */
            polled_at: string;
            /** @description Not read for 15 minutes. */
            stale: boolean;
        };
        ConsoleEdge: null | {
            edge_id: components["schemas"]["Id"];
            name: string;
            status: string;
            software_version: string | null;
            /** Format: date-time */
            last_heartbeat_at: string | null;
            guard_state: string | null;
        };
        MigrationWork: {
            migration_id: components["schemas"]["Id"];
            copier_kind: string;
            storage_mover: null | {
                job_ref: string | null;
                last_run: Record<string, never> | null;
                agents: components["schemas"]["MoverAgent"][];
                /** @description A cloud-to-cloud job (S3 through the Arc multicloud connector) runs without an agent. */
                agentless: boolean;
            };
            edge: components["schemas"]["ConsoleEdge"];
            restore_waves: {
                wave_id: components["schemas"]["Id"];
                job_id: string | null;
                state: string;
                objects: number;
                restored: number;
                open: number;
                speed: string;
                days: number;
                /** Format: date-time */
                requested_at: string;
                /** Format: date-time */
                finished_at: string | null;
                /** Format: date-time */
                first_expiry: string | null;
                failure: string | null;
            }[];
            checksum_jobs: {
                checksum_job_id: components["schemas"]["Id"];
                job_id: string | null;
                state: string;
                objects: number;
                found: number | null;
                /** Format: date-time */
                requested_at: string;
                /** Format: date-time */
                finished_at: string | null;
                failure: string | null;
            }[];
            verification: {
                total: number | null;
                verified: number;
                failed: number;
                excluded: number;
                bytes: {
                    total: number;
                    verified: number;
                };
                /** Format: date-time */
                journal_checked_through: string | null;
                archived_waiting: number;
            };
        };
        MigrationAgents: {
            agents: components["schemas"]["MoverAgent"][];
            /** @description False when the worker has listed no agents (none registered, or no Storage Mover configured). */
            polled: boolean;
            jobs: {
                migration_id: components["schemas"]["Id"];
                dataset_name: string;
                prefix: string;
                state: string;
                copier_kind: string;
                job_ref: string | null;
                run_state: string | null;
                run_counters: {
                    [key: string]: number;
                };
                edge: components["schemas"]["ConsoleEdge"];
                restore_waves_running: number;
                checksum_jobs_running: number;
            }[];
        };
        WaveWindow: null | {
            start: string;
            end: string;
        };
        Blackout: {
            /** Format: date-time */
            from: string;
            /** Format: date-time */
            to: string;
        };
        WaveCreate: {
            name?: string;
            prefixes: string[];
            /** Format: date-time */
            start_at: string;
            window?: components["schemas"]["WaveWindow"];
            blackouts?: components["schemas"]["Blackout"][];
            /** @description The number of an earlier wave that must be verified first. */
            after?: number | null;
            /**
             * @default auto
             * @enum {string}
             */
            cutover?: "auto" | "manual";
            /**
             * @description external is refused for now (422 action_not_allowed): an external copier's run reports can't reach a plan's migrations.
             * @default auto
             * @enum {string}
             */
            copier?: "auto" | "external";
            /** @description Advisory - shown */
            bandwidth_mbps?: number | null;
            /** @description Advisory - shown */
            egress_gb_per_day?: number | null;
        };
        MigrationPlanCreate: {
            dataset_id: components["schemas"]["Id"];
            name: string;
            /** @description An AD user in the sources' Owners or Writers group. A signed-in ArchiveAdmin may leave it out (themself). */
            owner_identity_id?: components["schemas"]["Id"];
            waves: components["schemas"]["WaveCreate"][];
        };
        WavePatch: {
            /** @description The plan's version you edited. */
            base_version: number;
            /** Format: date-time */
            start_at?: string;
            window?: components["schemas"]["WaveWindow"];
            blackouts?: components["schemas"]["Blackout"][];
            after?: number | null;
            /** @enum {string} */
            cutover?: "auto" | "manual";
        };
        PlanDecision: {
            note?: string;
        };
        MigrationWave: {
            seq: number;
            name: string;
            prefixes: string[];
            /** Format: date-time */
            start_at: string;
            window: components["schemas"]["WaveWindow"];
            blackouts: components["schemas"]["Blackout"][];
            after: number | null;
            cutover: string;
            copier: string;
            bandwidth_mbps: number | null;
            egress_gb_per_day: number | null;
            /** @description blocked - the wave it waits for was cancelled, so it never starts; cancelled - by the plan, or all its migrations were. */
            state: string;
            waiting_reason: string | null;
            /** Format: date-time */
            started_at: string | null;
            /** Format: date-time */
            verified_at: string | null;
            /** Format: date-time */
            cut_over_at: string | null;
            migrations: {
                prefix: string;
                migration_id: components["schemas"]["Id"];
                state: string;
            }[];
        };
        /**
         * @example {
         *       "plan_id": "3",
         *       "dataset_id": "1",
         *       "name": "Seismic by year",
         *       "state": "approved",
         *       "owner": {
         *         "identity_id": "11",
         *         "display_name": "Ada Admin"
         *       },
         *       "proposed_by": "user:11",
         *       "decided_by": "user:11",
         *       "decided_at": "2026-11-01T09:00:00.000Z",
         *       "decision_note": null,
         *       "version": 1,
         *       "created_at": "2026-11-01T09:00:00.000Z",
         *       "updated_at": "2026-11-01T09:00:00.000Z",
         *       "waves": [
         *         {
         *           "seq": 1,
         *           "name": "2016-2017",
         *           "prefixes": [
         *             "raw/2016/",
         *             "raw/2017/"
         *           ],
         *           "start_at": "2026-11-03T22:00:00.000Z",
         *           "window": {
         *             "start": "22:00",
         *             "end": "06:00"
         *           },
         *           "blackouts": [],
         *           "after": null,
         *           "cutover": "auto",
         *           "copier": "auto",
         *           "bandwidth_mbps": 500,
         *           "egress_gb_per_day": null,
         *           "state": "started",
         *           "waiting_reason": null,
         *           "started_at": "2026-11-03T22:00:00.000Z",
         *           "verified_at": null,
         *           "cut_over_at": null,
         *           "migrations": [
         *             {
         *               "prefix": "raw/2016/",
         *               "migration_id": "11",
         *               "state": "copying"
         *             }
         *           ]
         *         }
         *       ]
         *     }
         */
        MigrationPlan: {
            plan_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            name: string;
            state: string;
            owner: {
                identity_id: components["schemas"]["Id"];
                display_name: string | null;
            };
            proposed_by: string;
            decided_by: string | null;
            /** Format: date-time */
            decided_at: string | null;
            decision_note: string | null;
            version: number;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            updated_at: string;
            waves: components["schemas"]["MigrationWave"][];
        };
        MigrationSplit: {
            source_id: components["schemas"]["Id"];
            prefix: string;
            /** @enum {string} */
            by: "bytes" | "count";
            prefixes: {
                prefix: string;
                files: number;
                bytes: number;
            }[];
            /** @description Objects directly under the source prefix; they move only with the whole prefix. */
            loose: {
                files: number;
                bytes: number;
            };
            /** @description Folders a signed-in caller can't read: counted, never named, and left out of the waves (a role never grants read). */
            hidden: {
                folders: number;
                files: number;
                bytes: number;
            };
            waves: {
                prefixes: string[];
                files: number;
                bytes: number;
            }[];
        };
        RecallBatchCreate: {
            dataset_id: components["schemas"]["Id"];
            /** @description Dataset paths (files or folders, from '/'); absent - the whole dataset. */
            paths?: string[];
            /** Format: date-time */
            due_by?: string | null;
            /**
             * @description Null - the cheapest offered priority that meets due_by.
             * @enum {string|null}
             */
            priority?: "standard" | "high" | null;
        };
        RecallBatchItem: {
            file_id: components["schemas"]["Id"];
            path: string;
            status: string;
            priority: string | null;
            /** @description Why a file was skipped. */
            reason: string | null;
            /** Format: date-time */
            eta: string | null;
            /** @description No offered priority meets due_by; the fastest is used. */
            late: boolean;
            /** @description In the currency of the price table that priced the file. */
            fee: components["schemas"]["Money"] | null;
            recall_id: components["schemas"]["Id"] | null;
        };
        /**
         * @example {
         *       "batch_id": "9",
         *       "dataset_id": "2",
         *       "paths": [
         *         "/seismic/raw/2019"
         *       ],
         *       "due_by": "2026-10-20T00:00:00.000Z",
         *       "priority": null,
         *       "state": "starting",
         *       "counts": {
         *         "files": 120,
         *         "planned": 118,
         *         "started": 0,
         *         "joined": 0,
         *         "online": 2,
         *         "skipped": 0,
         *         "late": 0
         *       },
         *       "bytes": 22080000000000,
         *       "eta": "2026-10-16T09:00:00.000Z",
         *       "eta_confidence": "provider_sla",
         *       "costs": [
         *         {
         *           "amount": "441.60",
         *           "currency": "USD"
         *         }
         *       ],
         *       "cost": {
         *         "amount": "441.60",
         *         "currency": "USD"
         *       },
         *       "created_at": "2026-10-15T09:00:00.000Z",
         *       "done_at": null
         *     }
         */
        RecallBatch: {
            batch_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            paths: string[] | null;
            /** Format: date-time */
            due_by: string | null;
            priority: string | null;
            state: string;
            counts: {
                files: number;
                planned: number;
                started: number;
                joined: number;
                online: number;
                skipped: number;
                late: number;
            };
            bytes: number;
            /**
             * Format: date-time
             * @description When the last planned file should be back; null while planning, or when a file has no promised time (above the no-promise size).
             */
            eta: string | null;
            eta_confidence: string;
            /** @description The planned files' retrieval fees (an estimate), one total per currency, by currency code. A migrating dataset's files can be priced by two providers' tables; amounts in different currencies are never added. */
            costs: components["schemas"]["Money"][];
            /** @description The one total of costs when every fee is in one currency; null with no fee or with several currencies (read costs). */
            cost: components["schemas"]["Money"] | null;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            done_at: string | null;
            /** @description GET only. */
            items?: components["schemas"]["RecallBatchItem"][];
            next_cursor?: components["schemas"]["NextCursor"];
        };
        /** @description Finds a backend an ArchiveAdmin configured; never creates one. Any other config field is 422 config_invalid. */
        InlineBackend: {
            /** @enum {string} */
            kind: "s3";
            config: {
                bucket?: string;
                /** @description The bucket */
                container?: string;
                region?: string;
                account_id?: string;
                /** @description Only false today. */
                requester_pays?: boolean;
                /**
                 * @description A partner can't turn it off.
                 * @constant
                 */
                private_network?: true;
            };
        };
        /**
         * @description Built-in tables live in service/src/omnifile_service/policy/prices/<kind>.json.
         *     Every number carries the table's as_of date; verify stays true until all are
         *     checked against the provider's price page. Prices are per GB-month and
         *     per 10,000 operations, as the provider states them.
         */
        PriceTable: {
            id?: string;
            backend_kind: string;
            region?: string | null;
            currency: string;
            /** Format: date */
            as_of: string;
            /** Format: uri */
            source?: string | null;
            verify: boolean;
            readonly overridden?: boolean;
            readonly etag?: string;
            /** @description Keyed by native class (Hot, Cool, Cold, Archive, Smart, STANDARD, ...). */
            classes: {
                [key: string]: {
                    gb_month?: string;
                    write_per_10k?: string;
                    read_per_10k?: string;
                    retrieval_per_gb?: string;
                    retrieval_per_gb_high?: string;
                    read_per_10k_high?: string;
                    minimum_days?: number | null;
                    monitoring_per_10k_objects_month?: string;
                    verify?: boolean;
                };
            };
            notes?: string[];
            /** @description The list prices: the built-in table, whatever the override says. The editor's "reset to list price" copies from it. Ignored in a PUT. */
            readonly list?: {
                id?: string;
                region?: string | null;
                currency?: string;
                /** Format: date */
                as_of: string;
                classes: {
                    [key: string]: {
                        [key: string]: unknown;
                    };
                };
            };
        };
        PriceDisplay: {
            /** @description The roles that see prices; `others` is every signed-in user with neither admin role. Empty: nobody (the ArchiveAdmins' price editor still shows the rates). */
            show_to: ("ArchiveAdmin" | "DataSteward" | "others")[];
            /** @description Who last changed it. */
            set_by: string | null;
            /** Format: date-time */
            set_at: string | null;
        };
        /**
         * @example {
         *       "dataset_id": "2",
         *       "name": "seismic",
         *       "backend_kind": "azure_blob",
         *       "private_network": true,
         *       "storage_layout": "two_account",
         *       "migration_state": "steady",
         *       "shares": [
         *         {
         *           "share_id": "4",
         *           "name": "long-term-storage",
         *           "worm_enabled": false
         *         }
         *       ],
         *       "active_policy": {
         *         "version": 3,
         *         "mode": "scheduled",
         *         "archive_day": 365
         *       },
         *       "pending_proposals": 1
         *     }
         */
        Dataset: {
            dataset_id: components["schemas"]["Id"];
            name: string;
            backend_id?: components["schemas"]["Id"];
            backend_kind: string;
            private_network?: boolean;
            /** @description The edge that serves it (v1: one). */
            edge_id?: components["schemas"]["Id"] | null;
            key_prefix?: string;
            root_dir_id?: components["schemas"]["Id"];
            storage_layout: string;
            /** @description handed_off is reserved (decision 35, migration-namespace.md). */
            migration_state?: string;
            shares: {
                share_id: components["schemas"]["Id"];
                name: string;
                worm_enabled?: boolean;
            }[];
            active_policy?: null | {
                version: number;
                /** @enum {string} */
                mode: "scheduled" | "smart" | "m1";
                archive_day?: number | null;
            };
            pending_proposals?: number;
        };
        /**
         * @example {
         *       "backend_id": "1",
         *       "name": "azure-weu",
         *       "kind": "azure_blob",
         *       "private_network": true,
         *       "can_be_home": true
         *     }
         */
        Backend: {
            backend_id: components["schemas"]["Id"];
            name: string;
            kind: string;
            private_network: boolean;
            /** @description False for S3: a migration source, never a dataset's home (decision 18). */
            can_be_home: boolean;
        };
        /**
         * @example {
         *       "dataset_id": "2",
         *       "name": "long-term-storage",
         *       "protocols": [
         *         "nfs4",
         *         "smb"
         *       ],
         *       "nfs_sec": [
         *         "krb5p"
         *       ],
         *       "owners_group_sid": "S-1-5-21-1000-2000-3000-3003",
         *       "readers_group_sid": "S-1-5-21-1000-2000-3000-3001",
         *       "on_open": "auto",
         *       "wait_threshold_minutes": 10
         *     }
         */
        NewShare: {
            dataset_id: components["schemas"]["Id"];
            /** @description The SMB share name and the NFS pseudo path; for a share of the dataset's root, also the root folder's name on the edge. */
            name: string;
            /**
             * @description The folder shared: `/` (the dataset's root, the default) or an existing folder such as `/projects/2026`. A folder needs the dataset's root shared first.
             * @default /
             */
            path?: string;
            protocols: ("nfs3" | "nfs4" | "smb")[];
            /** @description NFS shares only. Any sys makes the share host-trusted (decision 28). */
            nfs_sec?: ("sys" | "krb5" | "krb5i" | "krb5p")[];
            /** @description NFS shares only; required with sys, and never a wildcard. */
            nfs_clients?: string[];
            /** @default true */
            root_squash?: boolean;
            owners_group_sid: components["schemas"]["Sid"];
            writers_group_sid?: components["schemas"]["Sid"];
            readers_group_sid?: components["schemas"]["Sid"];
            /**
             * @default fail
             * @enum {string}
             */
            on_open?: "wait" | "fail" | "auto";
            /** @description With on_open auto only. */
            wait_threshold_minutes?: number;
            /** @default false */
            smb_encrypt?: boolean;
            /** @default 0 */
            rewind_depth_days?: number;
            /** @default 0 */
            rewind_depth_max_days?: number;
            /**
             * @description Accept a sec=sys share over folders with a restricted group. Audited.
             * @default false
             */
            host_trust_override?: boolean;
        };
        /**
         * @example {
         *       "audit_id": "90211",
         *       "at": "2026-10-06T09:30:00.000Z",
         *       "actor": "user:11",
         *       "actor_display_name": "Ada Admin",
         *       "actor_sid": "S-1-5-21-1000-2000-3000-1001",
         *       "proof": "proven",
         *       "trust_level": null,
         *       "action": "policy.approve",
         *       "decision": "allow",
         *       "dataset_id": "2",
         *       "share_id": null,
         *       "file_id": null,
         *       "version_id": null,
         *       "client_addr": "10.20.0.15",
         *       "details": {
         *         "proposal_id": "5",
         *         "version": 3
         *       }
         *     }
         */
        AuditRecord: {
            audit_id: components["schemas"]["Id"];
            /** Format: date-time */
            at: string;
            /** @description user:<identity_id>, app:<client id>, edge:<edge_id>, system:<job> */
            actor: string;
            actor_display_name?: string | null;
            actor_sid?: components["schemas"]["Sid"] | null;
            /** @enum {string|null} */
            proof?: "claimed" | "proven" | null;
            /** @enum {string|null} */
            trust_level?: "proven" | "host_trusted" | null;
            action: string;
            /** @enum {string} */
            decision: "allow" | "deny" | "info";
            dataset_id?: components["schemas"]["Id"] | null;
            share_id?: components["schemas"]["Id"] | null;
            file_id?: components["schemas"]["Id"] | null;
            version_id?: components["schemas"]["Id"] | null;
            /** @description ArchiveAdmin only. */
            client_addr?: string | null;
            details?: {
                [key: string]: unknown;
            };
        };
        MapCount: {
            files: number;
            /** @description Sum of the files' sizes. */
            bytes: number;
            /** @description Bytes the edge holds for them (a stub keeps its first MB). */
            local_bytes: number;
        };
        /**
         * @example {
         *       "file_id": "1042",
         *       "backend": {
         *         "kind": "azure_blob",
         *         "role": "home",
         *         "storage_layout": "two_account",
         *         "account": "archive",
         *         "migration_state": "steady"
         *       },
         *       "tiers": [
         *         {
         *           "logical": "hot",
         *           "classes": [
         *             {
         *               "native_class": "Hot",
         *               "minimum_days": null,
         *               "needs_restore": false
         *             }
         *           ]
         *         },
         *         {
         *           "logical": "warm",
         *           "classes": [
         *             {
         *               "native_class": "Cool",
         *               "minimum_days": 30,
         *               "needs_restore": false
         *             }
         *           ]
         *         },
         *         {
         *           "logical": "cold",
         *           "classes": [
         *             {
         *               "native_class": "Cold",
         *               "minimum_days": 90,
         *               "needs_restore": false
         *             }
         *           ]
         *         },
         *         {
         *           "logical": "archive",
         *           "classes": [
         *             {
         *               "native_class": "Archive",
         *               "minimum_days": 180,
         *               "needs_restore": true
         *             }
         *           ]
         *         },
         *         {
         *           "logical": "smart",
         *           "classes": [
         *             {
         *               "native_class": "Smart",
         *               "minimum_days": null,
         *               "needs_restore": false
         *             }
         *           ]
         *         }
         *       ],
         *       "pin": null,
         *       "holds": [
         *         {
         *           "hold_id": "52",
         *           "kind": "keep_online",
         *           "until": "2026-10-20T09:30:00.000Z",
         *           "set_at": "2026-10-06T09:30:00.000Z",
         *           "set_by": {
         *             "kind": "identity",
         *             "display_name": "Ada Admin",
         *             "app_id": null,
         *             "identity_id": "11"
         *           },
         *           "reason": "Processing run",
         *           "inherited_from": null
         *         }
         *       ]
         *     }
         */
        FileJourney: {
            file_id: components["schemas"]["Id"];
            /** @description The backend that holds the primary copy now (the dataset's home if there is none yet). */
            backend: {
                kind: string;
                /** @description source: the dataset's migration source. */
                role: string;
                storage_layout: string;
                /** @description Which account holds the copy (Azure layouts); null on S3 or without a copy. */
                account: string | null;
                migration_state: string;
            };
            /** @description That backend's tiers from its capabilities, warmest first (hot, warm, cold, archive, then smart). Empty if its capabilities can't be read. */
            tiers: {
                logical: components["schemas"]["LogicalTier"];
                /** @description The native classes behind it; the first is the default. */
                classes: {
                    native_class: string;
                    minimum_days: number | null;
                    needs_restore: boolean;
                }[];
            }[];
            /** @description The nearest active pin on the file or a folder above it. */
            pin: null | {
                pin_id: components["schemas"]["Id"];
                tier: components["schemas"]["PinTier"];
                /** Format: date-time */
                until: string | null;
                override_archive: boolean;
                /** Format: date-time */
                set_at: string;
                /** @description ArchiveAdmin, DataSteward, the share's Owners, and the person who set it; null for others. */
                set_by: components["schemas"]["PersonRef"] | null;
                /** @description ArchiveAdmin, DataSteward and the share's Owners; null for others. */
                reason: string | null;
                inherited_from: components["schemas"]["Id"] | null;
            };
            /** @description The active holds on the file or a folder above it. */
            holds: {
                hold_id: components["schemas"]["Id"];
                /** @enum {string} */
                kind: "keep_online" | "legal";
                /** Format: date-time */
                until: string | null;
                /** Format: date-time */
                set_at: string;
                /** @description ArchiveAdmin, DataSteward, the share's Owners, and the person who set it; null for others. */
                set_by: components["schemas"]["PersonRef"] | null;
                /** @description ArchiveAdmin, DataSteward and the share's Owners; null for others. */
                reason: string | null;
                inherited_from: components["schemas"]["Id"] | null;
            }[];
        };
        /**
         * @example {
         *       "share_id": "4",
         *       "dataset_id": "2",
         *       "scope": "readable",
         *       "basis": "rollup",
         *       "computed_at": "2026-10-06T09:30:00.000Z",
         *       "home": {
         *         "backend_kind": "azure_blob",
         *         "storage_layout": "two_account"
         *       },
         *       "source": {
         *         "backend_kind": "s3"
         *       },
         *       "migration_state": "migrating",
         *       "edge": {
         *         "resident": {
         *           "files": 120,
         *           "bytes": 96000000000,
         *           "local_bytes": 96000000000
         *         },
         *         "stub": {
         *           "files": 40,
         *           "bytes": 7360000000000,
         *           "local_bytes": 167772160
         *         },
         *         "arriving": {
         *           "files": 2,
         *           "bytes": 18000000000,
         *           "local_bytes": 18000000000
         *         },
         *         "remote": {
         *           "files": 300,
         *           "bytes": 1200000000000,
         *           "local_bytes": 0
         *         }
         *       },
         *       "stores": [
         *         {
         *           "backend_kind": "azure_blob",
         *           "role": "home",
         *           "tiers": [
         *             {
         *               "logical_tier": "hot",
         *               "native_class": "Hot",
         *               "account": "live",
         *               "files": 80,
         *               "bytes": 64000000000
         *             },
         *             {
         *               "logical_tier": "warm",
         *               "native_class": "Cool",
         *               "account": "live",
         *               "files": 40,
         *               "bytes": 32000000000
         *             },
         *             {
         *               "logical_tier": "archive",
         *               "native_class": "Archive",
         *               "account": "archive",
         *               "files": 40,
         *               "bytes": 7360000000000
         *             }
         *           ]
         *         },
         *         {
         *           "backend_kind": "s3",
         *           "role": "source",
         *           "tiers": [
         *             {
         *               "logical_tier": "archive",
         *               "native_class": "GLACIER",
         *               "account": null,
         *               "files": 300,
         *               "bytes": 1200000000000
         *             }
         *           ]
         *         }
         *       ],
         *       "recalls": {
         *         "open": 3,
         *         "by_step": {
         *           "requested": 1,
         *           "rehydrating": 2,
         *           "downloading": 0
         *         }
         *       },
         *       "migrations": null
         *     }
         */
        ShareMap: {
            share_id: components["schemas"]["Id"];
            dataset_id: components["schemas"]["Id"];
            /**
             * @description all: every file under the share, counts only (ArchiveAdmin, DataSteward). readable: only files the caller can read, in folders it can read.
             * @enum {string}
             */
            scope: "all" | "readable";
            /** @description rollup: summed from the worker's roll-up, true as of computed_at (both scopes). counting: a folder it would sum hasn't been counted yet, so there is no total, never a total of zero. none: the count for scope readable ran out of time. With counting or none, edge is null and stores empty. */
            basis: string;
            /**
             * Format: date-time
             * @description The time the counts are true as of: the oldest count summed among folders with a change waiting, now for the rest. null unless basis is rollup.
             */
            computed_at: string | null;
            home: {
                backend_kind: string;
                storage_layout: string;
            };
            /** @description The migration source, while the dataset has one. */
            source: null | {
                backend_kind: string;
            };
            migration_state: string;
            /** @description What the edge holds, by file.state: resident (whole), stub (stub or being filled), arriving (landing or uploading), remote (never local). null with basis none. */
            edge: {
                resident: components["schemas"]["MapCount"];
                stub: components["schemas"]["MapCount"];
                arriving: components["schemas"]["MapCount"];
                remote: components["schemas"]["MapCount"];
            } | null;
            /** @description Primary copies per backend (home first), then per tier, warmest first. */
            stores: {
                backend_kind: string;
                role: string;
                tiers: {
                    logical_tier: string | null;
                    native_class: string | null;
                    account: string | null;
                    files: number;
                    bytes: number;
                }[];
            }[];
            /** @description Always live. null only when a readable count ran out of time. */
            recalls: {
                open: number;
                by_step: {
                    requested: number;
                    rehydrating: number;
                    downloading: number;
                };
            } | null;
            /** @description The dataset's open migrations; ArchiveAdmin only (null for others). */
            migrations: null | {
                migration_id: components["schemas"]["Id"];
                state: string;
                prefix: string;
                files: {
                    total: number | null;
                    verified: number;
                };
            }[];
        };
        MapFile: {
            file_id: components["schemas"]["Id"];
            name: string;
            parent_id: components["schemas"]["Id"] | null;
            size: number;
            state: string;
            resident_bytes: number;
            tier: components["schemas"]["TierState"];
        };
        /**
         * @example {
         *       "type": "urn:omnifile:error:name_taken",
         *       "title": "Name taken",
         *       "status": 409,
         *       "code": "name_taken",
         *       "detail": "'line_1042-restored.segy' already exists in that folder",
         *       "retryable": false,
         *       "retry_after_s": null,
         *       "request_id": "5f0c1e9a2b3c4d5e6f708192a3b4c5d6"
         *     }
         */
        Problem: {
            /** @description urn:omnifile:error:<code> */
            type: string;
            title: string;
            status: number;
            /**
             * @description See docs/contracts/portal-api.md section 7 and control-api.md section 9.
             * @enum {string}
             */
            code: "invalid_request" | "not_found" | "version_conflict" | "worm_violation" | "request_in_progress" | "policy_not_approved" | "layout_conflict" | "explicit_tier_set" | "beyond_rewind_depth" | "cursor_expired" | "idempotency_key_reused" | "throttled" | "actor_required" | "internal" | "unavailable" | "unauthenticated" | "reauth_required" | "role_required" | "forbidden" | "csrf_failed" | "self_approval" | "proposal_not_pending" | "recall_not_needed" | "version_not_restorable" | "worm_window_closed" | "precondition_failed" | "confirmation_mismatch" | "policy_invalid" | "precondition_required" | "groups_pending" | "name_taken" | "source_conflict" | "migration_conflict" | "config_invalid" | "batch_too_large" | "edge_enrolled" | "dataset_shared" | "share_in_use" | "client_not_registered" | "client_disabled" | "owner_lacks_access" | "dataset_annexed" | "file_present" | "copy_not_refused" | "owner_required" | "mode_not_available" | "rules_check_failed" | "target_not_allowed" | "requester_required" | "placement_not_cancellable" | "placement_not_abandonable" | "dependency_failed";
            detail?: string;
            retryable: boolean;
            retry_after_s?: number | null;
            request_id?: string;
            /** @description invalid_request only. */
            errors?: {
                field: string;
                message: string;
            }[];
            /** @description policy_invalid only. */
            checks?: components["schemas"]["Check"][];
            /** @description reauth_required only. */
            max_age_s?: number;
            /** @description rules_check_failed only. The annex.md section 4.2 checks a rule set fails. */
            rule_checks?: components["schemas"]["AnnexRuleCheck"][];
        };
        SettingCheck: {
            /** @description What was checked, e.g. private_dns, rbac_role, archive_pair, change_feed, assume_role. */
            code: string;
            status: string;
            title: string;
            /** @description Plain language */
            message: string;
            /** @description What to do about it */
            fix?: string | null;
        };
        /**
         * @example {
         *       "backend_id": "1",
         *       "name": "azure",
         *       "kind": "azure_blob",
         *       "private_network": true,
         *       "can_be_home": true,
         *       "where": "live omnilive01, archive omniarch01, Smart, westeurope",
         *       "config": {
         *         "layout": "two_account",
         *         "primary_account": "omnilive01",
         *         "archive_account": "omniarch01",
         *         "smart": true,
         *         "container": "omnilive01/data",
         *         "region": "westeurope"
         *       },
         *       "uses": {
         *         "datasets": 2,
         *         "sources": 0,
         *         "migrations": 0
         *       },
         *       "created_at": "2026-10-06T09:30:00.000Z"
         *     }
         */
        StorageBackend: {
            backend_id: components["schemas"]["Id"];
            name: string;
            kind: string;
            private_network: boolean;
            can_be_home: boolean;
            /** @description Where its bytes are, in a few words. */
            where: string;
            /** @description The row's names (accounts, containers, buckets, region, layout). Never a secret; a row holds none. */
            config: {
                [key: string]: unknown;
            };
            uses: {
                datasets: number;
                sources: number;
                migrations: number;
            };
            /** Format: date-time */
            created_at: string;
        };
        StorageSettings: {
            backends: components["schemas"]["StorageBackend"][];
            /** @description The deployment reaches storage only through private endpoints (decision 22): a public backend is refused. */
            private_network: boolean;
            /** @description The service has AWS settings (OMNIFILE_AWS_ACCOUNTS), so S3 backends can be checked. */
            aws_configured: boolean;
            dev_mode: boolean;
        };
        /** @description Azure: live and archive (two accounts, non-WORM) or account (one account, WORM shares). S3: account, region and buckets. Endpoints, roles and credentials are deployment settings, never a row's. */
        BackendDraft: {
            /** @enum {string} */
            kind: "azure" | "s3";
            name?: string;
            live?: string;
            archive?: string;
            /** @description Azure: the one storage account. S3: the 12-digit account id. */
            account?: string;
            /** @default false */
            worm?: boolean;
            smart?: boolean;
            /** @default data */
            container?: string;
            region?: string;
            buckets?: string[];
            /** @default true */
            versioning?: boolean;
            /** @enum {string} */
            archive_class?: "GLACIER" | "DEEP_ARCHIVE";
        };
        BackendChange: {
            /** @enum {string} */
            outcome: "added" | "updated" | "unchanged";
            backend: components["schemas"]["StorageBackend"];
            changes: string[];
        };
        BackendChecks: {
            backend_id: components["schemas"]["Id"];
            /** Format: date-time */
            checked_at: string;
            checks: components["schemas"]["SettingCheck"][];
        };
        AwsSettings: {
            configured: boolean;
            private_network: boolean;
            /** @description The web-identity audience: the deployment's api:// application ID URI. */
            audience: string | null;
            tenant_id: string;
            /** @description The token issuer AWS federates: https://sts.windows.net/<tenant>/ */
            issuer: string;
            /** @description Object ids of the service identities that assume the role (OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS). */
            subjects: string[];
            accounts: {
                account_id: string;
                region: string;
                role_arn: string | null;
                sts_endpoint?: string | null;
            }[];
        };
        AwsTrustPolicy: {
            role_arn: string;
            account_id: string;
            /** @description The account is in the service's AWS settings, so Test can run. */
            configured_account: boolean;
            oidc_provider: {
                url: string;
                arn: string;
                audience: string | null;
            };
            trust_policy: {
                [key: string]: unknown;
            };
            steps: string[];
        };
        /**
         * @example {
         *       "test_id": "12",
         *       "kind": "teams_card",
         *       "state": "passed",
         *       "detail": "Card sent to your Teams chat.",
         *       "result": {},
         *       "created_at": "2026-10-06T09:30:00.000Z",
         *       "finished_at": "2026-10-06T09:30:04.000Z"
         *     }
         */
        SettingsTest: {
            test_id: components["schemas"]["Id"];
            kind: string;
            state: string;
            detail: string | null;
            /** @description aws_assume_role: assumed_role_arn, expires_at. Never credentials. */
            result: {
                [key: string]: unknown;
            };
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            finished_at: string | null;
        };
        IdentitySettings: {
            tenant_id: string;
            client_id: string;
            api_audiences: string[];
            consent: {
                status: string;
                detail: string;
                /** @description Entra's admin-consent link for the portal's app. Only a tenant administrator can grant it; the app can't. */
                url: string;
            };
            graph: components["schemas"]["SettingCheck"];
            roles: {
                role: string;
                member_types: ("user" | "application")[];
                groups: {
                    object_id: string | null;
                    display_name: string | null;
                    sid?: string | null;
                }[];
                /** @description People who held it at a portal sign-in in the last 30 days. */
                people_seen: number;
                source: string;
            }[];
        };
        WhoAmI: {
            object_id: string;
            identity_id: components["schemas"]["Id"] | null;
            display_name: string | null;
            upn: string | null;
            sid: string | null;
            roles: string[];
            groups_source: string;
            group_count: number;
            owner_of: components["schemas"]["Id"][];
            session: {
                /** Format: date-time */
                auth_time: string | null;
                /** Format: date-time */
                expires_at: string;
                /** @description Signed in within 10 minutes, as Settings changes need. */
                recently_authenticated: boolean;
            };
            checks: components["schemas"]["SettingCheck"][];
        };
        TeamsSettings: {
            configured: boolean;
            teams_app_id: string | null;
            bot_app_id: string | null;
            health: {
                status: string;
                cards_sent_24h: number;
                cards_failed_24h: number;
                /** Format: date-time */
                last_sent_at: string | null;
                last_error: string | null;
                conversations_blocked: number;
                message: string;
            };
            package: {
                available: boolean;
                file_name: string | null;
                instructions: string[];
            };
        };
        EmailSettings: {
            configured: boolean;
            sender: string | null;
            /** @description Where your test email goes: your mail address as Graph last gave it. */
            my_mail: string | null;
            health: {
                sent_24h: number;
                failed_24h: number;
                /** Format: date-time */
                last_sent_at: string | null;
                last_error: string | null;
            };
            instructions: string[];
        };
        StorageMoverSettings: {
            configured: boolean;
            /** @description The latest deployment from the portal (cloud-connect.md 3). */
            deployment: components["schemas"]["MoverDeployment"] | null;
            can_deploy: {
                ok: boolean;
                /** @description Why not: Azure isn't connected, or no Storage Mover resource group was granted. */
                reason: string | null;
                subscription_id: string | null;
                resource_group: string | null;
            };
            /** @description Downloading and registering the on-premises agent. */
            agent_steps: string[];
            storage_mover: {
                name: string;
                resource_group: string;
                subscription_id: string;
                provisioning_state: string | null;
            } | null;
            agents: {
                name: string;
                /** @description Storage Mover's agentStatus (Online, Offline, Executing, ...), as it says it. */
                status: string | null;
                last_status_update?: string | null;
                arc_resource_id?: string | null;
                version?: string | null;
            }[];
            projects: {
                name: string;
                description?: string | null;
                provisioning_state: string | null;
                /** @description The project the migration engine runs jobs in. */
                in_use?: boolean;
            }[];
            endpoints: {
                /** @description S3 bucket to its source endpoint. */
                source: {
                    [key: string]: string;
                };
                /** @description <account>/<container> to its target endpoint. */
                target: {
                    [key: string]: string;
                };
            };
            error: string | null;
        };
        ConnectStep: {
            code: string;
            title: string;
            status: string;
            detail: string;
        };
        AzureSignIn: {
            op_id: components["schemas"]["Id"];
            purpose: string;
            authorize_url: string;
            /** Format: date-time */
            expires_at: string;
        };
        AzureGrantPlan: {
            /** @description purpose|scope|principal: stable across plans. */
            key: string;
            purpose: string;
            role_name: string;
            /** @description The custom role's id (a GUID fixed by the scope). */
            role_definition_id: string;
            /** @description /subscriptions/<id>/resourceGroups/<name> */
            scope: string;
            /** @description The service identity that gets it. */
            principal: string;
            /** @description Its managed identity's object id (OMNIFILE_SERVICE_PRINCIPAL_IDS); null when the deployment doesn't say. */
            principal_id: string | null;
            /** @description Every action in the role. No data actions */
            actions: string[];
            state: string;
            grant_id: components["schemas"]["Id"] | null;
        };
        AzureGrant: {
            grant_id: components["schemas"]["Id"];
            purpose: string;
            role_name: string;
            scope: string;
            principal: string;
            principal_id: string;
            assignment_id: string;
            state: string;
            /** Format: date-time */
            made_at: string;
            made_by: string | null;
            /** Format: date-time */
            removed_at: string | null;
            removed_by: string | null;
        };
        AzureStorageAccount: {
            id: string;
            name: string;
            resource_group: string;
            location: string | null;
            sku: string | null;
            kind: string | null;
            zrs: boolean;
            hns: boolean;
            public_network_access: string | null;
            /** @description An approved private endpoint for its blob service (a connection with no status is not approved). */
            blob_private_endpoint: boolean;
            /** @description A privatelink.blob DNS zone is linked to the service's VNet, so the service resolves the account privately. */
            dns_linked: boolean;
            /** @description Reachable privately (decision 22): a blob private endpoint, the DNS zone linked, and public access closed (publicNetworkAccess Disabled, or Deny by default with no IP or VNet rule). */
            private: boolean;
        };
        AzureDiscovery: {
            /** Format: date-time */
            discovered_at: string;
            /** @description The service's VNet (OMNIFILE_SERVICE_VNET_ID). */
            service_vnet_id: string | null;
            blob_dns_zones: {
                id: string;
                linked_to_service: boolean;
            }[];
            storage_accounts: components["schemas"]["AzureStorageAccount"][];
            virtual_networks: {
                id: string;
                name: string;
                resource_group: string;
                location: string | null;
                address_prefixes: string[];
                subnets: string[];
            }[];
            private_endpoints: {
                id: string;
                name: string;
                resource_group: string;
                subnet_id: string | null;
                target_id: string | null;
                group_ids: string[];
                status: string | null;
            }[];
            storage_movers: {
                id: string;
                name: string;
                resource_group: string;
                location: string | null;
            }[];
            /** @description Resource groups that couldn't be read, and why. */
            errors: string[];
        };
        AzureSettings: {
            tenant_id: string;
            identities: {
                name: string;
                object_id: string | null;
            }[];
            steps: components["schemas"]["ConnectStep"][];
            listing: {
                /** Format: date-time */
                listed_at: string;
                listed_by: string | null;
                tenants: {
                    tenant_id: string;
                    display_name: string | null;
                    selectable: boolean;
                    reason: string | null;
                }[];
                subscriptions: {
                    subscription_id: string;
                    display_name: string | null;
                    tenant_id: string | null;
                    state: string | null;
                }[];
                resource_groups: {
                    subscription_id: string;
                    name: string;
                    location: string | null;
                }[];
                truncated: boolean;
            } | null;
            choice: {
                subscription_id: string;
                resource_groups: string[];
                mover_resource_group: string | null;
                /** Format: date-time */
                chosen_at: string;
                chosen_by: string | null;
            } | null;
            plan: components["schemas"]["AzureGrantPlan"][];
            grants: components["schemas"]["AzureGrant"][];
            discovered: components["schemas"]["AzureDiscovery"] | null;
            confirmed: {
                /** Format: date-time */
                confirmed_at: string;
                confirmed_by: string | null;
                storage_accounts: string[];
                virtual_networks: string[];
                private_endpoints: string[];
            } | null;
            last_op: {
                op_id: components["schemas"]["Id"];
                purpose: string;
                state: string;
                detail: string | null;
                /** Format: date-time */
                created_at: string;
                /** Format: date-time */
                finished_at: string | null;
            } | null;
        };
        AwsConnect: {
            /** @description The deployment knows its audience and service identities, so the template can be filled in. */
            ready: boolean;
            problems: string[];
            tenant_id: string;
            audience: string | null;
            subjects: string[];
            private_network: boolean;
            region: string;
            deployment_id: string;
            stack_name: string;
            /** @description arn:aws:iam::<account>:role/omnifile-service-<deployment_id> */
            role_arn_pattern: string;
            /** @description The template's parameters */
            parameters: {
                [key: string]: string;
            };
            /** @description The CloudFormation console's quick-create link; null when no template URL in S3 is configured (OMNIFILE_AWS_CONNECT_TEMPLATE_URL). */
            quick_create_url: string | null;
            /** @description The console's Create stack page, for uploading the template. */
            console_create_url: string;
            template_path: string;
            steps: string[];
            connections: components["schemas"]["AwsConnection"][];
        };
        AwsConnectEntry: {
            account_id: string | null;
            region: string | null;
            deployment_id: string | null;
            outputs: {
                [key: string]: string;
            };
            /** @description The account's OMNIFILE_AWS_ACCOUNTS entry, checked by the loader the worker uses. */
            entry: {
                [key: string]: unknown;
            } | null;
            buckets: string[];
            problems: string[];
        };
        AwsConnection: {
            account_id: string;
            region: string;
            deployment_id: string;
            entry: {
                [key: string]: unknown;
            };
            buckets: string[];
            /** @description OMNIFILE_AWS_ACCOUNTS */
            env_name: string;
            /** @description The Bicep parameter that sets it (awsAccounts). */
            parameter_name: string;
            /** @description The whole OMNIFILE_AWS_ACCOUNTS value: the deployment's accounts with this one added. */
            settings_json: string;
            /** @description The deployment's settings already have this account. */
            configured: boolean;
            /** Format: date-time */
            confirmed_at: string;
            confirmed_by: string | null;
            steps: string[];
        };
        /** @description CloudTask.result of an aws_fetch. */
        AwsFetchResult: {
            account_id: string;
            region: string;
            role_arn: string;
            buckets: {
                name: string;
                region: string | null;
                versioning: string | null;
                /** @description EventBridge notifications are on (null: couldn't be read). */
                eventbridge: boolean | null;
                /** @description The stack's SourceBuckets names it. */
                in_stack: boolean;
            }[];
            queue: {
                url: string;
                ok: boolean;
            };
            stack: {
                [key: string]: unknown;
            } | null;
            entry: {
                [key: string]: unknown;
            };
            outputs: {
                [key: string]: string;
            };
            problems: string[];
        };
        /**
         * @example {
         *       "task_id": "7",
         *       "kind": "mover_deploy",
         *       "state": "done",
         *       "detail": "Created storage mover omnifile-mover and project migrations.",
         *       "progress": {},
         *       "result": {
         *         "storage_mover": "omnifile-mover"
         *       },
         *       "created_at": "2026-11-05T09:30:00.000Z",
         *       "updated_at": "2026-11-05T09:30:04.000Z",
         *       "finished_at": "2026-11-05T09:30:04.000Z"
         *     }
         */
        CloudTask: {
            task_id: components["schemas"]["Id"];
            kind: string;
            state: string;
            detail: string | null;
            progress: {
                [key: string]: unknown;
            };
            /** @description aws_fetch: AwsFetchResult. mover_deploy: the mover's names and OMNIFILE_STORAGE_MOVER value. Never a credential. */
            result: {
                [key: string]: unknown;
            };
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            updated_at: string;
            /** Format: date-time */
            finished_at: string | null;
        };
        MoverDeployment: {
            task_id: components["schemas"]["Id"];
            state: string;
            detail: string | null;
            subscription_id: string;
            resource_group: string;
            name: string;
            project: string;
            location: string;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            finished_at: string | null;
            /** @description The OMNIFILE_STORAGE_MOVER value (storageMoverConfig) to set, with the endpoints still to fill. */
            config_value: string;
            registration: {
                tenant_id: string;
                subscription_id: string;
                resource_group: string;
                storage_mover: string;
            };
        };
        SourceScanResult: {
            objects: number;
            bytes: number;
            /** @description First-level prefixes under the scanned one, largest first (at most 200; the rest summed in "(other)"). */
            prefixes: {
                prefix: string;
                objects: number;
                bytes: number;
                restore_objects: number;
                restore_bytes: number;
            }[];
            classes: {
                /** @description S3's class as listed (STANDARD, STANDARD_IA, GLACIER_IR, GLACIER, DEEP_ARCHIVE, INTELLIGENT_TIERING, ...). */
                storage_class: string;
                objects: number;
                bytes: number;
                restore_needed: boolean;
            }[];
            restore: {
                objects: number;
                bytes: number;
                waves: number;
                /** @description AWS's stated Bulk time for the slowest class to restore (per wave). */
                hours: number | null;
                /** @description Time to send the restore requests at the shared 1,000 a second. */
                request_seconds: number;
                /** @description Decimal; null when no S3 prices are set. */
                cost: string | null;
                currency: string | null;
                prices_set: boolean;
                notes: string[];
            };
            /** @description Objects over 10 GiB. No restore or copy time is promised for these. */
            large_objects: {
                objects: number;
                bytes: number;
            };
            waves: {
                wave: number;
                prefixes: string[];
                objects: number;
                bytes: number;
                restore_objects: number;
                restore_bytes: number;
                /** @description Nothing here needs a restore. */
                copy_only: boolean;
            }[];
        };
        SourceScan: {
            scan_id: components["schemas"]["Id"];
            state: string;
            detail: string | null;
            backend_id: components["schemas"]["Id"];
            bucket: string;
            prefix: string;
            reader: string;
            caps: {
                max_objects: number;
                max_seconds: number;
            };
            progress: {
                objects: number;
                bytes: number;
                batches: number;
                elapsed_s: number;
            };
            result: components["schemas"]["SourceScanResult"] | null;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            finished_at: string | null;
        };
        AlertSettings: {
            /** @description Where the rules live and fire. */
            where: string;
            alerts: {
                name: string;
                condition: string;
                status: string;
                detail: string;
            }[];
        };
        ApiClientOwner: {
            identity_id: components["schemas"]["Id"];
            display_name: string | null;
            upn: string | null;
            /** @description The app roles they held at their last portal sign-in. */
            roles: string[];
            /** Format: date-time */
            roles_seen_at: string | null;
        };
        /**
         * @example {
         *       "api_client_id": "3",
         *       "app_id": "6f1c2d3e-4a5b-4c6d-8e7f-001122334455",
         *       "display_name": "Migration planner",
         *       "scopes": [
         *         "Policy.Propose",
         *         "Archive.Read"
         *       ],
         *       "owner": {
         *         "identity_id": "11",
         *         "display_name": "Ada Admin",
         *         "upn": "ada@contoso.com",
         *         "roles": [
         *           "ArchiveAdmin"
         *         ],
         *         "roles_seen_at": "2026-10-06T09:00:00.000Z"
         *       },
         *       "state": "enabled",
         *       "created_by": {
         *         "kind": "identity",
         *         "display_name": "Ada Admin",
         *         "app_id": null,
         *         "identity_id": "11"
         *       },
         *       "created_at": "2026-10-06T09:30:00.000Z",
         *       "updated_at": "2026-10-06T09:30:00.000Z",
         *       "disabled_at": null,
         *       "revoked_at": null,
         *       "last_used_at": null,
         *       "etag": "\"1\""
         *     }
         */
        ApiClient: {
            api_client_id: components["schemas"]["Id"];
            /** @description The Entra application (client) id its tokens carry as azp. */
            app_id: string;
            display_name: string;
            /** @description The app roles it may use here. A role its token carries but this list lacks is ignored. */
            scopes: ("Policy.Propose" | "Recall.Request" | "Archive.Read" | "Archive.Adopt")[];
            owner: components["schemas"]["ApiClientOwner"];
            state: string;
            created_by: components["schemas"]["PersonRef"];
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            updated_at: string;
            /** Format: date-time */
            disabled_at: string | null;
            /** Format: date-time */
            revoked_at: string | null;
            /** Format: date-time */
            last_used_at: string | null;
            etag: string;
            /** @description Its OmniFile API keys, newest first (never the keys themselves). */
            keys?: components["schemas"]["ApiKey"][];
        };
        ApiClientCreate: {
            /** @description The Entra application (client) id, a GUID. */
            app_id: string;
            display_name: string;
            scopes: ("Policy.Propose" | "Recall.Request" | "Archive.Read" | "Archive.Adopt")[];
            owner_identity_id: components["schemas"]["Id"];
        };
        ApiClientPatch: {
            display_name?: string;
            scopes?: ("Policy.Propose" | "Recall.Request" | "Archive.Read" | "Archive.Adopt")[];
            owner_identity_id?: components["schemas"]["Id"];
            enabled?: boolean;
        };
        /** @description How a client gets a token. OmniFile makes and stores no key; the client uses its own Entra app credential, a certificate by preference. */
        ApiClientSetup: {
            tenant_id: string;
            token_endpoint: string;
            /** @description What to request in client credentials: <application ID URI>/.default */
            scope: string | null;
            audiences: string[];
            app_roles: string[];
            steps: string[];
        };
        /** @description The API client a proposal came from, and its owner. */
        ApiClientRef: {
            api_client_id: components["schemas"]["Id"];
            display_name: string;
            app_id: string;
            owner: components["schemas"]["PersonRef"];
        };
        /** @description An OmniFile API key, as listed. Never the key itself; only its public id. */
        ApiKey: {
            /** @description The public part of ofk1.<key_id>.<secret>; the audit records it. */
            key_id: string;
            scopes: ("Policy.Propose" | "Recall.Request" | "Archive.Read" | "Archive.Adopt")[];
            state: string;
            /** Format: date-time */
            created_at: string;
            created_by: components["schemas"]["PersonRef"];
            /** Format: date-time */
            expires_at: string;
            /** Format: date-time */
            revoked_at: string | null;
            rotated_from: string | null;
            /** Format: date-time */
            last_used_at: string | null;
            /** @description The address of its latest use. */
            last_used_addr: string | null;
        };
        ApiKeyIssued: {
            /** @description Send as `Authorization: Bearer <key>`, never in a URL. Shown once; the catalog keeps only its SHA-256. */
            key: string;
            api_key: components["schemas"]["ApiKey"];
            /** @description Rotation only: the old key, with its shortened expiry. */
            previous?: components["schemas"]["ApiKey"];
        };
        ApiKeyCreate: {
            /** @description Within the client's scopes; default, all of them. */
            scopes?: ("Policy.Propose" | "Recall.Request" | "Archive.Read" | "Archive.Adopt")[];
            /** @default 90 */
            expires_in_days?: number;
        };
        ApiKeyRotate: {
            /** @default 90 */
            expires_in_days?: number;
            /**
             * @description How long the old key keeps working.
             * @default 168
             */
            overlap_hours?: number;
        };
    };
    responses: {
        /** @description RFC 9457 problem details. */
        Problem: {
            headers: {
                [name: string]: unknown;
            };
            content: {
                "application/problem+json": components["schemas"]["Problem"];
            };
        };
    };
    parameters: {
        IdempotencyKey: string;
        /** @description Required for session callers; ignored for bearer callers. */
        CsrfToken: string;
        /** @description The resource's ETag (quoted entity_version). */
        IfMatch: string;
        Cursor: string;
        Limit: number;
        ShareId: components["schemas"]["Id"];
        ShareIdQuery: components["schemas"]["Id"];
        DirId: components["schemas"]["Id"];
        FileId: components["schemas"]["Id"];
        VersionId: components["schemas"]["Id"];
        RecallId: components["schemas"]["Id"];
        FilerId: components["schemas"]["Id"];
        RulesetVersion: number;
        DryRunId: components["schemas"]["Id"];
        AnnexId: components["schemas"]["Id"];
        DatasetId: components["schemas"]["Id"];
        ProposalId: components["schemas"]["Id"];
        SourceId: components["schemas"]["Id"];
        MigrationId: components["schemas"]["Id"];
        PlanId: components["schemas"]["Id"];
        WaveSeq: number;
        /** @description Closed for requests today; new backend kinds are added here additively. */
        BackendKind: "azure_blob" | "s3" | "sim";
    };
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    login: {
        parameters: {
            query?: {
                /** @description Same-origin path starting with "/". Anything else becomes "/". */
                return_to?: string;
                /** @description 1 forces prompt=login (re-authentication for WORM and legal holds). */
                reauth?: 0 | 1;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description To Entra's authorize endpoint. Sets __Host-omnifile_login (SameSite=Lax). */
            302: {
                headers: {
                    Location?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    authCallback: {
        parameters: {
            query: {
                code: string;
                state: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Signed in. Sets __Host-omnifile_session (HttpOnly; Secure; SameSite=Strict). */
            302: {
                headers: {
                    Location?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            400: components["responses"]["Problem"];
            401: components["responses"]["Problem"];
        };
    };
    getSession: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Signed in. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Session"];
                };
            };
            401: components["responses"]["Problem"];
        };
    };
    logout: {
        parameters: {
            query?: never;
            header?: {
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Signed out; cookie cleared. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        /** Format: uri */
                        logout_url: string;
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getMe: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The signed-in user. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Me"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    searchGroups: {
        parameters: {
            query?: {
                /** @description Case-insensitive prefix of display name or sAMAccountName, or an exact SID. */
                q?: string;
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Groups. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["GroupRef"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listShares: {
        parameters: {
            query?: {
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Shares the caller may see (portal-api.md 5.2). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Share"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    addShare: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["NewShare"];
            };
        };
        responses: {
            /** @description The share, with its checks (the Kerberos throughput note among them). */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Share"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getShare: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The share, with its checks and the caller's access. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Share"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    removeShare: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Removed. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        share_id: components["schemas"]["Id"];
                        /** @constant */
                        removed: true;
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    patchShareSettings: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ShareSettingsPatch"];
            };
        };
        responses: {
            /** @description Updated share. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Share"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    previewShareSettings: {
        parameters: {
            query?: never;
            header?: {
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ShareSettingsPatch"];
            };
        };
        responses: {
            /** @description Checks. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        checks: components["schemas"]["Check"][];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    putShareAccess: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ShareAccess"];
            };
        };
        responses: {
            /** @description Updated share. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Share"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    enableWorm: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WormEnable"];
            };
        };
        responses: {
            /** @description WORM is in its validation window; share.worm.lock_deadline says when it locks. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Share"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    cancelWorm: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    confirm_name: string;
                };
            };
        };
        responses: {
            /** @description WORM off. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Share"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    extendWorm: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    retention_days: number;
                };
            };
        };
        responses: {
            /** @description Updated share. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Share"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listChildren: {
        parameters: {
            query?: {
                /** @description Within the share's rewind depth, else 410 beyond_rewind_depth. */
                as_of?: string;
                cursor?: components["parameters"]["Cursor"];
                limit?: number;
            };
            header?: never;
            path: {
                dir_id: components["parameters"]["DirId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Children. Live rows are Entry; as_of rows are AsOfEntry. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        dir: components["schemas"]["DirHeader"];
                        /** Format: date-time */
                        as_of?: string | null;
                        items: (components["schemas"]["Entry"] | components["schemas"]["AsOfEntry"])[];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getFile: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description File or folder detail. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FileDetail"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listVersions: {
        parameters: {
            query?: {
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Versions. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Version"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getRecallOptions: {
        parameters: {
            query?: {
                /** @description Default the current content version. */
                version_id?: components["schemas"]["Id"];
            };
            header?: never;
            path: {
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Whether a restore is needed, and each priority's ETA and fee. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecallOptions"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    restoreVersion: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                file_id: components["parameters"]["FileId"];
                version_id: components["parameters"]["VersionId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["VersionRestoreRequest"];
            };
        };
        responses: {
            /** @description Queued. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VersionRestore"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    requestDownload: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                file_id: components["parameters"]["FileId"];
                version_id: components["parameters"]["VersionId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DownloadRequest"];
            };
        };
        responses: {
            /** @description Preparing, or needs_recall. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Download"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getVersionRestore: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                restore_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The restore. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VersionRestore"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getDownload: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                download_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The download; url is set when state is ready. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Download"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listRecalls: {
        parameters: {
            query?: {
                scope?: "mine" | "share" | "all";
                share_id?: components["parameters"]["ShareIdQuery"];
                state?: components["schemas"]["RecallState"];
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Recalls, newest first. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Recall"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    requestRecall: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RecallRequest"];
            };
        };
        responses: {
            /** @description Joined an active recall (joined = true). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Recall"];
                };
            };
            /** @description A new recall. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Recall"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getRecall: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                recall_id: components["parameters"]["RecallId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The recall. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Recall"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    previewPins: {
        parameters: {
            query?: never;
            header?: {
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PinPreviewRequest"];
            };
        };
        responses: {
            /** @description Per-item checks and totals. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PinPreview"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listPins: {
        parameters: {
            query?: {
                share_id?: components["parameters"]["ShareIdQuery"];
                active?: boolean;
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Pins on readable items. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Pin"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    createPins: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PinRequest"];
            };
        };
        responses: {
            /** @description Pinned, with any skipped items. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PinCreateResult"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    releasePin: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                pin_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Released. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Pin"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listHolds: {
        parameters: {
            query?: {
                share_id?: components["parameters"]["ShareIdQuery"];
                kind?: "keep_online" | "legal";
                /** @description The holds that apply to this file or folder (on it or on a folder above it). */
                file_id?: components["schemas"]["Id"];
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Active holds on readable items. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Hold"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    createHolds: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["HoldRequest"];
            };
        };
        responses: {
            /** @description Holds set, with any skipped items. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HoldCreateResult"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    releaseHold: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                hold_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Released. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Hold"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listDatasets: {
        parameters: {
            query?: {
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Datasets, with shares, the active policy's summary and pending proposals. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Dataset"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    addDataset: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    name: string;
                    backend_id: components["schemas"]["Id"];
                    edge_id: components["schemas"]["Id"];
                    /**
                     * @description single_account for a share that will be WORM (decision 31).
                     * @default two_account
                     * @enum {string}
                     */
                    storage_layout?: "two_account" | "single_account";
                    /**
                     * @description Where the dataset's blobs go in its backend: empty, or segments each ending in '/'.
                     * @default
                     */
                    key_prefix?: string;
                };
            };
        };
        responses: {
            /** @description The dataset, with no shares yet. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Dataset"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listBackends: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Every backend, by name. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        backends: components["schemas"]["Backend"][];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getActivePolicy: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                dataset_id: components["parameters"]["DatasetId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The active policy. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PolicyVersion"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listPolicyVersions: {
        parameters: {
            query?: {
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                dataset_id: components["parameters"]["DatasetId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Policy history, newest first. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["PolicyVersion"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    createPolicyVersion: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                dataset_id: components["parameters"]["DatasetId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PolicyCreate"];
            };
        };
        responses: {
            /** @description Active; the scheduler re-plans the dataset. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PolicyVersion"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    previewPolicy: {
        parameters: {
            query?: never;
            header?: {
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PolicyPreviewRequest"];
            };
        };
        responses: {
            /** @description The preview. An invalid body is valid=false with error checks, not an HTTP error. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PolicyPreview"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listProposals: {
        parameters: {
            query?: {
                dataset_id?: components["schemas"]["Id"];
                state?: components["schemas"]["ProposalState"];
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Proposals, newest first. Apps see only their own. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Proposal"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    submitProposal: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ProposalCreate"];
            };
        };
        responses: {
            /** @description Pending. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Proposal"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getProposal: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                proposal_id: components["parameters"]["ProposalId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The proposal, with preview and diff against the active policy. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Proposal"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    approveProposal: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                proposal_id: components["parameters"]["ProposalId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    note?: string;
                    /** @default false */
                    accept_stale?: boolean;
                };
            };
        };
        responses: {
            /** @description Approved. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        proposal: components["schemas"]["Proposal"];
                        policy: components["schemas"]["PolicyVersion"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    rejectProposal: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                proposal_id: components["parameters"]["ProposalId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    note: string;
                };
            };
        };
        responses: {
            /** @description Rejected. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Proposal"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    withdrawProposal: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                proposal_id: components["parameters"]["ProposalId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Withdrawn. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Proposal"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getPriceTable: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description Closed for requests today; new backend kinds are added here additively. */
                backend_kind: components["parameters"]["BackendKind"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The price table the preview uses (built-in or override). */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PriceTable"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    putPriceTable: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                /** @description Closed for requests today; new backend kinds are added here additively. */
                backend_kind: components["parameters"]["BackendKind"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PriceTable"];
            };
        };
        responses: {
            /** @description Stored. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PriceTable"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    deletePriceTableOverride: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                /** @description Closed for requests today; new backend kinds are added here additively. */
                backend_kind: components["parameters"]["BackendKind"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The built-in table, now in use. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PriceTable"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listAudit: {
        parameters: {
            query?: {
                share_id?: components["parameters"]["ShareIdQuery"];
                file_id?: components["schemas"]["Id"];
                /** @description user:<identity_id>, app:<client id>, edge:<edge_id> or system:<job>. */
                actor?: string;
                action?: string;
                from?: string;
                to?: string;
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Audit rows, newest first. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["AuditRecord"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    startSourceScan: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    backend_id: components["schemas"]["Id"];
                    bucket: string;
                    prefix: string;
                    /** @description Read this S3 Inventory instead of listing (s3_inventory or listing; S3 Metadata tables are read by the seed, not the scan). */
                    inventory?: components["schemas"]["SourceInventoryS3Inventory"] | components["schemas"]["SourceInventoryListing"] | null;
                    /** @default 2000000 */
                    max_objects?: number;
                    /** @default 300 */
                    max_seconds?: number;
                };
            };
        };
        responses: {
            /** @description Queued for the worker. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SourceScan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getSourceScan: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                scan_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The scan. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SourceScan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listSources: {
        parameters: {
            query?: {
                dataset_id?: components["schemas"]["Id"];
                state?: components["schemas"]["SourceState"];
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Sources, newest first. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Source"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    registerSource: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SourceCreate"];
            };
        };
        responses: {
            /** @description Registered; the worker seeds it. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Source"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    adoptData: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SourceCreate"];
            };
        };
        responses: {
            /** @description Registered (kind adopt); the worker seeds it. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Source"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getSource: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                source_id: components["parameters"]["SourceId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The source and its seeding progress. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Source"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    retrySource: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                source_id: components["parameters"]["SourceId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Seeding again. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Source"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listSourceBackends: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Every S3 backend an ArchiveAdmin configured, by name. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        backends: components["schemas"]["SourceBackend"][];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getFilesByPath: {
        parameters: {
            query?: {
                /** @description Required with path. */
                dataset_id?: components["schemas"]["Id"];
                /** @description Dataset-relative, starting with "/" (the root). A live entry. Give path or file_id. */
                path?: string;
                /** @description As an event names it; deleted entries too (deleted_at set). */
                file_id?: components["schemas"]["Id"];
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The entry; for a folder, a page of its live children by name. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        entry: components["schemas"]["PartnerFile"];
                        items: components["schemas"]["PartnerFile"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listRestores: {
        parameters: {
            query?: {
                dataset_id?: components["schemas"]["Id"];
                state?: "requested" | "in_progress" | "completed" | "expired" | "failed" | "cancelled";
                triggered_by?: "user_recall" | "migration_wave" | "api";
                /** @description Requested at or after this time. */
                since?: string;
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Restores. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["PartnerRestore"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getEvents: {
        parameters: {
            query?: {
                /** @description The `next` of a previous page; 0.0 (the default) for everything kept; now for the cursor of now. */
                after?: string;
                dataset_id?: components["schemas"]["Id"];
                /** @description Comma-separated kinds to keep (file.upsert, migration.cut_over, ...); unknown ones match nothing. Absent - every kind. */
                types?: string;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Events in commit order, and the cursor to read on from (never null). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["PartnerEvent"][];
                        next: string;
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getUsage: {
        parameters: {
            query?: {
                dataset_id?: components["schemas"]["Id"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The latest snapshot (computed_at null before the first). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Usage"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getActivity: {
        parameters: {
            query?: {
                days?: number;
                dataset_id?: components["schemas"]["Id"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The window's activity. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Activity"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getLiveActivity: {
        parameters: {
            query?: {
                /** @description The previous answer's `cursor`. Absent, or older than 15 minutes, reads the last 15 minutes. */
                since?: string;
                /** @description With `since`, hold the request up to this many seconds until something newer exists. */
                wait?: number;
                limit?: number;
                /** @description Comma-separated; default all of upload, recall, tier, heartbeat, migration, annex. Every filter applies before the limit. */
                kind?: string;
                edge_id?: components["schemas"]["Id"];
                share_id?: components["schemas"]["Id"];
                /** @description Only what this person did. Sessions only; an application gets 400. */
                identity_id?: components["schemas"]["Id"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The events and the live numbers. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LiveActivity"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listEdges: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The edges, by name. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        edges: components["schemas"]["Edge"][];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    addEdge: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    name: string;
                };
            };
        };
        responses: {
            /** @description The edge and its bundle. The bundle is shown once. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EnrolmentBundle"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getEdgeAppliance: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The appliance's images and enrolment. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EdgeAppliance"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    newEdgeBundle: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                edge_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The new bundle. It is shown once. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EnrolmentBundle"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    revokeEdge: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                edge_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Revoked (or already was). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        edge_id: components["schemas"]["Id"];
                        name: string;
                        /** @enum {string} */
                        state: "revoked";
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listLocations: {
        parameters: {
            query?: {
                dataset_id?: components["schemas"]["Id"];
                state?: "pending" | "copying" | "verifying" | "verified" | "cut_over" | "failed" | "cancelled";
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Migration prefixes, newest first. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Location"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    requestLocation: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LocationCreate"];
            };
        };
        responses: {
            /** @description Accepted; the worker copies, verifies and cuts over. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Location"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getLocation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                migration_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The prefix's progress. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Location"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listMigrations: {
        parameters: {
            query?: {
                dataset_id?: components["schemas"]["Id"];
                state?: "pending" | "copying" | "verifying" | "verified" | "cut_over" | "failed" | "cancelled";
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Migrations, newest first (those started through /v1/locations too). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Migration"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    startMigration: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MigrationCreate"];
            };
        };
        responses: {
            /** @description Accepted; the worker copies, verifies and cuts over. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Migration"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getMigration: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The migration's progress. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Migration"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    cutoverMigration: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Cutover asked; it happens once the prefix is verified. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Migration"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    cancelMigration: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Cancelled. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Migration"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    retryMigration: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Pending again. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Migration"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    reportMigrationRun: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MigrationRunReport"];
            };
        };
        responses: {
            /** @description Recorded. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Migration"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    excludeMigrationObjects: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MigrationExclude"];
            };
        };
        responses: {
            /** @description Excluded. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Migration"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getMigrationTombstones: {
        parameters: {
            query?: {
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description A page of the report. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TombstoneReport"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getMigrationConsole: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The console. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationConsole"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listMigrationAgents: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Agents and jobs. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationAgents"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getMigrationWork: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The drill-down. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationWork"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getMigrationStuckCopies: {
        parameters: {
            query: {
                kind: "stranded" | "no_checksum";
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description A page of the objects, by key. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["StuckCopies"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    rerunMigration: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Asked. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Migration"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    restoreMigrationAgain: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                migration_id: components["parameters"]["MigrationId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Asked. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Migration"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listMigrationPlans: {
        parameters: {
            query?: {
                dataset_id?: components["schemas"]["Id"];
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Plans, newest first (an app sees its own). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["MigrationPlan"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    createMigrationPlan: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MigrationPlanCreate"];
            };
        };
        responses: {
            /** @description The plan. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationPlan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    splitMigrationSource: {
        parameters: {
            query: {
                source_id: components["schemas"]["Id"];
                by?: "bytes" | "count";
                waves?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The split. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationSplit"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getMigrationPlan: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                plan_id: components["parameters"]["PlanId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The plan and its waves. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationPlan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    approveMigrationPlan: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                plan_id: components["parameters"]["PlanId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PlanDecision"];
            };
        };
        responses: {
            /** @description Approved. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationPlan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    rejectMigrationPlan: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                plan_id: components["parameters"]["PlanId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PlanDecision"];
            };
        };
        responses: {
            /** @description Rejected. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationPlan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    cancelMigrationPlan: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                plan_id: components["parameters"]["PlanId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Cancelled. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationPlan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    rescheduleMigrationWave: {
        parameters: {
            query?: never;
            header?: {
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                plan_id: components["parameters"]["PlanId"];
                seq: components["parameters"]["WaveSeq"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WavePatch"];
            };
        };
        responses: {
            /** @description The plan as it is now. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationPlan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    cutoverMigrationWave: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                plan_id: components["parameters"]["PlanId"];
                seq: components["parameters"]["WaveSeq"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Asked. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationPlan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    retryMigrationWave: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                plan_id: components["parameters"]["PlanId"];
                seq: components["parameters"]["WaveSeq"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Done. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationPlan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    cancelMigrationWave: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                plan_id: components["parameters"]["PlanId"];
                seq: components["parameters"]["WaveSeq"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Done. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MigrationPlan"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    createRecallBatch: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RecallBatchCreate"];
            };
        };
        responses: {
            /** @description Accepted; small batches are already planned (ETA and cost), large ones are planned by the worker. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecallBatch"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getRecallBatch: {
        parameters: {
            query?: {
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                batch_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The batch, with items and next_cursor. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecallBatch"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getFileJourney: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The file's journey. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FileJourney"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getShareMap: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The map. scope: all (counts only) for ArchiveAdmin and DataSteward, readable for everyone else. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ShareMap"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listShareMapFiles: {
        parameters: {
            query?: {
                /** @description What the edge holds: resident (whole), stub (stub or being filled), arriving (landing or uploading), remote (never local). */
                edge?: "resident" | "stub" | "arriving" | "remote";
                /** @description The logical tier of the primary copy. */
                tier?: "hot" | "warm" | "cold" | "archive" | "smart";
                /** @description With tier, the dataset's home backend or its migration source. */
                role?: "home" | "source";
                /** @description Files with a recall in flight. */
                recalling?: "true";
                cursor?: components["parameters"]["Cursor"];
                limit?: number;
            };
            header?: never;
            path: {
                share_id: components["parameters"]["ShareId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Readable files, by file_id. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["MapFile"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    healthz: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Liveness. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        /** @enum {string} */
                        status: "ok";
                    };
                };
            };
        };
    };
    getStorageSettings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Every backend, by id. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["StorageSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    addStorageBackend: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BackendDraft"];
            };
        };
        responses: {
            /** @description A backend of that name already holds these answers (unchanged), or was brought to them (updated). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BackendChange"];
                };
            };
            /** @description Added. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BackendChange"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    editStorageBackend: {
        parameters: {
            query?: never;
            header?: {
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                backend_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BackendDraft"];
            };
        };
        responses: {
            /** @description Updated or unchanged. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BackendChange"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    checkStorageBackend: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                backend_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The checks, run now. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BackendChecks"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAwsSettings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description What the deployment configured. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AwsSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAwsTrustPolicy: {
        parameters: {
            query: {
                role_arn: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The trust policy and the steps. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AwsTrustPolicy"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    testAwsRole: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    role_arn: string;
                };
            };
        };
        responses: {
            /** @description Queued for the worker. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SettingsTest"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getIdentitySettings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The tenant's settings, read now. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["IdentitySettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    whoAmI: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The caller. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WhoAmI"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getTeamsSettings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Teams, as configured. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TeamsSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getTeamsPackage: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The zip. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/zip": string;
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    sendTestCard: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Queued for the worker. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SettingsTest"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getEmailSettings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Email, as configured. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EmailSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    sendTestEmail: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Queued for the worker. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SettingsTest"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getSettingsTest: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                test_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The test. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SettingsTest"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getStorageMoverSettings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Storage Mover, read now. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["StorageMoverSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAzureSettings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The connection, read now. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AzureSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    startAzureSignIn: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    /** @enum {string} */
                    purpose: "list" | "grant" | "remove";
                    /** @description remove only: the grants to remove. */
                    grant_ids?: components["schemas"]["Id"][];
                };
            };
        };
        responses: {
            /** @description Go to authorize_url (a top-level navigation). Sets __Host-omnifile_azure (SameSite=Lax). */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AzureSignIn"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    azureConnectCallback: {
        parameters: {
            query?: {
                code?: string;
                state?: string;
                error?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Back to /settings/azure?op=<op_id>. The operation's outcome is in GET /v1/settings/azure (last_op). */
            302: {
                headers: {
                    Location?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            400: components["responses"]["Problem"];
            401: components["responses"]["Problem"];
        };
    };
    putAzureChoice: {
        parameters: {
            query?: never;
            header?: {
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    subscription_id: string;
                    resource_groups: string[];
                    mover_resource_group?: string | null;
                };
            };
        };
        responses: {
            /** @description The connection, with the new plan. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AzureSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    discoverAzure: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description The connection, with what was found. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AzureSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    confirmAzure: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    storage_accounts: string[];
                    virtual_networks?: string[];
                    private_endpoints?: string[];
                };
            };
        };
        responses: {
            /** @description The connection, confirmed. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AzureSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAwsConnect: {
        parameters: {
            query?: {
                region?: string;
                deployment_id?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description What to launch. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AwsConnect"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAwsConnectTemplate: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The template. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/yaml": string;
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    fetchAwsConnect: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    account_id: string;
                    region: string;
                    deployment_id: string;
                    /** @description With private networking: the stack's interface endpoint outputs (https://<dns>). */
                    endpoints?: {
                        s3?: string;
                        sts?: string;
                        sqs?: string;
                    };
                };
            };
        };
        responses: {
            /** @description Queued for the worker. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CloudTask"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    parseAwsOutputs: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    text: string;
                };
            };
        };
        responses: {
            /** @description What the outputs say. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AwsConnectEntry"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    confirmAwsConnect: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    account_id: string;
                    region: string;
                    deployment_id: string;
                    outputs: {
                        [key: string]: string;
                    };
                    buckets: string[];
                };
            };
        };
        responses: {
            /** @description Recorded. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AwsConnection"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    deployStorageMover: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    name: string;
                    project: string;
                    location: string;
                };
            };
        };
        responses: {
            /** @description Queued for the worker. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CloudTask"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getCloudTask: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The task. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CloudTask"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAlertSettings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The rules. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AlertSettings"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getPriceDisplay: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The setting. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PriceDisplay"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    putPriceDisplay: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    show_to: ("ArchiveAdmin" | "DataSteward" | "others")[];
                };
            };
        };
        responses: {
            /** @description Stored. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PriceDisplay"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    searchPeople: {
        parameters: {
            query: {
                /** @description A prefix of the name or user principal name, case-insensitive. */
                q: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Up to 20 people, by name. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["ApiClientOwner"][];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listApiClients: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The clients, by name, and the setup. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["ApiClient"][];
                        setup: components["schemas"]["ApiClientSetup"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    registerApiClient: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApiClientCreate"];
            };
        };
        responses: {
            /** @description Registered and enabled. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiClient"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getApiClient: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                api_client_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The client. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiClient"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    editApiClient: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                api_client_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApiClientPatch"];
            };
        };
        responses: {
            /** @description The client after the change. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiClient"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    revokeApiClient: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                api_client_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Revoked (or already was). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiClient"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    createApiKey: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                api_client_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApiKeyCreate"];
            };
        };
        responses: {
            /** @description The key. Copy it now; it is never shown again. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiKeyIssued"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    rotateApiKey: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                api_client_id: components["schemas"]["Id"];
                key_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApiKeyRotate"];
            };
        };
        responses: {
            /** @description The new key, shown once, and the old key's new expiry. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiKeyIssued"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    revokeApiKey: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                api_client_id: components["schemas"]["Id"];
                key_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Revoked (or already was). */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApiKey"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listFilers: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Filers by name. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Filer"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    addFiler: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FilerCreate"];
            };
        };
        responses: {
            /** @description Registered. */
            201: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Filer"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getFiler: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                filer_id: components["parameters"]["FilerId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The filer. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Filer"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    patchFiler: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                filer_id: components["parameters"]["FilerId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    placement_sid?: string | null;
                    access?: {
                        limited: boolean;
                        groups?: string[];
                    };
                    /**
                     * @description Decision 64: an ArchiveAdmin acknowledges that the filer's NAS shows no ACLs, so access there rests on mode bits alone; group-readable placements and share copies of its files go on. Audited (`filer.acls_hidden`). Only while the filer is flagged; a mount report that the ACLs show clears the flag and this with it.
                     * @enum {boolean}
                     */
                    acls_hidden_acknowledged?: true;
                };
            };
        };
        responses: {
            /** @description The filer. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Filer"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getFilerHealth: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                filer_id: components["parameters"]["FilerId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Health. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FilerHealth"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listAnnexes: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Annexes by name. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["Annex"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    addAnnex: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AnnexCreate"];
            };
        };
        responses: {
            /** @description Created; the edge's annex unit starts on its next poll. */
            201: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Annex"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAnnex: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The annex, with its counts and filer. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexDetail"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    patchAnnex: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AnnexPatch"];
            };
        };
        responses: {
            /** @description Changed. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Annex"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listAnnexRulesets: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Versions. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["AnnexRuleset"][];
                        next_cursor: string | null;
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    proposeAnnexRuleset: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AnnexRulesetPropose"];
            };
        };
        responses: {
            /** @description Proposed. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexRuleset"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAnnexRuleset: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                annex_id: components["parameters"]["AnnexId"];
                version: components["parameters"]["RulesetVersion"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The version. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexRulesetDetail"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    approveAnnexRulesetVersion: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
                version: components["parameters"]["RulesetVersion"];
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": components["schemas"]["AnnexDecision"];
            };
        };
        responses: {
            /** @description Running. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Annex"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    rejectAnnexRulesetVersion: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
                version: components["parameters"]["RulesetVersion"];
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": components["schemas"]["AnnexDecision"];
            };
        };
        responses: {
            /** @description Rejected. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Annex"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    createAnnexDryRun: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AnnexDryRunCreate"];
            };
        };
        responses: {
            /** @description Asked for. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexDryRun"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAnnexDryRun: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                annex_id: components["parameters"]["AnnexId"];
                run_id: components["parameters"]["DryRunId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The preview. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexDryRun"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    previewAnnexReturns: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AnnexDryRunCreate"];
            };
        };
        responses: {
            /** @description The preview. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexReturnPreview"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getReturnTargets: {
        parameters: {
            query?: {
                /** @description API clients only, the person the file is for. */
                requester?: components["schemas"]["Sid"];
            };
            header?: never;
            path: {
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The targets. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReturnTargets"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    requestPlacement: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PlacementCreate"];
            };
        };
        responses: {
            /** @description Joined an open placement. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexPlacement"];
                };
            };
            /** @description Requested. */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexPlacement"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getPlacement: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                placement_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The placement. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexPlacement"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    cancelPlacement: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                placement_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The placement, cancelled. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexPlacement"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    abandonPlacement: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                placement_id: components["schemas"]["Id"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The placement, ended. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexPlacement"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    confirmAnnexRemovals: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": Record<string, never>;
            };
        };
        responses: {
            /** @description Confirmed; removals are live unless something pauses them. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Annex"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listAnnexRemovals: {
        parameters: {
            query?: {
                state?: "pending" | "done" | "cancelled" | "stuck";
                cursor?: string;
                limit?: number;
            };
            header?: never;
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Removals. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["AnnexRemoval"][];
                        next_cursor: string | null;
                        live: boolean;
                        why_not?: string | null;
                        /** @description What the dry run says the running rules would remove: files, bytes, and up to 20 sample paths. ArchiveAdmins and apps only (null for anyone else: counts of an unfiltered listing). */
                        would_remove?: {
                            files: number;
                            /** Format: int64 */
                            bytes: number;
                            samples: string[];
                        } | null;
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listAnnexEvents: {
        parameters: {
            query?: {
                /** @description Event kinds, comma-separated. */
                kind?: string;
                since?: string;
                until?: string;
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Events. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["AnnexEvent"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    listAnnexFiles: {
        parameters: {
            query?: {
                /** @description A path prefix under the annex's root. */
                path?: string;
                state?: "present" | "gone" | "purged";
                cursor?: components["parameters"]["Cursor"];
                limit?: components["parameters"]["Limit"];
            };
            header?: never;
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Files. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        items: components["schemas"]["AnnexFile"][];
                        next_cursor: components["schemas"]["NextCursor"];
                    };
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAnnexTimeline: {
        parameters: {
            query?: {
                days?: number;
            };
            header?: never;
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description One row per day, oldest first. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexTimeline"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAnnexProblems: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                annex_id: components["parameters"]["AnnexId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Problems, newest first. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexProblems"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    purgeAnnexFile: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Requested; the worker purges it. */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexFile"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    clearAnnexFileBreaker: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The file. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexFile"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    stopAnnexFileCopy: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The file. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexFile"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    resumeAnnexFileCopy: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The file. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexFile"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    acknowledgeAnnexFileCopyRefused: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": components["parameters"]["IdempotencyKey"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path: {
                annex_id: components["parameters"]["AnnexId"];
                file_id: components["parameters"]["FileId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description The file. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexFile"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    getAnnexDefaults: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Defaults. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexDefaults"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
    putAnnexDefaults: {
        parameters: {
            query?: never;
            header: {
                /** @description The resource's ETag (quoted entity_version). */
                "If-Match": components["parameters"]["IfMatch"];
                /** @description Required for session callers; ignored for bearer callers. */
                "X-CSRF-Token"?: components["parameters"]["CsrfToken"];
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AnnexSettingsPut"];
            };
        };
        responses: {
            /** @description Stored. */
            200: {
                headers: {
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnnexDefaults"];
                };
            };
            default: components["responses"]["Problem"];
        };
    };
}
