"""Tests of the guide lint (docs/guide/lint/guide_lint.py).

    python3 -m unittest discover -s docs/guide/lint -p 'test_*.py'

The real guide must be clean; the broken fixture must fail every rule it breaks; a pending entry that
already resolves must fail; and a parameters file that names a parameter main.bicep lacks, or doesn't
build, must fail. Needs the Bicep CLI (or the Azure CLI) to compile main.bicep, offline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
import guide_lint  # noqa: E402


def bicep_commands() -> tuple[list[str], list[str]] | None:
    if shutil.which("bicep"):
        return ["bicep", "build"], ["bicep", "build-params"]
    if shutil.which("az"):
        return ["az", "bicep", "build", "--file"], ["az", "bicep", "build-params", "--file"]
    return None


CMDS = bicep_commands()


# WP1 (#34) and WP8 (#37) are merged: the guide resolves against main with no allowance.
PENDING: list[Path] | None = None


def lint(guide: Path, *, repo: Path = REPO, pending: list[Path] | None = PENDING,
         build: bool = False) -> guide_lint.Report:
    r = guide_lint.Report()
    p = guide_lint.Pending(pending)
    lt = guide_lint.Lint(repo, guide, LintTest.main_json, p, r)
    lt.run()
    if build:
        guide_lint.build_params(repo, CMDS[1], lt.params, p, r, lt.nullable)
    return r


def codes(r: guide_lint.Report) -> set[str]:
    return {f.split()[1] for f in r.fails}


def mirror(tmp: Path) -> Path:
    """A copy of the repository's layout, made of links, whose docs/guide is a real copy to break."""
    root = tmp / "repo"
    root.mkdir()
    for child in REPO.iterdir():
        if child.name not in ("docs", ".git"):
            (root / child.name).symlink_to(child)
    (root / "docs").mkdir()
    for child in (REPO / "docs").iterdir():
        if child.name != "guide":
            (root / "docs" / child.name).symlink_to(child)
    shutil.copytree(REPO / "docs/guide", root / "docs/guide", symlinks=True)
    return root


