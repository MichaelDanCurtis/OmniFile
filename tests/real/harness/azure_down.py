"""The Azure run's teardown: by tag, and by the ids and names the run recorded, WORM first,
each part isolated so one failure never skips the rest, then a proof that nothing is left.

What a run can leave outside its resource group, and how each is found:
- Entra applications: the ids the run recorded the moment they existed (register.sh's
  entra.json), its tag, and the names it gives them ("OmniFile <prefix> portal",
  "<prefix>-..."). Two runs can share a prefix, so a name match counts only for an app with
  no other run's tag that was made inside this run's window (ownership.py);
- the Teams app in the organisation's catalog: the recorded catalog id, and the manifest id of
  the package the run built (deploy/teams/out/omnifile-teams-app.zip);
- the PostgreSQL admin group (ofreal-<run id>-pg-admins, its description naming the run);
- custom role definitions scoped to the run's group;
- soft-deleted vaults (tagged, or named in the group before it went): purged when allowed,
  else reported.
The run's group is found by tag and by its name (rg-ofreal-<run id>), since a guide step's
`az group create` may have replaced its tags.
"""

from __future__ import annotations

import json
import time
import zipfile
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from .core import (
    HARNESS_TAG_VALUE,
    TAG_EXPIRES,
    TAG_HARNESS,
    TAG_RUN,
    HarnessError,
    StepFailed,
    current_time,
    iso,
    parse_iso,
    prefix_for,
    valid_run_id,
)
from .ownership import runs_in_tags, runs_in_text, why_not_ours

GRAPH = "https://graph.microsoft.com/v1.0"
#: How a data-plane call says the role assignment it needs hasn't reached the account yet.
NOT_YET = ("AuthorizationPermissionMismatch", "AuthorizationFailure", "403")


