"""The tenant run: the M4 exit and the real-tenant checks, on an Azure run that is up.

The Azure run's guide registers the Entra apps and publishes the Teams app (register.sh,
deploy/teams/build.sh), so they belong to that run and its teardown. This run adds only
what the checks need beyond the product: a partner probe application (client credentials,
a certificate made at run time) to check partner tokens, tagged with the run id. The M4
exit's Teams steps need a person: the owner confirms a card arrived, clicks it, and sees it
update; the harness asks, records the answer, and checks the service side itself.

The identity chain (uid -> SID -> Entra user) needs an edge joined to an AD that Entra
Connect syncs. The Azure run's own DC is not synced, so with it the stub-read step reports
pending; joining the edge to the client's synced AD is a manual step for now (RT-40).
"""

from __future__ import annotations

import json
from typing import Any

from .allowlist import AllowList
from .core import (
    HARNESS_TAG_VALUE,
    REAL,
    TAG_HARNESS,
    TAG_RUN,
    AwaitingApproval,
    Context,
    Refused,
    StepFailed,
    UsageError,
    digest_of,
    iso,
    norm_digest,
    prefix_for,
    require_run_id,
)
from .results import exit_result, run_checks

GRAPH = "https://graph.microsoft.com/v1.0"


def probe_name(run_id: str) -> str:
    """The partner probe app's name: the whole run id, never the prefix two runs can share."""
    return f"ofreal-{run_id}-partner-probe"


