"""Offline tests of publish_sources.py and the offer text (decision 46).

    python3 -m unittest discover -s appliance/source-offer

Nothing here reaches GitHub. The dry runs are given a fake gh that records any call, and must make none. The
--execute tests run the same fake by its absolute path (never `gh` from PATH), against a made-up repository, to
check the commands it would send; the real publish is the release task.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import publish_sources as ps  # noqa: E402
import source_offer as so  # noqa: E402

REPO = "example-owner/omnifile-gateway-sources"
RELEASE = "1.2.0"
GENERATED = int(datetime(2026, 10, 21, 12, 0, tzinfo=UTC).timestamp())


def make_bundle(root: Path, *, repo: str = REPO, complete: bool = True) -> Path:
    """A small bundle as source_offer.py writes it: three trees, the manifest, the sums and the offer."""
    b = root / "bundle"
    files = {
        "ubuntu/acl_2.3.2-2/acl_2.3.2.orig.tar.xz": os.urandom(3000),
        "ubuntu/acl_2.3.2-2/acl_2.3.2-2.dsc": b"Format: 3.0 (quilt)\n",
        "ubuntu/samba_2:4.23.6+dfsg-1ubuntu2.2/samba_4.23.6+dfsg.orig.tar.xz": os.urandom(5000),
        "upstream/samba-4.23.6.tar.gz": os.urandom(2000),
        "omnifile/fsal.tar.gz": os.urandom(1000),
        "omnifile/" + "x" * 120 + ".tar.gz": b"a long name\n",
    }
    for rel, data in files.items():
        (b / rel).parent.mkdir(parents=True, exist_ok=True)
        (b / rel).write_bytes(data)
    (b / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(d).hexdigest()}  {r}\n" for r, d in sorted(files.items()))
    )
    manifest = {
        "release": RELEASE,
        "snapshot": "20261021T000000Z",
        "generated": "2026-10-21T12:00:00Z",
        "complete": complete,
        "sources_repo": repo if so.repo_is_set(repo) else "",
        "release_tag": so.release_tag(RELEASE),
    }
    (b / "MANIFEST.json").write_text(json.dumps(manifest))
    (b / "packages.tsv").write_text("binary\tversion\tsource\tsource_version\n")
    sources = [so.Source("acl", "2.3.2-2", "ubuntu", files=[{"size": 3000}])]
    so.write_offer(b, RELEASE, "20261021T000000Z", repo, sources)
    so.mark_draft(b / "SOURCE-OFFER.md", complete, repo)
    return b


class FakeGh:
    """A stand-in for gh: records its argv and keeps one release's assets in a JSON file, as GitHub would."""

    SCRIPT = """#!/usr/bin/env python3
import json, os, sys
log, state_path = sys.argv[0] + ".calls", sys.argv[0] + ".state"
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\\n")
state = json.load(open(state_path))
if args[0] == "api":
    print(json.dumps(state["repo"]))
    sys.exit(0)
verb = args[1]
files = args[3:args.index("--repo")] if verb in ("create", "upload") else []
if verb == "view":
    if not state["exists"]:
        print("release not found", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"assets": [{"name": n, "size": s} for n, s in state["assets"].items()]}))
elif verb == "create":
    state["exists"] = True
    state["assets"] = {os.path.basename(f): os.path.getsize(f) for f in files}
elif verb == "upload":
    state["assets"].update({os.path.basename(f): os.path.getsize(f) for f in files})
elif verb == "delete-asset":
    state["assets"].pop(args[3])
json.dump(state, open(state_path, "w"))
"""

    PUBLIC = {"visibility": "public", "private": False, "has_issues": True}

    def __init__(self, root: Path, assets: dict[str, int] | None, repo: dict | None = None) -> None:
        """assets None: no such release yet; else the release exists with these assets (name: size).

        repo is what `gh api repos/<repo>` answers; by default a public repository with Issues on.
        """
        self.path = root / "fake-gh"
        self.path.write_text(self.SCRIPT)
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self.log = Path(f"{self.path}.calls")
        self.state = Path(f"{self.path}.state")
        state = {
            "exists": assets is not None,
            "assets": assets or {},
            "repo": self.PUBLIC if repo is None else repo,
        }
        self.state.write_text(json.dumps(state))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assets(self) -> dict[str, int]:
        return json.loads(self.state.read_text())["assets"]


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ps.main(argv)
    return rc, out.getvalue(), err.getvalue()


