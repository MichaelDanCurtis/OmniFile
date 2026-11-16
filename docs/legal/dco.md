**DRAFT for counsel review. Not legal advice. Not in effect.**

# The alternative: a Developer Certificate of Origin

## What it is

The Developer Certificate of Origin (DCO), version 1.1, published at <https://developercertificate.org>,
is a short statement a contributor makes about each change, by adding a line to its commit message:

```
Signed-off-by: Jane Doe <jane@example.com>
```

(`git commit -s` adds it.) In it the contributor certifies, in substance, that they wrote the change or
otherwise have the right to submit it under the project's open source licence, that they are submitting
it under that licence, and that they understand the contribution and their sign-off are public and kept
indefinitely. There is nothing to sign up front, and no list of contributors to keep.

If it is adopted, the DCO's own text is used word for word (its licence allows verbatim copies only), and
`CONTRIBUTING.md` links to it and requires the sign-off on every commit.

## The trade-off for OmniFile Gateway

| | Contributor licence agreement (`individual-cla.md`, `entity-cla.md`) | DCO |
|---|---|---|
| Friction for a contributor | Higher: an agreement to read and sign once, and for an employee, often their employer's signature. | Low: one line per commit. |
| What we can do with a contribution | License it under any terms, including commercial licences and future licence changes (section 2 of the drafts). | Only distribute it under the licence it came in under: the DCO certifies the right to submit under "the open source license indicated in the file"; it gives us no further rights. |
| Fit with the BSL 1.1 | Works: we can sell commercial licences to code that includes contributions. | Unclear at best. The BSL is not an open source licence, which is what the DCO's text speaks of, and a contribution under the BSL alone would not let us sell a commercial licence covering it. |
| Patent licence | Explicit (section 3). | None beyond what the project's licence itself grants. |
| Records | Signed agreements, and Schedule A for entities. | The sign-off lines in the git history. |

**Our reading, for counsel to confirm or correct:** decision 25 needs a CLA. AiLuxis Research sells
commercial licences, which requires it to hold, or be licensed, all the rights in every contribution, and
the DCO by itself doesn't give those rights. A DCO would fit only if the project stopped selling
commercial licences, or if each contribution also came with a separate licence grant in the
CONTRIBUTING terms (a "DCO plus licence grant" arrangement). Whether that hybrid would be enforceable,
and whether it is simpler than a CLA in practice, is question 1.4 in `questions-for-counsel.md`.

A DCO sign-off can still be required alongside a CLA, as a per-commit record of origin. That costs
contributors little and helps trace where each change came from.