class Tenant:
    def __init__(self, ctx: Context, al: AllowList) -> None:
        self.ctx, self.al = ctx, al

    def identity(self) -> dict[str, str]:
        """The tenant, checked against the allow-list before the sign-in is copied into the
        run's own az directory and pinned there (azpin), so another shell can't redirect it."""
        from . import azpin

        ctx = self.ctx
        acct = azpin.owner_account(ctx)
        if not acct:
            raise Refused(
                "az is not signed in to the tenant (az login --tenant <id> "
                "--allow-no-subscriptions); the harness never signs in"
            )
        planned = ctx.state.get("identity")
        account = (planned or {}).get("account") or acct["id"]
        want = planned["tenant"] if planned else acct["tenantId"]
        self.al.require("m365-tenant", want, "tenant")
        azpin.pin(ctx, account, service_principal=azpin.is_service_principal(acct))
        got = ctx.az("account", "show")
        self.al.require("m365-tenant", got["tenantId"], "tenant")
        st = ctx.state.setdefault(
            "identity",
            {
                "tenant": got["tenantId"],
                "account": got["id"],
                "user": acct.get("user", {}).get("name", ""),
            },
        )
        if st["tenant"] != got["tenantId"]:
            raise Refused(f"run {ctx.run_id} was planned in tenant {st['tenant']}")
        ctx.save_state()
        return st

    def azure_state(self) -> dict[str, Any]:
        """The Azure run this tenant run uses. A dry run without one gets a stand-in."""
        rid = self.ctx.azure_run
        if rid:
            require_run_id(rid)
            f = REAL / "out" / rid / "state.json"
            if not f.exists():
                raise UsageError(f"no state for Azure run {rid} ({f})")
            st = json.loads(f.read_text())
            if not st.get("up") or st.get("down"):
                raise UsageError(f"Azure run {rid} is not up")
            if st.get("dry_run") != self.ctx.dry_run:
                raise UsageError("a dry run can't use a real Azure run, or the reverse")
            return st
        if not self.ctx.dry_run:
            raise UsageError("a tenant run needs --azure-run <id> of an Azure run that is up")
        return {
            "run_id": "rr-dry-azure",
            "prefix": prefix_for("rr-dry-azure"),
            "outputs": {
                "webUrl": "https://rdry-web.internal.invalid",
                "workerPrincipalId": "11111111-2222-3333-4444-555555555555",
            },
            "vm": {"ip": "192.0.2.10"},
            "ad_realm": "OFREAL.TEST",
            "dry_run": True,
        }

    def the_plan(self) -> dict[str, Any]:
        az = self.azure_state()
        return {
            "cloud": "tenant",
            "run": self.ctx.run_id,
            "tenant": self.ctx.state["identity"]["tenant"],
            "azure_run": az["run_id"],
            "expires": self.ctx.expires,
            "test_user": self.al.one("m365-test-user"),
            "resources": {"partner_probe_app": probe_name(self.ctx.run_id)},
        }

    def plan(self) -> None:
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        ctx.state.update(
            {
                "run_id": ctx.run_id,
                "cloud": "tenant",
                "dry_run": ctx.dry_run,
                "prefix": prefix_for(ctx.run_id),
            }
        )
        ctx.state.setdefault("started", iso(ctx.now))
        ctx.fix_expiry(self.al.max_ttl)
        plan = self.the_plan()
        (ctx.evidence / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        ctx.state["digest"] = digest_of(plan)
        ctx.state["planned"] = True
        ctx.save_state()
        ctx.log(f"plan digest {ctx.state['digest']}")

    def gate(self) -> None:
        ctx = self.ctx
        if not ctx.approve:
            raise AwaitingApproval(f"awaiting approval: rerun with --approve {ctx.state['digest']}")
        fresh = digest_of(self.the_plan())
        if ctx.approve == "auto":
            if not ctx.dry_run:
                raise Refused("--approve auto is for dry runs only")
            ctx.log(f"dry run: approving this run's own plan digest {fresh}")
            want = fresh
        else:
            want = norm_digest(ctx.approve)
        if fresh != want:
            raise AwaitingApproval(f"the plan changed: approved {want}, now {fresh}")

    def up(self) -> None:
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        if not ctx.state.get("planned"):
            raise UsageError(f"run {ctx.run_id} has no plan; run `plan` first")
        with ctx.step("gate"):
            self.gate()
        with ctx.step("up.partner-probe"):
            from .pki import leaf

            name = probe_name(ctx.run_id)
            app = ctx.az(
                "ad", "app", "create", "--display-name", name, "--sign-in-audience", "AzureADMyOrg"
            )
            ctx.state["partner_app"] = {"id": app["id"], "appId": app["appId"]}
            ctx.save_state()
            ctx.az(
                "rest",
                "--method",
                "patch",
                "--url",
                f"{GRAPH}/applications/{app['id']}",
                "--body",
                json.dumps(
                    {"tags": [f"{TAG_RUN}:{ctx.run_id}", f"{TAG_HARNESS}:{HARNESS_TAG_VALUE}"]}
                ),
            )
            cert = leaf(ctx, "partner-probe", f"omnifile-partner-probe-{ctx.run_id}", None)
            pem = ctx.out / "pki" / "partner-probe.pem"
            pem.write_text(cert["cert"])
            ctx.az(
                "ad",
                "app",
                "credential",
                "reset",
                "--id",
                app["id"],
                "--cert",
                f"@{pem}",
                "--append",
            )
            ctx.az("ad", "sp", "create", "--id", app["appId"])
        ctx.state["up"] = True
        ctx.save_state()

    # ------------------------------------------------------------------ exit

    def ask(self, step: str, question: str) -> bool | None:
        """A question for the owner (a real run waits for y or n). A dry run can't answer, so
        the step is left manual, never passed."""
        if self.ctx.dry_run:
            self.ctx.log(f"dry run: would ask the owner: {question}")
            return None
        ans = input(f"[{step}] {question} [y/n] ").strip().lower()
        return ans.startswith("y")

    def exit(self) -> None:
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        az = self.azure_state()
        user = self.al.one("m365-test-user")
        steps = [
            (
                "stub-read",
                "the AD-synced test user reads a stub over SMB; the edge sends the "
                "user's SID with the recall",
            ),
            ("card-sent", "Bot Connector accepted the card for that user (worker logs)"),
            ("card-seen", f"{user} sees the card in their 1:1 chat with the bot"),
            ("click-recall", "the owner clicks Recall on a failed card; a recall is queued"),
            ("card-follows", "the card follows the new recall to available"),
            ("policy-cost", "an admin edits a policy through the real Entra app and sees the cost"),
        ]
        failed = []
        for name, what in steps:
            with ctx.step(f"exit.{name}"):
                try:
                    status, detail = self._exit_step(name, az, user)
                except StepFailed as e:
                    status, detail = "fail", str(e)
                    failed.append(name)
            exit_result(ctx, name, status, f"{what}: {detail}")
        if failed:
            raise StepFailed(f"M4 exit on the tenant failed at: {', '.join(failed)}")

    def _exit_step(self, name: str, az: dict[str, Any], user: str) -> tuple[str, str]:
        ctx = self.ctx
        vm_host = f"omnitest@{az['vm']['ip']}"
        key = REAL / "out" / az["run_id"] / "ssh" / "id_ed25519"
        ssh = ["ssh", "-i", str(key), "-o", "BatchMode=yes", vm_host]
        if name == "stub-read":
            if az.get("ad_realm", "").upper() == "OFREAL.TEST" and not ctx.dry_run:
                return "pending", (
                    "the edge is joined to the Azure run's own DC, which Entra doesn't sync; "
                    "join it to the client's synced AD by hand first (RT-40)"
                )
            p = ctx.run(
                [*ssh, "sudo /srv/ofreal/vm/exit.sh tenant-stub-read"],
                input=json.dumps({"user": user}),
            )
            return "pass", p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "read"
        if name == "card-sent":
            ws = az.get("outputs", {}).get("logAnalyticsWorkspaceId", "")
            rows = (
                ctx.az(
                    "monitor",
                    "log-analytics",
                    "query",
                    "-w",
                    ws,
                    "--analytics-query",
                    "ContainerAppConsoleLogs_CL | where ContainerAppName_s endswith "
                    "'-worker' and Log_s has 'botconnector' and Log_s has 'sent' "
                    "| take 5",
                )
                or []
            )
            if not rows:
                raise StepFailed("no Bot Connector send in the worker's logs")
            return "pass", f"{len(rows)} send(s) logged"
        if name in ("card-seen", "card-follows"):
            q = {
                "card-seen": f"Did {user} get a Teams card for the file they read?",
                "card-follows": "Did the card change to 'available' after the recall?",
            }[name]
            a = self.ask(name, q)
            return (
                ("manual", "dry run: the owner was not asked")
                if a is None
                else ("pass" if a else "fail", "the owner answered " + ("yes" if a else "no"))
            )
        if name == "click-recall":
            a = self.ask(name, "Click Recall on the card now. Done?")
            if a is None:
                return "manual", "dry run: no click"
            if not a:
                raise StepFailed("the owner did not click Recall")
            return "pass", "the owner clicked Recall"
        if name == "policy-cost":
            from .portal import Portal

            portal = Portal(ctx, az.get("outputs", {}).get("webUrl", ""), None)
            r = portal.act("exit-policy-cost")
            return "pass", r.get("detail", "cost changed")
        raise AssertionError(name)

    def checks(self) -> dict[str, int]:
        with self.ctx.step("identity"):
            self.identity()
        from .checks.tenant import TenantEnv

        return run_checks(self.ctx, TenantEnv(self.ctx, self))

    # ------------------------------------------------------------------ down

    def _apps(self) -> list[dict]:
        """The partner probe app: by the id recorded the moment it was made, by tag, and by its
        name (the whole run id), so an app whose tagging failed is still found. A name match
        with another run's tag, or made outside this run's window, is left alone."""
        from .ownership import runs_in_tags, why_not_ours

        ctx, found = self.ctx, {}
        lists = [
            ctx.az("ad", "app", "list", "--filter", f"tags/any(t:t eq '{TAG_RUN}:{ctx.run_id}')"),
            ctx.az(
                "ad",
                "app",
                "list",
                "--filter",
                f"startswith(displayName,'{probe_name(ctx.run_id)}')",
            ),
        ]
        for lst in lists:
            for a in lst or []:
                found[a["id"]] = a
        rec = ctx.state.get("partner_app") or {}
        if rec.get("id") and rec["id"] not in found:
            a = ctx.az("ad", "app", "show", "--id", rec["id"], check=False)
            if a:
                found[a["id"]] = a
        mine = []
        for a in found.values():
            why = why_not_ours(
                ctx,
                ctx.run_id,
                runs=runs_in_tags(a.get("tags")),
                created=a.get("createdDateTime"),
                recorded=a["id"] == rec.get("id"),
            )
            if why is None:
                mine.append(a)
            else:
                ctx.log(f"leaving Entra application {a.get('displayName')!r} alone: {why}")
        return mine

    def down(self) -> None:
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        done, errors = [], []
        try:
            with ctx.step("down.entra"):
                for a in self._apps():
                    ctx.run(["az", "ad", "app", "delete", "--id", a["id"]])
                    done.append(f"app {a['displayName']}")
        except Exception as e:  # the proof still runs and says what is left
            errors.append(f"down.entra: {type(e).__name__}: {e}")
        with ctx.step("down.prove"):
            left = [f"Entra application {a['displayName']}" for a in self._apps()]
        (ctx.out / "teardown.json").write_text(
            json.dumps(
                {
                    "runs": {
                        ctx.run_id: {
                            "done": done,
                            "residue": left,
                            "errors": errors,
                            "warnings": [],
                        }
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        if left or errors:
            raise StepFailed(f"teardown left resources or failed in part: {left + errors}")
        ctx.state["down"] = True
        ctx.save_state()
        ctx.log(f"teardown proved: nothing of {ctx.run_id} remains")
