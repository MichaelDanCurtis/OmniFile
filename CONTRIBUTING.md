# Contributing to OmniFile Gateway

Contributions are welcome. OmniFile Gateway is licensed under the Apache License 2.0 (decision 86), and
what you contribute is licensed under it too (section 5 of the licence). There is no agreement to sign.

## Signing off your commits

Every commit in a pull request carries a sign-off: a line at the end of its message, with your real name
and an email address you can be reached at.

```
Signed-off-by: Jane Doe <jane@example.com>
```

`git commit -s` adds it. By signing off you certify the Developer Certificate of Origin, version 1.1
(<https://developercertificate.org>): in short, that you wrote the change or otherwise have the right to
submit it under the project's licence, and that you understand the contribution and the sign-off are
public and kept. A pull request with a commit that isn't signed off can't be merged; `git commit --amend
-s` or `git rebase --signoff origin/main` on your own branch fixes it before review.

## Making a change

- Open an issue first for anything larger than a fix, so we can agree on the approach before you write it.
  `AGENTS.md` lists the decisions that are closed; a change that reverses one won't be merged.
- Keep a pull request to one change, with a test that fails without it.
- Run the lanes your change touches and give their results in the pull request (below): there is no CI on
  GitHub.
- Add each new direct dependency to `LICENSES.md`, with its licence. No AGPL, and GPL code only as a
  separate process.
- Code in `fsal/` is LGPL-3.0 and code in `samba-vfs/` GPL-3.0-or-later; a change there is contributed under
  that licence.

You are also welcome to:

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
- new code is Apache-2.0, except in `fsal/` (LGPL-3.0) and `samba-vfs/` (GPL-3.0-or-later);
- before merging an outside pull request, check every commit is signed off:
  `git log --format='%h %(trailers:key=Signed-off-by,valueonly,separator=%x2C )' origin/main..HEAD`
  prints a name after each hash.