class Offer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_the_offer_names_the_release_and_the_issues(self) -> None:
        text = (make_bundle(self.tmp) / "SOURCE-OFFER.md").read_text()
        self.assertIn(f"https://github.com/{REPO}/releases/tag/v{RELEASE}", text)
        self.assertIn(f"https://github.com/{REPO}/issues", text)
        self.assertIn(f"omnifile-edge-{RELEASE}-sources-*.tar", text)
        self.assertNotIn("DRAFT", text)
        self.assertNotIn("{{", text)

    def test_without_a_sources_repository_the_offer_is_a_draft(self) -> None:
        text = (
            make_bundle(self.tmp, repo=so.SOURCES_REPO_PLACEHOLDER) / "SOURCE-OFFER.md"
        ).read_text()
        self.assertTrue(text.startswith("> **DRAFT: the sources repository is not set"))

    def test_an_extra_address_is_added_to_the_issues(self) -> None:
        b = make_bundle(self.tmp)
        so.write_offer(
            b, RELEASE, "20261021T000000Z", REPO, [], contact="AiLuxis Research, Source Requests"
        )
        text = (b / "SOURCE-OFFER.md").read_text()
        self.assertIn("Or write to:\n\nAiLuxis Research, Source Requests\n\nName the release", text)


class DryRun(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.gh = FakeGh(self.tmp, None)

    def dry(self, bundle: Path, *extra: str) -> tuple[int, str, str]:
        rc, out, err = run(
            ["--bundle", str(bundle), "--repo", REPO, "--gh", str(self.gh.path), *extra]
        )
        self.assertEqual(self.gh.calls(), [], "a dry run must not run gh")
        return rc, out, err

    def test_a_complete_bundle_plans_the_release_and_runs_nothing(self) -> None:
        rc, out, err = self.dry(make_bundle(self.tmp))
        self.assertEqual(rc, 0, err)
        self.assertIn(f"DRY RUN (nothing runs gh; --execute publishes): release v{RELEASE}", out)
        self.assertIn(f"omnifile-edge-{RELEASE}-sources-01.tar: 6 files", out)
        self.assertIn(f"release create v{RELEASE} <6 assets> --repo {REPO}", out)
        self.assertIn(f"api repos/{REPO}`: visibility public (not private or internal)", out)
        self.assertIn(f"release upload v{RELEASE} <6 assets> --repo {REPO} --clobber", out)

    def test_a_small_volume_size_splits_whole_files_across_archives(self) -> None:
        b = make_bundle(self.tmp)
        sums = ps.load_bundle(b, REPO, None)[2]
        vols = ps.plan_volumes(b, RELEASE, sums, 20000)
        self.assertGreater(len(vols), 1)
        packed = [rel for v in vols for rel, _ in v.files]
        self.assertEqual(sorted(packed), sorted(sums))
        self.assertTrue(all(v.estimate <= 20000 for v in vols))
        self.assertEqual(vols[0].name, f"omnifile-edge-{RELEASE}-sources-01.tar")

    def test_a_file_bigger_than_an_archive_is_refused(self) -> None:
        rc, _, err = self.dry(make_bundle(self.tmp), "--volume-size", "12000")
        self.assertEqual(rc, 1)
        self.assertIn("does not fit in one archive", err)

    def test_written_archives_are_reproducible_and_hold_the_tree(self) -> None:
        b = make_bundle(self.tmp)
        w1, w2 = self.tmp / "w1", self.tmp / "w2"
        for w in (w1, w2):
            rc, out, err = self.dry(
                b, "--work", str(w), "--write-archives", "--volume-size", "20000"
            )
            self.assertEqual(rc, 0, err)
        names = sorted(p.name for p in w1.iterdir())
        self.assertEqual(names, sorted(p.name for p in w2.iterdir()))
        for n in names:
            self.assertEqual((w1 / n).read_bytes(), (w2 / n).read_bytes(), n)
        listed = dict(
            reversed(line.split("  "))
            for line in (w1 / "ASSETS.SHA256SUMS").read_text().splitlines()
        )
        self.assertGreater(len(listed), 1)
        for name in listed:
            self.assertLessEqual((w1 / name).stat().st_size, 20000, f"{name} is over --volume-size")
        got = {}
        for name, digest in listed.items():
            self.assertEqual(hashlib.sha256((w1 / name).read_bytes()).hexdigest(), digest)
            with tarfile.open(w1 / name) as t:
                for m in t.getmembers():
                    self.assertEqual((m.uid, m.gid, m.mode, m.mtime), (0, 0, 0o644, GENERATED))
                    got[m.name] = hashlib.sha256(t.extractfile(m).read()).hexdigest()
        want = dict(
            reversed(line.split("  ")) for line in (b / "SHA256SUMS").read_text().splitlines()
        )
        self.assertEqual(got, want)
        for loose in ps.LOOSE:
            self.assertTrue((w1 / loose).is_file())

    # Refusals: each is a bundle that must never become a release.

    def refused(self, bundle: Path, needle: str, *extra: str, repo: str = REPO) -> None:
        rc, _, err = run(
            ["--bundle", str(bundle), "--repo", repo, "--gh", str(self.gh.path), *extra]
        )
        self.assertEqual(rc, 1, err)
        self.assertIn(needle, err)
        self.assertEqual(self.gh.calls(), [])

    def test_an_incomplete_bundle_is_refused(self) -> None:
        self.refused(make_bundle(self.tmp, complete=False), "incomplete")

    def test_a_draft_offer_is_refused(self) -> None:
        b = make_bundle(self.tmp)
        offer = b / "SOURCE-OFFER.md"
        offer.write_text("> **DRAFT**\n\n" + offer.read_text())
        self.refused(b, "marked DRAFT")

    def test_a_bundle_for_another_repository_is_refused(self) -> None:
        self.refused(make_bundle(self.tmp), "regenerate the bundle", repo="someone/else")

    def test_a_bundle_without_a_repository_is_refused(self) -> None:
        self.refused(
            make_bundle(self.tmp, repo=so.SOURCES_REPO_PLACEHOLDER),
            "names sources repository '(none)'",
        )

    def test_the_placeholder_repository_is_refused(self) -> None:
        self.refused(make_bundle(self.tmp), "is not OWNER/NAME", repo=so.SOURCES_REPO_PLACEHOLDER)

    def test_another_tag_is_refused(self) -> None:
        self.refused(make_bundle(self.tmp), "must be v1.2.0", "--tag", "1.2.0")

    def test_a_changed_file_is_refused(self) -> None:
        b = make_bundle(self.tmp)
        (b / "omnifile/fsal.tar.gz").write_bytes(b"changed")
        self.refused(b, "omnifile/fsal.tar.gz does not match its SHA-256")

    def test_an_unlisted_file_is_refused(self) -> None:
        b = make_bundle(self.tmp)
        (b / "upstream/extra.tar.gz").write_bytes(b"x")
        self.refused(b, "files not in SHA256SUMS: upstream/extra.tar.gz")

    def test_a_missing_file_is_refused(self) -> None:
        b = make_bundle(self.tmp)
        (b / "upstream/samba-4.23.6.tar.gz").unlink()
        self.refused(b, "names files that are not there")

    def test_a_path_outside_the_trees_is_refused(self) -> None:
        b = make_bundle(self.tmp)
        with (b / "SHA256SUMS").open("a") as f:
            f.write(f"{'0' * 64}  ubuntu/../../etc/passwd\n")
        self.refused(b, "is not a path under")

    def test_a_symlink_is_refused(self) -> None:
        b = make_bundle(self.tmp)
        (b / "ubuntu/link").symlink_to("/etc/passwd")
        self.refused(b, "is a symlink")

    def test_a_symlinked_tree_is_refused(self) -> None:
        b = make_bundle(self.tmp)
        (b / "upstream").rename(self.tmp / "elsewhere")
        (b / "upstream").symlink_to(self.tmp / "elsewhere")
        self.refused(b, "upstream/ is a symlink")

    def test_a_repository_named_only_dots_is_not_set(self) -> None:
        self.assertFalse(so.repo_is_set("owner/.."))
        self.assertFalse(so.repo_is_set("owner/."))
        self.assertTrue(so.repo_is_set(REPO))


class Execute(unittest.TestCase):
    """--execute against the fake gh only (absolute path, made-up repository): the commands it sends."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bundle = make_bundle(self.tmp)
        self.work = self.tmp / "work"
        self.expected = sorted(
            [f"omnifile-edge-{RELEASE}-sources-01.tar", "ASSETS.SHA256SUMS", *ps.LOOSE]
        )

    def go(self, gh: FakeGh, *extra: str) -> tuple[int, str, str]:
        return run(
            [
                "--bundle",
                str(self.bundle),
                "--repo",
                REPO,
                "--gh",
                str(gh.path),
                "--work",
                str(self.work),
                "--execute",
                *extra,
            ]
        )

    def test_a_new_release_is_created_with_every_asset_and_read_back(self) -> None:
        gh = FakeGh(self.tmp, None)
        rc, out, err = self.go(gh)
        self.assertEqual(rc, 0, err)
        self.assertIn("6 assets, verified by name and size", out)
        calls = gh.calls()
        self.assertEqual([c[1] for c in calls], [f"repos/{REPO}", "view", "create", "view"])
        self.assertEqual(calls[0], ["api", f"repos/{REPO}"])
        create = calls[2]
        self.assertEqual(create[:3], ["release", "create", "v1.2.0"])
        self.assertEqual(
            create[-6:],
            [
                "--repo",
                REPO,
                "--title",
                "OmniFile edge appliance 1.2.0: corresponding source",
                "--notes-file",
                str(self.work.resolve() / "SOURCE-OFFER.md"),
            ],
        )
        self.assertEqual(sorted(gh.assets()), self.expected)

    def test_an_existing_release_with_foreign_assets_is_refused_without_prune(self) -> None:
        gh = FakeGh(self.tmp, {"old.tar": 1})
        rc, _, err = self.go(gh)
        self.assertEqual(rc, 1)
        self.assertIn("old.tar (--prune deletes them)", err)
        self.assertEqual([c[1] for c in gh.calls()], [f"repos/{REPO}", "view"])
        self.assertEqual(gh.assets(), {"old.tar": 1})

    def test_an_existing_release_is_updated_pruned_and_read_back(self) -> None:
        gh = FakeGh(self.tmp, {"old.tar": 1, "SOURCE-OFFER.md": 3})
        rc, _, err = self.go(gh, "--prune")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            [c[1] for c in gh.calls()],
            [f"repos/{REPO}", "view", "delete-asset", "upload", "edit", "view"],
        )
        self.assertEqual(
            gh.calls()[2], ["release", "delete-asset", "v1.2.0", "old.tar", "--repo", REPO, "--yes"]
        )
        self.assertIn("--clobber", gh.calls()[3])
        self.assertEqual(sorted(gh.assets()), self.expected)
        self.assertEqual(
            gh.assets()["SOURCE-OFFER.md"], (self.bundle / "SOURCE-OFFER.md").stat().st_size
        )

    def test_a_read_back_that_differs_fails_the_run(self) -> None:
        gh = FakeGh(self.tmp, None)
        # A gh whose create "succeeds" but stores nothing.
        gh.path.write_text(
            FakeGh.SCRIPT.replace('state["exists"] = True', 'state["exists"] = True; files = []')
        )
        rc, _, err = self.go(gh)
        self.assertEqual(rc, 1)
        self.assertIn("does not hold exactly the bundle's assets", err)

    def test_a_failing_gh_call_stops_before_any_write(self) -> None:
        gh = FakeGh(self.tmp, None)
        gh.path.write_text(
            f"#!/usr/bin/env bash\necho \"$*\" >>{gh.log}\necho 'HTTP 401: Bad credentials' >&2\nexit 1\n"
        )
        rc, _, err = self.go(gh)
        self.assertEqual(rc, 1)
        self.assertIn(f"gh api repos/{REPO} failed: HTTP 401", err)
        self.assertEqual(len(gh.log.read_text().splitlines()), 1)

    # The sources repository must be one anyone can read and file an issue in (review of #29).

    def refused_repo(self, repo: dict, needle: str) -> None:
        gh = FakeGh(self.tmp, None, repo=repo)
        rc, _, err = self.go(gh)
        self.assertEqual(rc, 1, err)
        self.assertIn(needle, err)
        self.assertEqual(gh.calls(), [["api", f"repos/{REPO}"]], "no release call before the check")
        self.assertEqual(gh.assets(), {})

    def test_a_private_repository_is_refused_before_any_release_call(self) -> None:
        self.refused_repo(
            {"visibility": "private", "private": True, "has_issues": True}, "is private"
        )

    def test_an_internal_repository_is_refused(self) -> None:
        self.refused_repo(
            {"visibility": "internal", "private": True, "has_issues": True}, "is internal"
        )

    def test_a_repository_with_issues_off_is_refused(self) -> None:
        self.refused_repo(
            {"visibility": "public", "private": False, "has_issues": False}, "Issues turned off"
        )

    def test_a_repository_answer_without_the_fields_is_refused(self) -> None:
        self.refused_repo({}, "is not public")


if __name__ == "__main__":
    unittest.main()