class AzureDown:
    def __init__(self, azure) -> None:
        self.az, self.ctx = azure, azure.ctx

    # ------------------------------------------------------------- driver

    def down(self) -> None:
        ctx = self.ctx
        run_ids = [ctx.run_id]
        if ctx.expired:
            run_ids += self._expired_runs()
        report: dict[str, Any] = {"runs": {}}
        for rid in run_ids:
            report["runs"][rid] = self._down_one(rid)
        (ctx.out / "teardown.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        bad = {
            r: v["residue"] + v["errors"]
            for r, v in report["runs"].items()
            if v["residue"] or v["errors"]
        }
        if bad:
            raise StepFailed(f"teardown left resources or failed in part: {json.dumps(bad)}")
        ctx.state["down"] = True
        for k in ("paused_at", "pending_gate"):
            ctx.state.pop(k, None)  # nothing left to resume
        ctx.save_state()
        ctx.log("teardown proved: nothing of " + ", ".join(run_ids) + " remains")

    @contextmanager
    def _isolated(self, name: str, errors: list[str]):
        """One part of the teardown: its failure is recorded and the rest still runs."""
        try:
            with self.ctx.step(name):
                yield
        except (HarnessError, KeyError, ValueError, OSError) as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            self.ctx.log(f"teardown part {name} failed, the rest goes on: {e}")

    def _expired_runs(self) -> list[str]:
        out = []
        groups = self.ctx.az("group", "list", "--tag", f"{TAG_HARNESS}={HARNESS_TAG_VALUE}") or []
        for g in groups:
            t = g.get("tags") or {}
            rid = t.get(TAG_RUN, "")
            if not valid_run_id(rid):
                # A tag is anyone's to write: never use one that isn't a run id.
                self.ctx.log(f"skipping group {g.get('name')!r}: its {TAG_RUN} tag is not a run id")
                continue
            try:
                expired = parse_iso(t.get(TAG_EXPIRES, "")) < self.ctx.now
            except ValueError:
                continue
            if rid != self.ctx.run_id and expired:
                out.append(rid)
        return sorted(set(out))

    def _prefix(self, rid: str) -> str:
        if rid == self.ctx.run_id:
            return self.ctx.state.get("prefix") or prefix_for(rid)
        return prefix_for(rid)

    def _groups(self, rid: str) -> list[str]:
        ctx = self.ctx
        names = {
            g["name"]
            for g in (ctx.az("group", "list", "--tag", f"{TAG_RUN}={rid}") or [])
            if (g.get("tags") or {}).get(TAG_HARNESS) == HARNESS_TAG_VALUE
        }
        own = f"rg-ofreal-{rid}"
        if own not in names:
            p = ctx.run(["az", "group", "show", "-n", own, "-o", "json"], check=False)
            if p.returncode == 0:
                names.add(own)
        return sorted(names)

    def _down_one(self, rid: str) -> dict[str, Any]:
        ctx = self.ctx
        sub = ctx.state["identity"]["subscription"]
        done: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []
        residue: list[str] = []
        groups = self._groups(rid)
        vaults = ctx.state.setdefault("vault_names", [])
        with self._isolated(f"down.vault-names[{rid}]", errors):
            for g in groups:
                for v in ctx.az("keyvault", "list", "-g", g) or []:
                    if v["name"] not in vaults:
                        vaults.append(v["name"])
            ctx.save_state()
        held: dict[str, list[str]] = {}
        for g in groups:
            with self._isolated(f"down.worm[{g}]", errors):
                d, h = self._worm_first(g)
                done += d
                held.update(h)
        for g in groups:
            with self._isolated(f"down.group[{g}]", errors):
                done += self._delete_group(g, held)
        with self._isolated(f"down.roles[{rid}]", errors):
            defs = (
                ctx.az(
                    "role",
                    "definition",
                    "list",
                    "--custom-role-only",
                    "true",
                    "--scope",
                    f"/subscriptions/{sub}",
                )
                or []
            )
            for g in groups:
                scope = f"/subscriptions/{sub}/resourceGroups/{g}".lower()
                for d in defs:
                    if any(s.lower() == scope for s in d.get("assignableScopes", [])):
                        ctx.run(
                            [
                                "az",
                                "role",
                                "definition",
                                "delete",
                                "--name",
                                d["name"],
                                "--scope",
                                d["assignableScopes"][0],
                            ]
                        )
                        done.append(f"role definition {d['roleName']}")
        with self._isolated(f"down.vaults[{rid}]", errors):
            for v in self._deleted_vaults(rid):
                p = ctx.run(
                    ["az", "keyvault", "purge", "-n", v["name"], "-l", v["properties"]["location"]],
                    check=False,
                )
                if p.returncode == 0:
                    done.append(f"purged vault {v['name']}")
                else:
                    warnings.append(
                        f"vault {v['name']} could not be purged: {(p.stderr or '').strip()[-200:]}"
                    )
        with self._isolated(f"down.entra[{rid}]", errors):
            done += self._entra_down(rid)
        with self._isolated(f"down.prove[{rid}]", errors):
            residue = self._prove(rid, groups, held)
        return {"done": done, "warnings": warnings, "errors": errors, "residue": residue}

    # -------------------------------------------------------------- WORM

    def _dp(self, *args: str) -> Any:
        """A data-plane az call (--auth-mode login). Right after the harness gives itself a
        data role, Azure answers 403 until the assignment propagates: retry those."""
        ctx = self.ctx
        tries, wait = (6, 0) if ctx.dry_run else (21, 30)
        for i in range(tries):
            p = ctx.run(["az", *args, "-o", "json"], check=False)
            if p.returncode == 0:
                return json.loads(p.stdout) if p.stdout.strip() else None
            err = p.stderr or ""
            if not any(s in err for s in NOT_YET) or i == tries - 1:
                raise StepFailed(f"az {' '.join(args[:3])} failed: {err.strip()[-300:]}")
            ctx.log(f"data plane not yet allowed ({args[0]} {args[1]}); again in {wait} s")
            time.sleep(wait)
        return None

    def _worm_first(self, rg: str) -> tuple[list[str], dict[str, list[str]]]:
        """Before a WORM account can go: the group's locks, then each container's unlocked
        default policy and legal holds, then each blob version's unlocked policy and legal
        hold (read one version at a time: `blob list --include` shows neither). A locked policy
        can't be removed before it expires: the account is residue, reported, never forced,
        and the rest of the group still goes."""
        ctx, done, held = self.ctx, [], {}
        for lock in ctx.az("lock", "list", "-g", rg) or []:
            ctx.run(["az", "lock", "delete", "--ids", lock["id"]])
            done.append(f"lock {lock['name']}")
        accounts = ctx.az("storage", "account", "list", "-g", rg) or []
        for a in accounts:
            if not (a.get("immutableStorageWithVersioning") or {}).get("enabled"):
                continue
            name = a["name"]
            try:
                done += self._clear_account(rg, a, held)
            except StepFailed as e:
                held.setdefault(name, []).append(str(e))
        return done, held

    def _clear_account(self, rg: str, a: dict, held: dict[str, list[str]]) -> list[str]:
        ctx, name, done = self.ctx, a["name"], []
        now = current_time(ctx.dry_run, ctx.now)
        # Data-plane access for the owner, from the allow-listed address only, for this.
        ctx.az(
            "storage",
            "account",
            "update",
            "-g",
            rg,
            "-n",
            name,
            "--public-network-access",
            "Enabled",
            "--default-action",
            "Deny",
        )
        ctx.az(
            "storage",
            "account",
            "network-rule",
            "add",
            "-g",
            rg,
            "--account-name",
            name,
            "--ip-address",
            self.az.al.one("ssh-from"),
        )
        ctx.az(
            "role",
            "assignment",
            "create",
            "--assignee-object-id",
            ctx.state.get("owner_oid", ""),
            "--assignee-principal-type",
            "User",
            "--role",
            "Storage Blob Data Owner",
            "--scope",
            a["id"],
        )
        for c in (
            self._dp("storage", "container", "list", "--account-name", name, "--auth-mode", "login")
            or []
        ):
            cname = c["name"]
            pol = (
                ctx.az(
                    "storage",
                    "container",
                    "immutability-policy",
                    "show",
                    "-g",
                    rg,
                    "--account-name",
                    name,
                    "-c",
                    cname,
                    check=False,
                )
                or {}
            )
            # A LOCKED container policy can't be removed, but it protects each blob only for
            # its days from the blob's creation: what is past them is deleted, the rest held.
            locked_days = (
                int(pol.get("immutabilityPeriodSinceCreationInDays") or 0)
                if pol.get("state") == "Locked"
                else None
            )
            held_here: list[str] = []
            if pol.get("state") == "Unlocked":
                ctx.az(
                    "storage",
                    "container",
                    "immutability-policy",
                    "delete",
                    "-g",
                    rg,
                    "--account-name",
                    name,
                    "-c",
                    cname,
                    "--if-match",
                    pol["etag"],
                )
                done.append(f"container policy {name}/{cname}")
            lh = (
                ctx.az(
                    "storage",
                    "container",
                    "legal-hold",
                    "show",
                    "-g",
                    rg,
                    "--account-name",
                    name,
                    "-c",
                    cname,
                    check=False,
                )
                or {}
            )
            if lh.get("tags"):
                ctx.az(
                    "storage",
                    "container",
                    "legal-hold",
                    "clear",
                    "-g",
                    rg,
                    "--account-name",
                    name,
                    "-c",
                    cname,
                    "--tags",
                    *lh["tags"],
                )
                done.append(f"legal hold {name}/{cname}")
            for b in (
                self._dp(
                    "storage",
                    "blob",
                    "list",
                    "--account-name",
                    name,
                    "-c",
                    cname,
                    "--auth-mode",
                    "login",
                    "--include",
                    "v",
                )
                or []
            ):
                vid = b.get("versionId")
                full = (
                    self._dp(
                        "storage",
                        "blob",
                        "show",
                        "--account-name",
                        name,
                        "-c",
                        cname,
                        "-n",
                        b["name"],
                        "--version-id",
                        vid,
                        "--auth-mode",
                        "login",
                    )
                    or {}
                )
                props = full.get("properties", {})
                pol = props.get("immutabilityPolicy") or {}
                until = self._locked_until(pol, props, locked_days)
                if pol.get("policyMode") != "Locked" and pol.get("expiryTime"):
                    self._dp(
                        "storage",
                        "blob",
                        "immutability-policy",
                        "delete",
                        "--account-name",
                        name,
                        "-c",
                        cname,
                        "-n",
                        b["name"],
                        "--version-id",
                        vid,
                        "--auth-mode",
                        "login",
                    )
                    done.append(f"version policy {name}/{cname}/{b['name']}@{vid}")
                if props.get("hasLegalHold") or props.get("legalHold"):
                    self._dp(
                        "storage",
                        "blob",
                        "set-legal-hold",
                        "--account-name",
                        name,
                        "-c",
                        cname,
                        "-n",
                        b["name"],
                        "--version-id",
                        vid,
                        "--legal-hold",
                        "false",
                        "--auth-mode",
                        "login",
                    )
                    done.append(f"version legal hold {name}/{cname}/{b['name']}@{vid}")
                if until is None:
                    continue
                where = f"{cname}/{b['name']}@{vid}"
                if until is False or until > now:
                    when = "with no end" if until is False else f"until {iso(until)}"
                    held_here.append(where)
                    held.setdefault(name, []).append(
                        f"{where}: LOCKED {when}"
                        + ("" if until is False else "; deletable after then (rerun down)")
                    )
                    continue
                # Its retention has ended: Azure lets the version go now, so it goes.
                self._dp(
                    "storage",
                    "blob",
                    "delete",
                    "--account-name",
                    name,
                    "-c",
                    cname,
                    "-n",
                    b["name"],
                    "--version-id",
                    vid,
                    "--auth-mode",
                    "login",
                )
                done.append(
                    f"deleted expired version {name}/{where} (LOCKED, retention ended {iso(until)})"
                )
            if locked_days is not None and not held_here:
                # Nothing left under its retention: a container with a locked policy can be
                # deleted once it holds no protected blob, and then the account can go.
                self._dp(
                    "storage",
                    "container",
                    "delete",
                    "--account-name",
                    name,
                    "-n",
                    cname,
                    "--auth-mode",
                    "login",
                )
                done.append(f"container {name}/{cname} (LOCKED policy, nothing under retention)")
        return done

    def _locked_until(self, pol: dict, props: dict, locked_days: int | None):
        """When a version's LOCKED protection ends: the later of its own locked policy's
        expiry and its container's locked days from its creation. None when nothing locked
        protects it; False when a locked protection has no readable end (held, never forced)."""
        ends = []
        if pol.get("policyMode") == "Locked":
            try:
                ends.append(parse_iso(pol.get("expiryTime") or ""))
            except ValueError:
                return False
        if locked_days is not None:
            try:
                created = parse_iso(props.get("creationTime") or "")
            except ValueError:
                return False
            ends.append(created + timedelta(days=locked_days))
        return max(ends) if ends else None

    def _delete_group(self, rg: str, held: dict[str, list[str]]) -> list[str]:
        """The whole group, or, when a WORM account in it is held, everything else in it one
        by one (the VM and PostgreSQL first of all), leaving the held account as residue."""
        ctx = self.ctx
        if not held:
            ctx.run(["az", "group", "delete", "-n", rg, "--yes"])
            return [f"group {rg}"]
        keep = {n.lower() for n in held}
        ids = [
            r["id"]
            for r in ctx.az("resource", "list", "-g", rg) or []
            if r["name"].lower() not in keep
        ]
        done = []
        for rid in ids:
            p = ctx.run(["az", "resource", "delete", "--ids", rid], check=False)
            if p.returncode == 0:
                done.append(f"resource {rid.rsplit('/', 1)[-1]}")
        return done

    # ------------------------------------------------------------- vaults

    def _deleted_vaults(self, rid: str) -> list[dict]:
        names = set(self.ctx.state.get("vault_names", [])) if rid == self.ctx.run_id else set()
        out = []
        for v in self.ctx.az("keyvault", "list-deleted", "--resource-type", "vault") or []:
            tagged = (v.get("properties", {}).get("tags") or {}).get(TAG_RUN) == rid
            if tagged or v["name"] in names:
                out.append(v)
        return out

    # -------------------------------------------------------------- Entra

    def _name_filters(self, prefix: str) -> list[str]:
        return [
            f"startswith(displayName,'OmniFile {prefix} ')",
            f"startswith(displayName,'{prefix}-')",
        ]

    def _apps(self, rid: str) -> list[dict]:
        """Every Entra application of the run: by tag, by recorded id, and by the run's names
        when nothing says it is another run's (ownership.py). The rest is logged, not taken."""
        ctx, found = self.ctx, {}
        prefix = self._prefix(rid)
        lists = [ctx.az("ad", "app", "list", "--filter", f"tags/any(t:t eq '{TAG_RUN}:{rid}')")]
        lists += [ctx.az("ad", "app", "list", "--filter", f) for f in self._name_filters(prefix)]
        for lst in lists:
            for a in lst or []:
                found[a["id"]] = a
        recorded = set(ctx.state.get("entra_app_ids", [])) if rid == ctx.run_id else set()
        if recorded:
            known = {a.get("appId") for a in found.values()}
            for app_id in sorted(recorded - known):
                a = ctx.az("ad", "app", "show", "--id", app_id, check=False)
                if a:
                    found[a["id"]] = a
        mine = []
        for a in found.values():
            why = why_not_ours(
                ctx,
                rid,
                runs=runs_in_tags(a.get("tags")),
                created=a.get("createdDateTime"),
                recorded=a.get("appId") in recorded,
            )
            if why is None:
                mine.append(a)
            else:
                self._not_ours(f"Entra application {a.get('displayName')!r} ({a['id']})", why)
        return mine

    def _not_ours(self, what: str, why: str) -> None:
        seen = self.__dict__.setdefault("_said", set())
        if what not in seen:
            seen.add(what)
            self.ctx.log(f"leaving {what} alone: {why}")

    def _groups_of(self, rid: str) -> list[dict]:
        """The run's Entra groups (ofreal-<run id>-...): named for the whole run id, and kept
        only when their description names no other run and they are the run's (ownership)."""
        ctx, mine = self.ctx, []
        found: dict[str, dict] = {}
        flt = f"startswith(displayName,'ofreal-{rid}-')"
        for g in ctx.az("ad", "group", "list", "--filter", flt) or []:
            found[g["id"]] = g
        for g in found.values():
            why = why_not_ours(
                ctx,
                rid,
                runs=runs_in_text(g.get("description")),
                created=g.get("createdDateTime"),
                recorded=rid == ctx.run_id and g["id"] == ctx.state.get("pg_admin_oid"),
            )
            if why is None:
                mine.append(g)
            else:
                self._not_ours(f"Entra group {g.get('displayName')!r} ({g['id']})", why)
        return mine

    def _teams_apps(self, rid: str) -> list[dict]:
        ctx, found = self.ctx, {}
        if rid != ctx.run_id:
            return []
        for cid in ctx.state.get("teams_catalog_ids", []):
            a = ctx.az(
                "rest",
                "--method",
                "get",
                "--url",
                f"{GRAPH}/appCatalogs/teamsApps/{cid}",
                check=False,
            )
            if a:
                found[a["id"]] = a
        ext = self._manifest_id()
        if ext:
            got = (
                ctx.az(
                    "rest",
                    "--method",
                    "get",
                    "--url",
                    f"{GRAPH}/appCatalogs/teamsApps?$filter=externalId eq '{ext}'",
                )
                or {}
            )
            for a in got.get("value", []):
                found[a["id"]] = a
        return list(found.values())

    def _manifest_id(self) -> str | None:
        z = self.ctx.out / "release" / "deploy" / "teams" / "out" / "omnifile-teams-app.zip"
        try:
            with zipfile.ZipFile(z) as f:
                return json.loads(f.read("manifest.json")).get("id")
        except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
            return None

    def adopt_entra(self) -> None:
        """Tag (and record) the applications the guide's steps made. Teardown finds them by
        name and recorded id anyway; the tag is what an expired-run sweep looks for."""
        ctx = self.ctx
        apps = self._apps(ctx.run_id)
        for a in apps:
            tags = sorted(
                set(a.get("tags") or [])
                | {f"{TAG_RUN}:{ctx.run_id}", f"{TAG_HARNESS}:{HARNESS_TAG_VALUE}"}
            )
            if tags != sorted(a.get("tags") or []):
                ctx.az(
                    "rest",
                    "--method",
                    "patch",
                    "--url",
                    f"{GRAPH}/applications/{a['id']}",
                    "--body",
                    json.dumps({"tags": tags}),
                )
        ctx.state["entra_app_ids"] = sorted(
            {*ctx.state.get("entra_app_ids", []), *(a["appId"] for a in apps if a.get("appId"))}
        )
        ctx.save_state()

    def _entra_down(self, rid: str) -> list[str]:
        ctx, done = self.ctx, []
        for a in self._apps(rid):
            ctx.run(["az", "ad", "app", "delete", "--id", a["id"]])
            done.append(f"app {a.get('displayName')}")
        for t in self._teams_apps(rid):
            ctx.run(
                [
                    "az",
                    "rest",
                    "--method",
                    "delete",
                    "--url",
                    f"{GRAPH}/appCatalogs/teamsApps/{t['id']}",
                ]
            )
            done.append(f"teams app {t['id']}")
        for g in self._groups_of(rid):
            ctx.run(["az", "ad", "group", "delete", "--group", g["id"]])
            done.append(f"group {g['displayName']}")
        return done

    # -------------------------------------------------------------- proof

    def _prove(self, rid: str, groups: list[str], held: dict[str, list[str]]) -> list[str]:
        """Nothing of the run is left: no group (by tag or by name), resource, custom role
        scoped to its groups, soft-deleted vault, Entra application (tagged or not), Teams
        catalog app or admin group. A held WORM account is listed with why."""
        ctx, left = self.ctx, []
        sub = ctx.state["identity"]["subscription"]
        for g in ctx.az("group", "list", "--tag", f"{TAG_RUN}={rid}") or []:
            left.append(f"resource group {g['name']}")
        if (
            ctx.run(
                ["az", "group", "show", "-n", f"rg-ofreal-{rid}", "-o", "json"], check=False
            ).returncode
            == 0
            and f"resource group rg-ofreal-{rid}" not in left
        ):
            left.append(f"resource group rg-ofreal-{rid}")
        for r in ctx.az("resource", "list", "--tag", f"{TAG_RUN}={rid}") or []:
            left.append(f"resource {r['id']}")
        scopes = {f"/subscriptions/{sub}/resourcegroups/{g}".lower() for g in groups}
        for d in (
            ctx.az(
                "role",
                "definition",
                "list",
                "--custom-role-only",
                "true",
                "--scope",
                f"/subscriptions/{sub}",
            )
            or []
        ):
            if any(s.lower() in scopes for s in d.get("assignableScopes", [])):
                left.append(f"role definition {d['roleName']}")
        for v in self._deleted_vaults(rid):
            left.append(f"soft-deleted vault {v['name']}")
        for a in self._apps(rid):
            left.append(f"Entra application {a.get('displayName')}")
        for t in self._teams_apps(rid):
            left.append(f"Teams catalog app {t['id']}")
        for g in self._groups_of(rid):
            left.append(f"Entra group {g['displayName']}")
        for acct, why in sorted(held.items()):
            left.append(f"WORM account {acct} held: {'; '.join(why)}")
        return sorted(set(left))