@unittest.skipIf(CMDS is None, "needs the Bicep CLI or the Azure CLI")
class LintTest(unittest.TestCase):
    main_json: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "main.json"
            subprocess.run([*CMDS[0], str(REPO / "deploy/azure/main.bicep"), "--outfile", str(out)],
                           check=True, capture_output=True)
            cls.main_json = json.loads(out.read_text())

    def test_the_guide_is_clean(self) -> None:
        r = lint(REPO / "docs/guide/README.md", build=True)
        self.assertEqual(r.fails, [])

    def test_the_broken_fixture_fails_every_rule_it_breaks(self) -> None:
        r = lint(HERE / "fixtures/broken-guide.md")
        text = "\n".join(r.fails)
        want = {
            "step-dup": "guide-step=1 is also used",
            "step-order": "guide-step=2 comes after guide-step=3",
            "step-on": "on='moon'",
            "step-lang": "a workstation step is bash, not 'powershell'",
            "step-key": "unknown key colour=",
            "source": "starts with `. ./omnifile.env",
            "var": "OMNI_NO_SUCH_VALUE is not in omnifile.env.example",
            "bicep": "`apiIngresss` is neither a parameter nor an output",
            "output": "noSuchOutput is not an output",
            "path": "deploy/azure/no-such-script.sh doesn't exist",
            "flag": "relay-ranges.sh has no flag --no-such-flag",
            "subcommand": "omnifile-setup has no subcommand frobnicate",
            "module": "omnifile_service.no_such_module is not a module",
            "psql": "gives -v unused",
            "portal-route": "/no-such-page is not a portal route",
            "portal-label": "'No Such Button Anywhere'",
            "portal-verb": "'wave'",
            "toml": "[aws] colour is not a setup answer",
            "section": "no section about 'troubleshooting'",
            "kerberos": "Kerberos note",
            "recall-promise": "184 GB",
            "edge-path": "/var/lib/omnifile/no-such-file.conf: neither",
            "unit": "omnifile-nosuch: no unit file",
            "setting": "OMNIFILE_NO_SUCH_SETTING is read by no",
            "portal-expect": "expect 'Totally Absent Text'",
            "strict": "a step on the filer starts with `set -euo pipefail`",
            "step-ref": "step 1 doesn't name `edge-ca.pem`",
            "feature": "text marked pending WP8 (no-such-feature), with no feature entry",
            "gate": "a plan=yes step comes before the gate=whatif step",
            "aws-name": "AWS name omnifile-batch without $sfx",
            "product-name": "names a partner product: say 'API client' or 'the API'",
        }
        for code, words in want.items():
            with self.subTest(code=code):
                self.assertIn(code, codes(r))
                self.assertIn(words, text)
        self.assertIn("omnifile_service.enrol has no subcommand make-coffee", text)
        self.assertIn("omnifile-setup apply has no flag --no-such-option", text)
        self.assertIn("[weather] is not a section", text)
        self.assertIn("<secret:OMNI_UNDECLARED_PASSWORD> is not declared", text)
        # Commands inside quotes are checked like any other.
        self.assertIn("omnifile-setup has no subcommand nosuchcmd", text)
        self.assertIn("relay-ranges.sh has no flag --bogus-flag", text)
        self.assertIn("nfs-ganesha-nope: no unit file", text)
        # A label a loose template would have matched (`Recall ${name}`) is refused.
        self.assertIn("'Recall Some Totally Made Up Thing That Is Long'", text)
        # The flags of a script or module still to come are checked against what was declared.
        self.assertIn("deploy/azure/whatif.sh has no flag --no-such-whatif-flag", text)
        self.assertIn("omnifile_service.admin has no flag --no-such-admin-flag", text)
        # The re-review's mutations: a template's variable part is never free text; a flag on a
        # continuation line is checked; an edge path is a whole token, not a prefix of one.
        self.assertIn("click 'Recall ahead of tme': no such text", text)
        self.assertIn("omnifile_service.catalog.migrate has no flag --lieve", text)
        self.assertIn("omnifile_service.admin has no flag --lieve", text)
        self.assertIn("/etc/omnifile/setup.tom: neither", text)
        self.assertIn("step 999: the guide has no such step", text)
        self.assertIn("gate= takes only whatif", text)
        # A second deploy with no preview of its own since the first (a deploy flag on a continuation line too).
        self.assertIn("line 117: a whatif.sh --deploy step with no gate=whatif step since the previous deploy", text)
        self.assertNotIn("line 112: a whatif.sh --deploy step", text)
        self.assertIn("AWS name omnifile-edge-s3-read without $sfx", text)
        self.assertNotIn("AWS name omnifile-service without", text)
        # smbd is not ours: no unit file needed.
        self.assertNotIn("smbd: no unit file", text)

    def test_check_sh_is_red_on_the_fixture_and_green_on_the_guide(self) -> None:
        bad = subprocess.run([str(REPO / "docs/guide/check.sh"), str(HERE / "fixtures/broken-guide.md")],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertIn("guide lint:", bad.stdout)
        good = subprocess.run([str(REPO / "docs/guide/check.sh")], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertIn("guide lint: ok", good.stdout)

    def test_a_pending_entry_that_resolves_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending-wp1.txt"
            pending.write_text("param namePrefix\npath deploy/azure/check.sh\n")
            extra = Path(tmp) / "pending-wp8.txt"
            extra.write_text("module omnifile_service.catalog.migrate\n"
                             "feature landed-already deploy/azure/check.sh Compile-only checks\n")
            r = lint(REPO / "docs/guide/README.md", pending=[pending, extra])
        text = "\n".join(r.fails)
        self.assertIn("param namePrefix resolves now", text)
        self.assertIn("path deploy/azure/check.sh resolves now", text)
        self.assertIn("module omnifile_service.catalog.migrate resolves now", text)
        self.assertIn("feature landed-already deploy/azure/check.sh Compile-only checks resolves now", text)

    def test_nothing_is_pending(self) -> None:
        r = lint(REPO / "docs/guide/README.md")
        self.assertEqual(r.pending, [])
        self.assertFalse((HERE / "pending-wp1.txt").exists())
        self.assertFalse((HERE / "pending-wp8.txt").exists())
        self.assertNotIn("--pending", (REPO / "docs/guide/check.sh").read_text())

    def test_a_pending_script_is_checked_against_its_declared_flags(self) -> None:
        # The allowance mechanism stays for the next package: a script still to come may use only its declared flags.
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending-wpx.txt"
            pending.write_text("path deploy/azure/future.sh\nflags deploy/azure/future.sh --known\n")
            guide = Path(tmp) / "guide.md"
            guide.write_text("# Guide\n\n```bash guide-step=1 on=workstation\n. ./omnifile.env && . ./omnifile.state.env\n"
                             "deploy/azure/future.sh --known x --unknown y\n```\n")
            r = lint(guide, pending=[pending])
        text = "\n".join(r.fails)
        self.assertIn("deploy/azure/future.sh (still to come from WP1) declares no flag --unknown", text)
        self.assertNotIn("declares no flag --known", text)

    def test_a_product_name_anywhere_in_docs_guide_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = mirror(Path(tmp))
            tool = root / "docs/guide/tools/check-env.sh"
            tool.write_text(tool.read_text() + "# used by " + "Prizm" + "anyx\n")
            r = lint(root / "docs/guide/README.md", repo=root)
        self.assertIn("docs/guide/tools/check-env.sh line", "\n".join(r.fails))
        self.assertIn("product-name", codes(r))

    def test_a_parameter_main_bicep_lacks_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = mirror(Path(tmp))
            bp = root / "docs/guide/customer.bicepparam"
            bp.write_text(bp.read_text() + "param noSuchParameter = 'x'\n")
            r = lint(root / "docs/guide/README.md", repo=root)
        self.assertIn("noSuchParameter (customer.bicepparam line", "\n".join(r.fails))

    def test_a_client_default_that_differs_from_main_bicep_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = mirror(Path(tmp))
            bp = root / "docs/guide/customer.bicepparam"
            # The server's size is deploymentSize's (null here); the vault's soft-delete days still have a default.
            bp.write_text(bp.read_text().replace("empty(testKvDays) ? 90 :", "empty(testKvDays) ? 30 :"))
            r = lint(root / "docs/guide/README.md", repo=root)
        self.assertIn("keyVaultSoftDeleteDays: the client default here is 30, main.bicep's is 90", "\n".join(r.fails))

    def test_a_client_value_that_overrides_the_small_size_fails(self) -> None:
        """A small site gets small's server: a literal in customer.bicepparam would replace it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = mirror(Path(tmp))
            bp = root / "docs/guide/customer.bicepparam"
            bp.write_text(bp.read_text().replace("empty(testPgName) ? null :", "empty(testPgName) ? 'Standard_D4ds_v5' :"))
            r = lint(root / "docs/guide/README.md", repo=root, build=True)
        self.assertIn("customer.bicepparam (a small site) sets postgresSkuName", "\n".join(r.fails))

    def test_the_size_comes_from_omnifile_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = mirror(Path(tmp))
            bp = root / "docs/guide/customer.bicepparam"
            bp.write_text(bp.read_text().replace("param deploymentSize = empty(sizeChoice) ? 'standard' : sizeChoice",
                                                 "param deploymentSize = 'standard'"))
            r = lint(root / "docs/guide/README.md", repo=root, build=True)
        fails = "\n".join(r.fails)
        self.assertIn("customer.bicepparam (a small site) deploys the standard size", fails)
        self.assertIn("customer.bicepparam (a test run) deploys the standard size", fails)

    def test_a_parameters_file_that_does_not_build_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = mirror(Path(tmp))
            bp = root / "docs/guide/customer.bicepparam"
            # A string where main.bicep wants an int.
            bp.write_text(bp.read_text() + "param logRetentionDays = 'ninety'\n")
            r = lint(root / "docs/guide/README.md", repo=root, build=True)
        self.assertIn("params", codes(r))
        self.assertIn("doesn't build", "\n".join(r.fails))


class ParsingTest(unittest.TestCase):
    def test_fences_and_attributes(self) -> None:
        blocks = guide_lint.parse_blocks([
            "text", "```bash guide-step=3 on=workstation manual=yes", "echo hi", "```", "```", "plain", "```"])
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].attrs, {"guide-step": "3", "on": "workstation", "manual": "yes"})
        self.assertEqual(blocks[0].lang, "bash")
        self.assertEqual(blocks[0].body, ["echo hi"])

    def test_commands_join_continuations_and_drop_comments(self) -> None:
        got = guide_lint.commands("# a comment\nfoo --a \\\n  --b x\nbar")
        self.assertEqual([g.split() for g in got], [["foo", "--a", "--b", "x"], ["bar"]])

    def test_segments_split_pipes_and_substitutions(self) -> None:
        segs = guide_lint.segments('x=$(deploy/azure/whatif.sh -g "$RG" | tail -n 1) && echo ok')
        self.assertIn(["deploy/azure/whatif.sh", "-g", "$RG"], segs)
        self.assertIn(["tail", "-n", "1"], segs)


if __name__ == "__main__":
    os.chdir(REPO)
    unittest.main()
