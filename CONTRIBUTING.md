# Contributing to OmniFile Gateway

**Outside contributions are not accepted yet.** OmniFile Gateway is licensed under the Business Source
License 1.1 and AiLuxis Research sells commercial licences to it, so every contribution must come with
rights that allow that. That needs a contributor licence agreement (decision 25), which is still a draft
for counsel's review (`docs/legal/`). Until one is in effect, please don't open pull requests: we can't
merge them, and we would rather not read code we may have to write again without looking at it.

You are welcome to:

- **report a bug or ask a question** in an issue. Say what you ran, what you expected and what happened,
  and leave out anything confidential (names of your files, users or networks);
- **report a security problem privately**, never in a public issue: write to AiLuxis Research and say
  which part (edge filer, service, relay, appliance) and how to reproduce it;
- **ask for the GPL and LGPL source** of an appliance release through the issues of the sources repository
  the release's written offer names (decision 46).

When the agreement is in effect, this file will say how to sign it and how changes are reviewed.

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
