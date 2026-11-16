# Contributing to OmniFile Gateway

**Outside contributions are not accepted yet.** OmniFile Gateway is licensed under the Apache License 2.0
(decision 86), and this file will say how changes are submitted and reviewed once the project takes them.
Until then, please don't open pull requests.

You are welcome to:

- **report a bug or ask a question** in an issue. Say what you ran, what you expected and what happened,
  and leave out anything confidential (names of your files, users or networks);
- **report a security problem privately**, never in a public issue: write to AiLuxis Research and say
  which part (edge filer, service, relay, appliance) and how to reproduce it;
- **ask for the GPL and LGPL source** of an appliance release through the issues of the sources repository
  the release's written offer names (decision 46).

If you use or change OmniFile Gateway, keep its copyright and attribution notices, including `NOTICE`:
the Apache License requires it of anyone who redistributes the work or a work built on it.

## For the project's own developers

Start with `AGENTS.md`: it is the anchor document, with the decisions that are closed. Then:

- branch from `main`; never rebase or force-push a branch that has been pushed (`git merge origin/main`
  to catch up);
- there is no CI on GitHub (decision 45): run the lanes your change touches and give their results in the
  pull request: the service (`cd service && uv run pytest -q`), the portal (`cd portal && npm test`), the
  edge (`cd edge && cargo test` and `cargo clippy --all-targets -- -D warnings`), the Bicep
  (`deploy/azure/check.sh`), the customer guide (`docs/guide/check.sh` and `docs/guide/test.sh`), and the
  e2e scripts in `tests/e2e/` on the dev VMs;
- every bug fix comes with a test that fails without it;
- add each new direct dependency to `LICENSES.md`, with its licence. No AGPL dependency, ever, and GPL code
  only as a separate process (`AGENTS.md`, "Critical constraints");
- new code is BSL 1.1, except in `fsal/` (LGPL-3.0) and `samba-vfs/` (GPL-3.0-or-later).
