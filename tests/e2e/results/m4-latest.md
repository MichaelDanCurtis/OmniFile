# M4 exit test (sim, fakes, real relay, Playwright)

- When: 2026-11-09 03:30 UTC
- Revision: `7b1747a9-dirty` (`7b1747a987141665af73f088774e6dbb9ea03e92`)
- Machine: Darwin 27.0.0 (arm64)

### Service side (tests/e2e/m4.py): 15/15 passed

- PASS 1. stub read by an AD user -> Teams card follows the recall to available: recall 2: card states ['requested', 'rehydrating', 'downloading', 'available'] in 4 versions of one activity; recall_state_log ['requested', 'rehydrating', 'downloading', 'available'] (23.2 s)
- PASS 2. Keep online through the relay -> 14-day keep_online hold: hold 1: keep_online, 14 days, set by the clicker; card updated (9.9 s)
- PASS 2b. a replayed click is refused by the relay: relay: application/vnd.microsoft.activity.message ({"statusCode": 200, "type": "application/vnd.microsoft.activity.message", "value...) (0.0 s)
- PASS 3a. a second stub read (line_2001.segy) -> card with Keep online: recall 3, card rehydrating (2.1 s)
- PASS 3b. a stranger's click is refused (relay and api) and audited: relay 403 (wrong_recipient); api 403 not_intended_recipient, audited (0.0 s)
- PASS 3c. a click by a user who lost read access is refused and audited: relay forwarded, api 403 forbidden (no read), audited; no hold (0.1 s)
- PASS 3d. an expired intent is refused (relay and api) and audited: relay 400; api 410 intent_expired, audited (0.1 s)
- PASS 4. injected restore failure -> Recall on the failed card -> available: recall 3 failed; Recall queued 4 (notification 18); the same card followed it to available (23.8 s)
- PASS 5. Bot Connector 403 -> email fallback via Graph sendMail: recall 5: Bot Connector 403 twice -> teams notification fell_back, email 'Recalling line_3001.segy' to reader@contoso.test (sent) (2.1 s)
- PASS 6. restore choices (decision 34): restored, name_taken, timestamped, overwrite: restored -> survey-restored.txt; again -> 409 name_taken; timestamped -> survey-restored-2026-11-11-033019Z.txt; overwrite -> new current version 15 (4 versions) (5.7 s)
- PASS 7a. policy preview cost changes with the policy; a saved version is active and re-planned: first year per TB 116.86 -> 77.38 USD; saved v5 active; next action 2026-12-09 03:28:46.154543+00:00 -> 2026-11-11 03:30:23.166478+00:00 (0.1 s)
- PASS 7b. partner proposal changes nothing until another ArchiveAdmin approves: partner proposal 3 stored, not applied; app can't approve (401); self-approval 403; another ArchiveAdmin approved -> v6 (0.1 s)
- PASS 8. a reader's listings follow their AD groups: reader sees ['survey']; restricted/ and payroll.csv are 404; owner sees ['restricted', 'survey'] (0.1 s)
- PASS 9. tier pins, rewind as of a date, rewind depth, share access screen: pin cold (Owner), reader's pin skipped (forbidden); as-of listing shows the first version; depth 30 -> 14 days, 20 days back 410; access PUT audited (0.2 s)
- PASS 10. WORM enable: typed share name, re-authentication, lock deadline: wrong name 422; stale sign-in 401 reauth_required; after re-auth enabled (test window, locks 2026-11-11T05:30:23.636Z); again 409 (0.1 s)

### Browser (portal/e2e, Playwright chromium): 10/10 passed

- PASS ops.e2e.mjs: an ArchiveAdmin reads the activity dashboard from the catalog (1.4 s)
- PASS ops.e2e.mjs: an ArchiveAdmin sees sources and moving prefixes on the Migrations page (0.6 s)
- PASS ops.e2e.mjs: a Reader has neither page (1.0 s)
- PASS policy.e2e.mjs: an ArchiveAdmin edits a policy, sees the cost update, and saves it (1.1 s)
- PASS proposals.e2e.mjs: a DataSteward proposes a change and an ArchiveAdmin approves it (1.8 s)
- PASS proposals.e2e.mjs: an ArchiveAdmin's own proposal needs a second ArchiveAdmin (0.5 s)
- PASS reader.e2e.mjs: a Reader browses only what their AD groups allow (0.4 s)
- PASS reader.e2e.mjs: a Reader recalls a file ahead of time and watches the four steps (16.5 s)
- PASS restore.e2e.mjs: an Owner restores an old version as a copy; a taken name is refused (4.0 s)
- PASS worm.e2e.mjs: an ArchiveAdmin turns on WORM after re-authenticating (1.2 s)
