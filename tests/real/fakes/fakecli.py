#!/usr/bin/env python3
"""The dry run's stand-ins for az, aws, gh, ssh, scp, docker and node (tests/real/fakes/bin).

Every call is appended to the transcript ($REAL_FAKE_TRANSCRIPT), with the repo and the
run's output directory written as $REPO and $OUT, so the tests can compare it with the
golden transcripts in tests/real/golden/. Nothing here reaches a network except the aws
shim, which calls the run's moto server on 127.0.0.1.

az keeps a small model of what the harness touches ($REAL_FAKE_STATE): resource groups,
the resources a deployment of main.bicep with test.bicepparam makes (azure_deployment.json),
custom role definitions, soft-deleted vaults, Entra applications and groups, and the WORM
account's container policies and blob versions. It refuses what Azure refuses, where the
harness depends on it: deleting a group whose WORM account still holds a lock, an
immutability policy or a legal hold; purging a vault under purge protection. An az command
it doesn't know fails (exit 2), so a dry run can't pass over a command nobody modelled.

REAL_FAKE_FAIL=<regex> fails any call whose command line matches it (exit 1), to inject a
failure inside a step.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import guide_models  # tests/real/fakes, next to this file

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SUB = "00000000-0000-4000-8000-0000000000a1"
TENANT = "00000000-0000-4000-8000-0000000000b1"
OWNER = "00000000-0000-4000-8000-0000000000c1"
#: The az verbs that change something (the subscription each acted in is recorded).
MUTATING = {"create", "delete", "update", "add", "purge", "deploy", "assign", "upload", "remove"}


def norm(text: str) -> str:
    text = text.replace(str(REPO), "$REPO")
    out = os.environ.get("REAL_FAKE_OUT")
    if out:
        text = text.replace(out, "$OUT")
    moto = os.environ.get("REAL_FAKE_MOTO")
    if moto:
        text = text.replace(moto, "$MOTO")
    return text


SECRET_OPTS = {"--value", "--password", "--secret", "--client-secret", "--admin-password"}


def record(tool: str, argv: list[str], note: str = "") -> None:
    shown = [("<secret>" if i and argv[i - 1] in SECRET_OPTS else a) for i, a in enumerate(argv)]
    line = norm(" ".join([tool, *shown]))
    line = re.sub(r"-----BEGIN ([A-Z ]+)-----.*?-----END \1-----", r"<\1 PEM>", line, flags=re.S)
    line = line.replace("\n", "\\n")
    if note:
        line += f"  # {note}"
    with open(os.environ["REAL_FAKE_TRANSCRIPT"], "a") as f:
        f.write(line + "\n")


def fail_injected(tool: str, argv: list[str]) -> None:
    pat = os.environ.get("REAL_FAKE_FAIL")
    if pat and re.search(pat, " ".join([tool, *argv])):
        print(f"fake {tool}: injected failure ({pat})", file=sys.stderr)
        sys.exit(1)


# ------------------------------------------------------------------ state


def load() -> dict[str, Any]:
    p = Path(os.environ["REAL_FAKE_STATE"])
    if p.exists():
        return json.loads(p.read_text())
    s = {
        "groups": {},
        "resources": [],
        "role_defs": [],
        "deleted_vaults": [],
        "apps": [],
        "ad_groups": [],
        "deployments": {},
        "locks": [],
        "worm": {},
        "teams_apps": [],
        "assignments": [],
        "secrets": {},
        "seq": 0,
    }
    seed = os.environ.get("REAL_FAKE_SEED")
    if seed:
        s.update(json.loads(Path(seed).read_text()))
    return s


def save(s: dict[str, Any]) -> None:
    Path(os.environ["REAL_FAKE_STATE"]).write_text(json.dumps(s, indent=2, sort_keys=True))


def new_id(s: dict[str, Any]) -> str:
    s["seq"] += 1
    return str(uuid.UUID(int=0xF00D << 96 | s["seq"]))


# ---------------------------------------------------------------- parsing


def split(argv: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    verbs, opts, key = [], {}, None
    for a in argv:
        if a.startswith("--") or (a.startswith("-") and len(a) == 2 and not a[1].isdigit()):
            key = a
            opts.setdefault(key, [])
        elif key is None:
            verbs.append(a)
        else:
            opts[key].append(a)
    return verbs, opts


def opt(opts: dict[str, list[str]], *names: str, default: str | None = None) -> str | None:
    for n in names:
        if n in opts:
            v = opts[n]
            return v[0] if v else "true"
    return default


def tags_of(values: list[str]) -> dict[str, str]:
    out = {}
    for v in values:
        k, _, x = v.partition("=")
        out[k] = x
    return out


def emit(obj: Any, opts: dict[str, list[str]]) -> None:
    q = opt(opts, "--query")
    if q:
        import jmespath

        obj = jmespath.search(q, obj)
    o = opt(opts, "-o", "--output", default="json")
    if o == "none" or obj is None:
        return
    if o == "tsv":

        def tsv(x):
            return str(x).lower() if isinstance(x, bool) else str(x)

        if isinstance(obj, list):
            print("\n".join(tsv(x) for x in obj))
        else:
            print(tsv(obj))
        return
    print(json.dumps(obj, indent=2))


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------- az


def rg_id(rg: str) -> str:
    return f"/subscriptions/{SUB}/resourceGroups/{rg}"


def match_tag(tags: dict[str, str], want: str | None) -> bool:
    if want is None:
        return True
    k, _, v = want.partition("=")
    return k in tags and (not v or tags[k] == v)


def deploy(s: dict[str, Any], rg: str, name: str, opts=None) -> dict[str, Any]:
    tpl = json.loads((HERE / "azure_deployment.json").read_text())
    # The parameters' tags: customer.bicepparam's OMNI_TEST_RUN_* (or test.bicepparam's).
    rid = os.environ.get("OMNI_TEST_RUN_ID") or os.environ.get("OMNIFILE_RUN_ID")
    exp = os.environ.get("OMNI_TEST_RUN_EXPIRES") or os.environ.get("OMNIFILE_RUN_EXPIRES")
    if not rid or not exp:
        die("the parameters read no run id: OMNI_TEST_RUN_ID / OMNIFILE_RUN_ID unset", 1)
    tags = {
        "product": "omnifile-gateway",
        "omnifile-run": rid,
        "omnifile-expires": exp,
        "omnifile-harness": "tests-real",
    }
    p = prefix_of(opts)
    have = {r["id"] for r in s["resources"]}
    for r in tpl["resources"]:
        n = r["name"].replace("{p}", p)
        rid_ = f"{rg_id(rg)}/providers/{r['type']}/{n}"
        if rid_ in have:
            continue
        res = {"id": rid_, "name": n, "type": r["type"], "resourceGroup": rg, "tags": tags}
        if r["type"].endswith("storageAccounts"):
            res["immutableStorageWithVersioning"] = {"enabled": r.get("kind") == "worm"}
            if r.get("kind") == "worm":
                s["worm"][n] = {
                    "containers": {
                        c: {
                            "policy": {"state": "Unlocked", "etag": f'"{new_id(s)}"', "days": 1},
                            "legal_hold": [],
                            "blobs": [],
                        }
                        for c in tpl["worm_containers"]
                        if c == "data"
                    }
                }
                for c in tpl["worm_containers"]:
                    s["worm"][n]["containers"].setdefault(
                        c, {"policy": {}, "legal_hold": [], "blobs": []}
                    )
        if r["type"].endswith("vaults"):
            res["properties"] = {
                "enablePurgeProtection": os.environ.get("REAL_FAKE_PURGE_PROTECTION") == "1",
                "location": s["groups"][rg]["location"],
            }
        s["resources"].append(res)
    for rd in tpl["role_definitions"]:
        if not any(
            d["roleName"] == rd and rg_id(rg) in d["assignableScopes"] for d in s["role_defs"]
        ):
            s["role_defs"].append(
                {
                    "name": new_id(s),
                    "roleName": rd,
                    "roleType": "CustomRole",
                    "assignableScopes": [rg_id(rg)],
                }
            )

    def fill(v):
        if isinstance(v, dict):
            return {k: fill(x) for k, x in v.items()}
        if isinstance(v, list):
            return [fill(x) for x in v]
        return v.replace("{p}", p).replace("{rgid}", rg_id(rg)) if isinstance(v, str) else v

    outs = {
        k: {"type": "Object" if isinstance(v, dict) else "String", "value": fill(v)}
        for k, v in tpl["outputs"].items()
    }
    dep = {
        "name": name,
        "properties": {
            "provisioningState": "Succeeded",
            "outputs": outs,
            "parameters": {"namePrefix": {"value": p}},
            "timestamp": f"2026-11-03T00:{len(s['deployments'].get(rg, [])):02d}:00Z",
        },
    }
    s["deployments"].setdefault(rg, []).append(dep)
    return dep


def soft_deleted(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": r["name"],
        "properties": {
            "tags": r["tags"],
            "location": r["properties"]["location"],
            "purgeProtectionEnabled": r["properties"]["enablePurgeProtection"],
        },
    }


def fake_now() -> str:
    return os.environ.get("REAL_NOW", "2026-11-03T00:00:00Z")


def blob_protected(c: dict[str, Any], b: dict[str, Any]) -> bool:
    """As Azure: a legal hold, an unlocked policy (until removed), a LOCKED version policy
    until its expiry, and a LOCKED container policy for its days from the blob's creation."""
    pol = b.get("policy") or {}
    if b.get("legal"):
        return True
    if pol.get("policyMode") == "Locked":
        if (pol.get("expiryTime") or "9999") > fake_now():
            return True
    elif pol:
        return True
    cp = c.get("policy") or {}
    if cp.get("state") == "Locked":
        from datetime import datetime, timedelta

        created = datetime.strptime(b.get("created") or fake_now(), "%Y-%m-%dT%H:%M:%SZ")
        until = (created + timedelta(days=cp.get("days", 0))).strftime("%Y-%m-%dT%H:%M:%SZ")
        if until > fake_now():
            return True
    return False


def worm_container_blocks(a: str, cn: str, c: dict[str, Any]) -> list[str]:
    why = []
    if c["policy"].get("state") == "Unlocked":
        why.append(f"{a}/{cn} has an immutability policy")
    if c["legal_hold"]:
        why.append(f"{a}/{cn} has a legal hold")
    for b in c["blobs"]:
        if blob_protected(c, b):
            why.append(f"{a}/{cn}/{b['name']}@{b['versionId']} is immutable")
    return why


def worm_blocks_account(s: dict[str, Any], a: str) -> list[str]:
    why = []
    for cn, c in s["worm"].get(a, {}).get("containers", {}).items():
        why += worm_container_blocks(a, cn, c)
    return why


def worm_blocks(s: dict[str, Any], rg: str) -> list[str]:
    why = []
    accts = [
        r["name"]
        for r in s["resources"]
        if r["resourceGroup"] == rg
        and (r.get("immutableStorageWithVersioning") or {}).get("enabled")
    ]
    for lock in s["locks"]:
        if lock["rg"] == rg:
            why.append(f"lock {lock['name']}")
    for a in accts:
        why += worm_blocks_account(s, a)
    return why


def prefix_of(opts) -> str:
    """The namePrefix the parameters give (WP1's whatif.sh compiles them to a file), else the
    test parameters' environment."""
    got = (guide_models.params_of(opts or {}).get("namePrefix") or {}).get("value")
    return got or os.environ.get("OMNIFILE_TEST_PREFIX") or "rxxxxx"


def what_if(s: dict[str, Any], rg: str, opts=None) -> dict[str, Any]:
    tpl = json.loads((HERE / "azure_deployment.json").read_text())
    p = prefix_of(opts)
    tags = {
        "omnifile-run": os.environ.get("OMNIFILE_RUN_ID", ""),
        "omnifile-expires": os.environ.get("OMNIFILE_RUN_EXPIRES", ""),
    }
    have = {x["id"] for x in s["resources"]}
    changes = []
    for r in tpl["resources"]:
        rid_ = f"{rg_id(rg)}/providers/{r['type']}/{r['name'].replace('{p}', p)}"
        # As Azure: what exists already is a Modify, so a second deploy's preview (and its
        # digest) differs from the first's.
        kind = "Modify" if rid_ in have else "Create"
        changes.append({"changeType": kind, "resourceId": rid_, "after": {"tags": tags}})
    return {"status": "Succeeded", "error": None, "changes": changes}


def az(argv: list[str]) -> None:
    verbs, opts = split(argv)
    s = load()
    v = " ".join(verbs)
    g = opt(opts, "-g", "--resource-group", "--name", "-n")

    if v == "version":
        emit({"azure-cli": "2.70.0", "extensions": {}}, opts)
    elif v == "bicep version":
        print("Bicep CLI version 0.48.1 (fake: dry run)")
    elif v == "account show":
        emit(profile_default(), opts)
    elif v == "account set":
        # As az: only a subscription the sign-in knows (one in azureProfile.json, or the
        # fake's own when there is no profile yet) can become the default.
        sub_id = opt(opts, "--subscription", "-s")
        subs = profile_subscriptions()
        if not any(x.get("id") == sub_id for x in subs):
            die(f"The subscription of '{sub_id}' doesn't exist in cloud 'AzureCloud'.", 1)
        write_profiles([{**x, "isDefault": x.get("id") == sub_id} for x in subs])
    elif v == "account get-access-token":
        emit({"accessToken": "fake-token", "expiresOn": "2026-11-03 01:00:00"}, opts)
    elif v == "group create":
        # A PUT: on an existing group it replaces the tags with what this call gives, as
        # Azure does (a guide step's `az group create` strips the run's tags).
        rg = opt(opts, "-n", "--name")
        t = tags_of(opts.get("--tags", []))
        s["groups"][rg] = {
            "name": rg,
            "location": opt(opts, "-l", "--location"),
            "tags": t,
            "id": rg_id(rg),
        }
        save(s)
        emit(s["groups"][rg], opts)
    elif v == "group show":
        rg = opt(opts, "-n", "--name", "-g")
        if rg not in s["groups"]:
            die(f"ResourceGroupNotFound: Resource group '{rg}' could not be found.", 3)
        emit(s["groups"][rg], opts)
    elif v == "group list":
        want = opt(opts, "--tag")
        emit([x for x in s["groups"].values() if match_tag(x["tags"], want)], opts)
    elif v == "group delete":
        rg = opt(opts, "-n", "--name")
        if rg not in s["groups"]:
            die(f"ResourceGroupNotFound: {rg}", 3)
        why = worm_blocks(s, rg)
        if why:
            die("DeleteFailed: storage account(s) hold immutable data: " + "; ".join(why))
        for r in [r for r in s["resources"] if r["resourceGroup"] == rg]:
            if r["type"].endswith("vaults"):
                s["deleted_vaults"].append(soft_deleted(r))
            s["worm"].pop(r["name"], None)
        s["resources"] = [r for r in s["resources"] if r["resourceGroup"] != rg]
        s["deployments"].pop(rg, None)
        s["assignments"] = [a for a in s["assignments"] if rg_id(rg) not in a["scope"]]
        del s["groups"][rg]
        save(s)
    elif v == "resource list":
        want, rg = opt(opts, "--tag"), opt(opts, "-g", "--resource-group")
        emit(
            [
                r
                for r in s["resources"]
                if match_tag(r["tags"], want) and (rg is None or r["resourceGroup"] == rg)
            ],
            opts,
        )
    elif v == "resource delete":
        for rid in opts.get("--ids", []):
            r = next((x for x in s["resources"] if x["id"] == rid), None)
            if r is None:
                die(f"ResourceNotFound: {rid}", 3)
            if r["name"] in s["worm"] and worm_blocks_account(s, r["name"]):
                die(f"DeleteFailed: {r['name']} holds immutable data")
            if r["type"].endswith("vaults"):
                s["deleted_vaults"].append(soft_deleted(r))
            s["resources"].remove(r)
        save(s)
    elif v == "deployment group what-if":
        note_env()
        emit(what_if(s, g, opts), opts)
    elif v == "deployment group create":
        note_env()
        dep = deploy(s, g, opt(opts, "-n", "--name") or "main", opts)
        guide_models.on_deploy(
            sys.modules[__name__],
            s,
            g,
            opts,
            dep["properties"]["parameters"]["namePrefix"]["value"],
        )
        save(s)
        emit(dep, opts)
    elif v == "deployment group list":
        emit(s["deployments"].get(g, []), opts)
    elif v == "deployment group show":
        name = opt(opts, "-n", "--name")
        deps = [
            d
            for d in s["deployments"].get(opt(opts, "-g", "--resource-group"), [])
            if d["name"] == name
        ]
        if not deps:
            die("DeploymentNotFound", 3)
        emit(deps[-1], opts)
    elif v == "acr create":
        add_resource(
            s,
            opt(opts, "-g", "--resource-group"),
            opt(opts, "-n", "--name"),
            "Microsoft.ContainerRegistry/registries",
            tags_of(opts.get("--tags", [])),
        )
        emit({"loginServer": f"{opt(opts, '-n', '--name')}.azurecr.io"}, opts)
    elif v == "acr login":
        print("Login Succeeded")
    elif v == "network vnet create":
        rg, name = opt(opts, "-g", "--resource-group"), opt(opts, "-n", "--name")
        add_resource(
            s, rg, name, "Microsoft.Network/virtualNetworks", tags_of(opts.get("--tags", []))
        )
        emit(
            {
                "newVNet": {
                    "name": name,
                    "id": f"{rg_id(rg)}/providers/Microsoft.Network/virtualNetworks/{name}",
                }
            },
            opts,
        )
    elif v == "network vnet list":
        rg = opt(opts, "-g", "--resource-group")
        emit(
            [
                {"name": r["name"], "id": r["id"]}
                for r in s["resources"]
                if r["resourceGroup"] == rg and r["type"].endswith("virtualNetworks")
            ],
            opts,
        )
    elif v == "network nsg create":
        add_resource(
            s,
            opt(opts, "-g", "--resource-group"),
            opt(opts, "-n", "--name"),
            "Microsoft.Network/networkSecurityGroups",
            tags_of(opts.get("--tags", [])),
        )
        emit({"NewNSG": {"name": opt(opts, "-n", "--name")}}, opts)
    elif v in ("network nsg rule create", "network vnet subnet create"):
        emit({"name": opt(opts, "-n", "--name"), "provisioningState": "Succeeded"}, opts)
    elif v == "network list-service-tags":
        print((REPO / "deploy/azure/testdata/service-tags.json").read_text())
    elif v == "vm create":
        rg, name = opt(opts, "-g", "--resource-group"), opt(opts, "-n", "--name")
        t = tags_of(opts.get("--tags", []))
        for typ, n in (
            ("Microsoft.Compute/virtualMachines", name),
            ("Microsoft.Network/networkInterfaces", f"{name}VMNic"),
            ("Microsoft.Network/publicIPAddresses", f"{name}PublicIP"),
            ("Microsoft.Compute/disks", f"{name}_OsDisk"),
        ):
            add_resource(s, rg, n, typ, t)
        emit(
            {
                "publicIpAddress": "192.0.2.10",
                "privateIpAddress": "10.60.1.4",
                "identity": {"systemAssignedIdentity": "77777777-8888-9999-aaaa-bbbbbbbbbbbb"},
                "powerState": "VM running",
            },
            opts,
        )
    elif v == "role assignment create":
        a = {
            "id": new_id(s),
            "scope": opt(opts, "--scope"),
            "role": opt(opts, "--role"),
            "principalId": opt(opts, "--assignee-object-id"),
        }
        s["assignments"].append(a)
        save(s)
        emit(a, opts)
    elif v == "role definition list":
        emit(s["role_defs"], opts)
    elif v == "role definition delete":
        name = opt(opts, "--name")
        s["role_defs"] = [d for d in s["role_defs"] if d["name"] != name]
        save(s)
    elif v == "lock list":
        rg = opt(opts, "-g", "--resource-group")
        emit([x for x in s["locks"] if x["rg"] == rg], opts)
    elif v == "lock delete":
        lid = opt(opts, "--ids")
        s["locks"] = [x for x in s["locks"] if x["id"] != lid]
        save(s)
    elif v == "storage account list":
        rg = opt(opts, "-g", "--resource-group")
        emit(
            [
                r
                for r in s["resources"]
                if r["resourceGroup"] == rg and r["type"].endswith("storageAccounts")
            ],
            opts,
        )
    elif v in ("storage account update", "storage account network-rule add"):
        emit({"name": opt(opts, "-n", "--name", "--account-name")}, opts)
    elif v == "storage container list":
        data_plane_not_yet(s, argv)
        a = s["worm"].get(opt(opts, "--account-name"), {"containers": {}})
        emit([{"name": n, "properties": {}} for n in a["containers"]], opts)
    elif v == "storage container legal-hold show":
        c = worm_c(s, opts)
        emit({"hasLegalHold": bool(c["legal_hold"]), "tags": c["legal_hold"]}, opts)
    elif v == "storage container immutability-policy show":
        c = worm_c(s, opts)
        if not c["policy"]:
            die("ImmutabilityPolicyNotFound", 3)
        emit(
            {
                "state": c["policy"]["state"],
                "etag": c["policy"]["etag"],
                "immutabilityPeriodSinceCreationInDays": c["policy"]["days"],
            },
            opts,
        )
    elif v == "storage container immutability-policy delete":
        c = worm_c(s, opts)
        if c["policy"].get("state") == "Locked":
            die("ContainerImmutabilityPolicyFailure: a locked policy can't be deleted")
        if opt(opts, "--if-match") != c["policy"].get("etag"):
            die("ConditionNotMet: etag")
        c["policy"] = {}
        save(s)
    elif v == "storage container legal-hold clear":
        c = worm_c(s, opts)
        c["legal_hold"] = [t for t in c["legal_hold"] if t not in opts.get("--tags", [])]
        save(s)
        emit({"hasLegalHold": bool(c["legal_hold"]), "tags": c["legal_hold"]}, opts)
    elif v == "storage blob list":
        data_plane_not_yet(s, argv)
        c = worm_c(s, opts)
        emit(
            [
                {"name": b["name"], "versionId": b["versionId"], "properties": {}}
                for b in c["blobs"]
            ],
            opts,
        )
    elif v == "storage blob show":
        data_plane_not_yet(s, argv)
        c = worm_c(s, opts)
        b = next(
            (
                b
                for b in c["blobs"]
                if b["name"] == opt(opts, "-n", "--name")
                and b["versionId"] == opt(opts, "--version-id")
            ),
            None,
        )
        if b is None:
            die("BlobNotFound", 3)
        emit(
            {
                "name": b["name"],
                "versionId": b["versionId"],
                "properties": {
                    "immutabilityPolicy": b.get("policy") or {},
                    "hasLegalHold": bool(b.get("legal")),
                    "creationTime": b.get("created") or fake_now(),
                },
            },
            opts,
        )
    elif v == "storage blob delete":
        data_plane_not_yet(s, argv)
        c = worm_c(s, opts)
        n, vid = opt(opts, "-n", "--name"), opt(opts, "--version-id")
        b = next((b for b in c["blobs"] if b["name"] == n and b["versionId"] == vid), None)
        if b is None:
            die("BlobNotFound", 3)
        if blob_protected(c, b):
            die("BlobImmutableDueToPolicy: this version is still immutable", 1)
        c["blobs"].remove(b)
        save(s)
    elif v == "storage container delete":
        data_plane_not_yet(s, argv)
        a = s["worm"].get(opt(opts, "--account-name"), {"containers": {}})
        cn = opt(opts, "-n", "--name")
        c = a["containers"].get(cn)
        if c is None:
            die("ContainerNotFound", 3)
        if c["blobs"] or c["legal_hold"]:
            die("ContainerProtectedFromDeletion: it holds blobs or a legal hold", 1)
        del a["containers"][cn]
        save(s)
        emit({"deleted": True}, opts)
    elif v in ("storage blob immutability-policy delete", "storage blob set-legal-hold"):
        data_plane_not_yet(s, argv)
        c = worm_c(s, opts)
        for b in c["blobs"]:
            if b["name"] == opt(opts, "-n", "--name") and b["versionId"] == opt(
                opts, "--version-id"
            ):
                if v.endswith("delete"):
                    if (b.get("policy") or {}).get("policyMode") == "Locked":
                        die("ImmutabilityPolicyLocked")
                    b["policy"] = {}
                else:
                    b["legal"] = opt(opts, "--legal-hold") == "true"
        save(s)
    elif v == "keyvault list":
        rg = opt(opts, "-g", "--resource-group")
        emit(
            [
                {"name": r["name"], "id": r["id"]}
                for r in s["resources"]
                if r["resourceGroup"] == rg and r["type"].endswith("vaults")
            ],
            opts,
        )
    elif v == "keyvault list-deleted":
        emit(s["deleted_vaults"], opts)
    elif v == "keyvault purge":
        n = opt(opts, "-n", "--name")
        dv = [x for x in s["deleted_vaults"] if x["name"] == n]
        if not dv:
            die("VaultNotFound", 3)
        if dv[0]["properties"]["purgeProtectionEnabled"]:
            die("Operation 'DeletedVaultPurge' is not allowed: purge protection is enabled")
        s["deleted_vaults"] = [x for x in s["deleted_vaults"] if x["name"] != n]
        save(s)
    elif v == "keyvault secret set":
        s["secrets"][opt(opts, "-n", "--name")] = "set"
        save(s)
        emit({"id": f"https://fake.vault/secrets/{opt(opts, '-n', '--name')}"}, opts)
    elif v == "containerapp replica list":
        app = opt(opts, "-n", "--name") or "app"
        emit([{"name": f"{app}--rev1-0f00d", "properties": {"runningState": "Running"}}], opts)
    elif v == "containerapp exec":
        # No TTY, as the guide's tools/enrol-exec.sh runs it: the CA bootstrap writes the
        # certificate, `cat` prints it, and the relay certificate comes with its SHA-256.
        cmd = opt(opts, "--command") or ""
        pem = guide_models.ca_pem()
        if "issue-relay-cert" in cmd and "--csr-b64" in cmd:
            print(guide_models.issue_relay_cert(cmd.split("--csr-b64", 1)[1].split()[0]), end="")
        elif "init-kv-ca" in cmd:
            print(f"wrote {cmd.split()[-1]}: fake: dry-run edge CA (not a real key)")
        elif "cat " in cmd and ".pem" in cmd:
            print(pem, end="")
        elif "relay" in cmd and ("issue" in cmd or "cert" in cmd):
            print(pem, end="")
            print("SHA-256: " + hashlib.sha256(pem.encode()).hexdigest())
        elif "base64" in cmd and "exec(" in cmd:
            # the T5 probe (RA-30): direct TLS refuses both handshakes without a client cert
            print(
                'ofreal-t5-probe {"no_client_cert": {"tls": "refused", "error": "SSLError"}, '
                '"forged_xfcc": {"tls": "refused", "error": "SSLError"}}'
            )
        elif "date -u +%s" in cmd:
            print(os.environ.get("REAL_FAKE_EPOCH", "1793664000"))
        else:
            print(json.dumps({"ok": True, "status": 401, "dry_run": True}))
    elif v in (
        "containerapp logs show",
        "monitor log-analytics query",
        "monitor app-insights query",
    ):
        emit(canned("logs", verbs, opts), opts)
    elif v == "monitor scheduled-query list":
        emit(
            [
                {
                    "name": f"alert-{i}",
                    "criteria": {"allOf": [{"query": "AppMetrics | where Name == 'x'"}]},
                }
                for i in range(8)
            ],
            opts,
        )
    elif v == "ad signed-in-user show":
        emit({"id": OWNER, "userPrincipalName": "owner@fake.example"}, opts)
    elif v == "ad group create":
        grp = {
            "id": new_id(s),
            "displayName": opt(opts, "--display-name"),
            "description": opt(opts, "--description") or "",
            "createdDateTime": os.environ.get("REAL_NOW", ""),
        }
        s["ad_groups"].append(grp)
        save(s)
        emit(grp, opts)
    elif v == "ad group member add":
        gid, member = opt(opts, "--group", "-g"), opt(opts, "--member-id")
        for x in s["ad_groups"]:
            if gid in (x["id"], x["displayName"]):
                x.setdefault("members", [])
                if member not in x["members"]:
                    x["members"].append(member)
        save(s)
    elif v == "ad group list":
        emit([x for x in s["ad_groups"] if odata(x, opt(opts, "--filter"))], opts)
    elif v == "ad group delete":
        gid = opt(opts, "--group", "-g")
        s["ad_groups"] = [x for x in s["ad_groups"] if x["id"] != gid]
        save(s)
    elif v == "ad app create":
        app = {
            "id": new_id(s),
            "appId": new_id(s),
            "displayName": opt(opts, "--display-name"),
            "tags": [],
            "createdDateTime": os.environ.get("REAL_NOW", ""),
            "appRoles": app_roles(opts),
        }
        s["apps"].append(app)
        save(s)
        emit(app, opts)
    elif v == "ad app list":
        name = opt(opts, "--display-name")
        emit(
            [
                x
                for x in s["apps"]
                if odata(x, opt(opts, "--filter")) and (name is None or x["displayName"] == name)
            ],
            opts,
        )
    elif v in (
        "ad app update",
        "ad app permission add",
        "ad app permission admin-consent",
        "ad app federated-credential create",
        "ad app federated-credential delete",
    ):
        # register.sh's settings on its app: accepted (the app must exist), not modelled.
        aid = opt(opts, "--id")
        if not any(aid in (x["id"], x["appId"]) for x in s["apps"]):
            die(f"Resource '{aid}' does not exist", 3)
    elif v == "ad app federated-credential list":
        emit([], opts)
    elif v == "ad sp show":
        sid = opt(opts, "--id")
        if sid == GRAPH_APP:
            emit(graph_sp(), opts)
        elif sid == ARM_APP:
            emit(arm_sp(), opts)
        else:
            sp = next((x for x in s.setdefault("sps", []) if sid in (x["id"], x["appId"])), None)
            if sp is None:
                die(f"Resource '{sid}' does not exist", 3)
            app = next((a for a in s["apps"] if a["appId"] == sp["appId"]), {})
            # A service principal shows its application's roles (register.sh's --app-roles).
            emit({**sp, "appRoles": app.get("appRoles", [])}, opts)
    elif v == "ad app show":
        aid = opt(opts, "--id")
        a = next((x for x in s["apps"] if aid in (x["id"], x["appId"])), None)
        if a is None:
            die(f"Resource '{aid}' does not exist", 3)
        emit(a, opts)
    elif v == "ad app delete":
        aid = opt(opts, "--id")
        s["apps"] = [x for x in s["apps"] if aid not in (x["id"], x["appId"])]
        save(s)
    elif v == "ad sp create":
        sp = {"id": new_id(s), "appId": opt(opts, "--id")}
        s.setdefault("sps", []).append(sp)
        save(s)
        emit(sp, opts)
    elif v == "ad app credential reset":
        emit({"appId": opt(opts, "--id")}, opts)
    elif v == "rest":
        rest(s, opts)
    elif guide_models.az(sys.modules[__name__], s, v, opts):
        pass
    else:
        die(f"fake az: no model of `az {v}` (add it to tests/real/fakes/fakecli.py)", 2)


#: Names whose values never go into a transcript (a golden is committed).
SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|_KEY$|_CODE$|CREDENTIAL", re.I)


def shown_value(name: str, value: str) -> str:
    return "<secret>" if SECRET_NAME.search(name) else value


def app_roles(opts) -> list[dict[str, Any]]:
    """`--app-roles @file`, as az reads it: each role gets an id when the file gives none."""
    src = opt(opts, "--app-roles")
    if not src:
        return []
    roles = json.loads(Path(src[1:]).read_text() if src.startswith("@") else src)
    return [
        {**r, "id": r.get("id") or str(uuid.uuid5(uuid.NAMESPACE_URL, "role:" + r["value"]))}
        for r in roles
    ]


def note_env() -> None:
    """The values a .bicepparam reads from the environment (the test parameters' OMNIFILE_*
    and the customer guide's omnifile.env, OMNI_*), every one, into the transcript: a golden
    then fails on any changed value (OMNI_USER_CIDRS=0.0.0.0/1 as much as a new name)."""
    guard = ("OMNIFILE_DEPLOY_",)
    keys = sorted(
        k for k in os.environ if k.startswith(("OMNIFILE_", "OMNI_")) and not k.startswith(guard)
    )
    record(
        "env", [f"{k}={shown_value(k, os.environ[k])}" for k in keys], "what the parameters read"
    )


def note_env_file(path: str) -> None:
    """An env file copied somewhere (the edge's omnifile.env): its values, not its size."""
    try:
        text = Path(path).read_text()
    except OSError:
        return
    for ln in text.splitlines():
        m = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", ln)
        if m:
            record("  env-file", [f"{m.group(1)}={shown_value(m.group(1), m.group(2))}"])


def profile_path() -> Path | None:
    d = os.environ.get("AZURE_CONFIG_DIR")
    return Path(d) / "azureProfile.json" if d else None


def profile_default() -> dict[str, Any]:
    """The default subscription in $AZURE_CONFIG_DIR/azureProfile.json (az's own file and
    shape), or the fake's own when there is none."""
    p = profile_path()
    if p and p.exists():
        subs = json.loads(p.read_text()).get("subscriptions", [])
        for x in subs:
            if x.get("isDefault"):
                return dict(x)
    return {
        "id": SUB,
        "tenantId": os.environ.get("REAL_FAKE_TENANT", TENANT),
        "name": "fake",
        "user": {"name": "owner@fake.example", "type": "user"},
        "isDefault": True,
    }


def profile_subscriptions() -> list[dict[str, Any]]:
    p = profile_path()
    if p and p.exists():
        return json.loads(p.read_text()).get("subscriptions", [])
    return [profile_default()]


def write_profiles(subs: list[dict[str, Any]]) -> None:
    p = profile_path()
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"subscriptions": subs}))


def acted_in() -> None:
    """Every call that changes something records the subscription it acted in (the default of
    the az directory it ran with), so a test can see a run never left its own."""
    s = load()
    s.setdefault("acted_in", []).append(profile_default().get("id"))
    save(s)


def switch_owner_after(argv: list[str]) -> None:
    """REAL_FAKE_SWITCH_AFTER=<regex> and REAL_FAKE_OWNER_AZ=<dir>: once a call matching the
    regex has run, another shell's `az account set` moves the owner's own default (in DIR) to
    the other subscription, mid-phase."""
    pat, owner = os.environ.get("REAL_FAKE_SWITCH_AFTER"), os.environ.get("REAL_FAKE_OWNER_AZ")
    if not pat or not owner or not re.search(pat, " ".join(argv)):
        return
    f = Path(owner) / "azureProfile.json"
    if f.exists():
        subs = json.loads(f.read_text()).get("subscriptions", [])
        other = next((x for x in subs if not x.get("isDefault")), None)
        if other:
            for x in subs:
                x["isDefault"] = x is other
            f.write_text(json.dumps({"subscriptions": subs}))


def data_plane_not_yet(s: dict[str, Any], argv: list[str]) -> None:
    """REAL_FAKE_403=N: the first N data-plane calls (--auth-mode login) answer 403, as Azure
    does until a fresh role assignment reaches the account."""
    n = int(os.environ.get("REAL_FAKE_403", "0") or 0)
    if n and "--auth-mode" in argv:
        s["seq_403"] = s.get("seq_403", 0) + 1
        save(s)
        if s["seq_403"] <= n:
            die(
                "ERROR: AuthorizationPermissionMismatch (403): This request is not authorized "
                "to perform this operation using this permission."
            )


def add_resource(s, rg, name, typ, tags) -> None:
    if rg not in s["groups"]:
        die(f"ResourceGroupNotFound: {rg}", 3)
    s["resources"].append(
        {
            "id": f"{rg_id(rg)}/providers/{typ}/{name}",
            "name": name,
            "type": typ,
            "resourceGroup": rg,
            "tags": tags,
        }
    )
    save(s)


def worm_c(s, opts) -> dict[str, Any]:
    a = s["worm"].get(opt(opts, "--account-name"))
    c = (a or {}).get("containers", {}).get(opt(opts, "-c", "--container-name"))
    if c is None:
        die("ContainerNotFound", 3)
    return c


def odata(obj: dict[str, Any], flt: str | None) -> bool:
    if not flt:
        return True
    m = re.fullmatch(r"startswith\(displayName,'([^']*)'\)", flt)
    if m:
        return obj.get("displayName", "").startswith(m.group(1))
    m = re.fullmatch(r"tags/any\(t:t eq '([^']*)'\)", flt)
    if m:
        return m.group(1) in obj.get("tags", [])
    die(f"fake az: no model of the filter {flt!r}", 2)
    return False


#: Microsoft Graph's own application id, and the scopes and roles register.sh asks it for.
GRAPH_APP = "00000003-0000-0000-c000-000000000000"
GRAPH_SCOPES = ("openid", "profile")
GRAPH_ROLES = (
    "User.Read.All",
    "GroupMember.Read.All",
    "TeamsAppInstallation.ReadWriteSelfForUser.All",
)


#: Azure Service Management (ARM): register.sh asks it for user_impersonation (Connect Azure).
ARM_APP = "797f4846-ba00-4fd7-ba43-dac1f8f63013"


def arm_sp() -> dict[str, Any]:
    return {
        "id": "00000000-0000-4000-9000-00000000a001",
        "appId": ARM_APP,
        "oauth2PermissionScopes": [
            {"value": "user_impersonation", "id": "00000000-0000-4000-9000-000000000200"}
        ],
        "appRoles": [],
    }


def graph_sp() -> dict[str, Any]:
    def gid(i: int) -> str:
        return f"00000000-0000-4000-9000-{i:012d}"

    return {
        "id": "00000000-0000-4000-9000-00000000a000",
        "appId": GRAPH_APP,
        "oauth2PermissionScopes": [{"value": v, "id": gid(i)} for i, v in enumerate(GRAPH_SCOPES)],
        "appRoles": [{"value": v, "id": gid(100 + i)} for i, v in enumerate(GRAPH_ROLES)],
    }


def rest(s, opts) -> None:
    method = (opt(opts, "--method", "-m") or "get").lower()
    url = opt(opts, "--url", "--uri", "-u") or ""
    body = opt(opts, "--body", "-b")
    m = re.match(r".*/servicePrincipals/([^/]+)/appRoleAssignments$", url)
    # register.sh's grants to the worker identity (an Azure run); a tenant run's checks read
    # the canned answer below for an identity this fake hasn't seen granted.
    if m and (method == "post" or m.group(1) in s.get("role_assignments_graph", {})):
        got = s.setdefault("role_assignments_graph", {}).setdefault(m.group(1), [])
        if method == "post":
            got.append(json.loads(Path(body[1:]).read_text()) if body and body[0] == "@" else {})
            save(s)
            emit({}, opts)
        else:
            emit({"value": got}, opts)
        return
    m = re.match(r".*/servicePrincipals/([^/]+)/appRoleAssignedTo$", url)
    if m:
        sp = next((x for x in s.setdefault("sps", []) if x["id"] == m.group(1)), None)
        if sp is None:
            die(f"Resource '{m.group(1)}' does not exist", 3)
        got = s.setdefault("app_role_assigned_to", {}).setdefault(m.group(1), [])
        if method == "post":
            b = json.loads(body or "{}")
            app = next((a for a in s["apps"] if a["appId"] == sp["appId"]), {})
            if b.get("appRoleId") not in {r["id"] for r in app.get("appRoles", [])}:
                die("Permission being assigned was not found on application", 1)
            if any(
                x["principalId"] == b.get("principalId") and x["appRoleId"] == b.get("appRoleId")
                for x in got
            ):
                die("Permission being assigned already exists on the object", 1)
            got.append(b)
            save(s)
            emit(b, opts)
        else:
            emit({"value": got}, opts)
        return
    m = re.match(r".*/appCatalogs/teamsApps/([^/?]+)/appDefinitions$", url)
    if m and method == "post":
        if not any(a["id"] == m.group(1) for a in s["teams_apps"]):
            die("NotFound", 3)
        emit({}, opts)
        return
    m = re.match(r"https://graph\.microsoft\.com/v1\.0/applications/([^/?]+)$", url)
    if m and method == "patch":
        for a in s["apps"]:
            if a["id"] == m.group(1):
                a.update(json.loads(body or "{}"))
        save(s)
        return
    if "/appCatalogs/teamsApps" in url and method == "post":
        ext = None
        if body and body.startswith("@"):
            import zipfile

            try:
                with zipfile.ZipFile(body[1:]) as z:
                    ext = json.loads(z.read("manifest.json")).get("id")
            except (OSError, KeyError, zipfile.BadZipFile):
                ext = None
        app = {
            "id": new_id(s),
            "displayName": "OmniFile",
            "distributionMethod": "organization",
            "externalId": ext,
        }
        s["teams_apps"].append(app)
        save(s)
        emit(app, opts)
        return
    m = re.match(r".*/appCatalogs/teamsApps\?\$filter=externalId eq '([^']*)'$", url)
    if m and method == "get":
        emit({"value": [a for a in s["teams_apps"] if a.get("externalId") == m.group(1)]}, opts)
        return
    m = re.match(r".*/appCatalogs/teamsApps/([^/?]+)$", url)
    if m and method == "get":
        a = next((a for a in s["teams_apps"] if a["id"] == m.group(1)), None)
        if a is None:
            die("NotFound", 3)
        emit(a, opts)
        return
    m = re.match(r".*/appCatalogs/teamsApps/([^/?]+)$", url)
    if m and method == "delete":
        s["teams_apps"] = [a for a in s["teams_apps"] if a["id"] != m.group(1)]
        save(s)
        return
    emit(canned("graph", [method, url], opts), opts)


def canned(kind: str, key: list[str], opts) -> Any:
    """Answers for reads the checks make (Graph, logs, metrics): fakes/canned.json, first
    entry whose pattern matches the call."""
    table = json.loads((HERE / "canned.json").read_text())[kind]
    line = " ".join(key + [x for vals in opts.values() for x in vals])
    for row in table:
        if re.search(row["match"], line):
            return row["answer"]
    die(f"fake az: no canned {kind} answer for {line!r}", 2)


# -------------------------------------------------------------- ssh, scp


def ssh(argv: list[str]) -> None:
    i, flags = 0, []
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] in ("-i", "-o", "-J", "-D", "-L", "-p", "-l"):
            flags += argv[i : i + 2]
            i += 2
        else:
            flags.append(argv[i])
            i += 1
    host = argv[i] if i < len(argv) else ""
    cmd = " ".join(argv[i + 1 :])
    if "-N" in flags:
        record("ssh", ["-N", "-D", "<port>", host], "SOCKS tunnel for the portal")
        return
    data = sys.stdin.read() if not sys.stdin.isatty() else ""
    note = f"stdin {len(data.splitlines())} lines" if data else ""
    if "/vm/exit.sh" in cmd:
        step = cmd.rsplit(" ", 1)[-1]
        note = f"exit step {step}"
    record("ssh", [host, norm(cmd)], note)
    fail_injected("ssh", [host, cmd])
    if cmd == "true" or cmd.startswith(("mkdir", "cd ", "cat >")) or "tar -C" in cmd:
        return
    if "/vm/exit.sh" in cmd:
        exit_step(cmd.rsplit(" ", 1)[-1], data)
    elif "/vm/azure_checks.py" in cmd:
        cid = cmd.split("azure_checks.py", 1)[1].split()[0]
        print(
            json.dumps(
                {
                    "ok": True,
                    "id": cid,
                    "status": "pass",
                    "dry_run": True,
                    "evidence": {"note": "dry run: the VM-side check was not run"},
                }
            )
        )
    elif any(
        x in cmd
        for x in (
            "/vm/provision.sh",
            "/vm/samba-dc.sh",
            "/vm/edge-shell.sh",
            "/vm/console-answers.py",
            "/vm/pg.sh",
        )
    ):
        print(json.dumps({"ok": True, "dry_run": True}))
    else:
        die(f"fake ssh: no model of {cmd!r}", 2)


def exit_step(step: str, data: str) -> None:
    facts = {}
    if step in ("nfs3-write", "nfs4-write", "smb-write"):
        proto = step.split("-")[0]
        facts = {
            f"{proto}_path": f"/exit/{proto}/line_{proto}.segy",
            f"{proto}_sha256": hashlib.sha256(proto.encode()).hexdigest(),
        }
    if step == "worm-write":
        s = load()
        acct = next(n for n in s["worm"])
        c = s["worm"][acct]["containers"]["data"]
        for vid in ("v1", "v2"):
            c["blobs"].append(
                {
                    "name": "worm/ledger.segy",
                    "versionId": vid,
                    "policy": {"expiryTime": "2026-11-04T00:00:00Z", "policyMode": "Unlocked"},
                    "legal": vid == "v1",
                    "created": fake_now(),
                }
            )
        save(s)
    print(json.dumps({"ok": True, "step": step, "dry_run": True, "facts": facts}))


def scp(argv: list[str]) -> None:
    rest_ = [a for a in argv if not a.startswith("-")]
    src = rest_[-2] if len(rest_) >= 2 else ""
    size = os.path.getsize(src) if os.path.isfile(src) else 0
    env_file = re.search(r"\.env(\.vm)?$", src) is not None
    record(
        "scp",
        [norm(rest_[-2]) if len(rest_) >= 2 else "", rest_[-1] if rest_ else ""],
        # an env file's size depends on the checkout's path: its values follow, normalised
        "env file, its values below" if env_file else f"{size} bytes",
    )
    if env_file:
        note_env_file(src)


# --------------------------------------------------------- docker, node, gh


def docker(argv: list[str]) -> None:
    record("docker", argv)
    fail_injected("docker", argv)


def node(argv: list[str]) -> None:
    if not argv or not argv[0].endswith("tests/real/portal/driver.mjs"):
        die(f"fake node: only the portal driver runs in a dry run, not {argv}", 2)
    cfg = json.loads(Path(argv[1]).read_text())
    action = cfg["action"]
    record(
        "node",
        ["tests/real/portal/driver.mjs", action],
        f"params sha256:{hashlib.sha256(json.dumps(cfg['params'], sort_keys=True).encode()).hexdigest()[:12]}",
    )
    fail_injected("node", [action])
    out: dict[str, Any] = {"ok": True, "action": action, "dry_run": True, "facts": {}}
    if action == "guide":
        cap = {}
        for line in cfg["params"].get("lines", []):
            for name in re.findall(r"<value:([A-Za-z0-9_.-]+)>", line):
                cap[name] = f"dry-{name.lower()}"
        if any(
            ln.split()[:2] == ["click", "Copy"] or ln.startswith("click Copy")
            for ln in cfg["params"].get("lines", [])
        ):
            cap["__clipboard__"] = "ofe1.dry-run-enrolment-code"
        out["captured"] = cap
    if action == "exit-uploaded":
        out["facts"] = {"file_ids": {"nfs3": "101", "nfs4": "102", "smb": "103"}}
    if action == "exit-pin-archive":
        out["facts"] = {"pinned_file": "101"}
    if action == "exit-recall-high":
        out["facts"] = {"recall_id": "201", "priority": "high"}
    if action == "exit-worm-share":
        out["facts"] = {"worm_share": "tst", "worm_share_id": "7"}
    print(json.dumps(out))


def gh(argv: list[str]) -> None:
    record("gh", argv)
    fail_injected("gh", argv)
    if argv[:2] == ["release", "view"]:
        print(json.dumps({"tagName": "v0.0.0-dry", "assets": []}))


# --------------------------------------------------------------------- aws


def aws(argv: list[str]) -> None:
    """`aws SERVICE OP --param value ...` through boto3 to the run's moto server (and
    aws_extra for what moto lacks)."""
    import boto3
    from botocore import xform_name

    sys.path.insert(0, str(HERE.parent))
    from fakes import aws_extra  # noqa: E402

    if not os.environ.get("REAL_FAKE_MOTO"):
        die(
            "fake aws: refusing: no moto server for this dry run (REAL_FAKE_MOTO is unset); "
            "it never calls AWS",
            2,
        )
    record("aws", argv)
    fail_injected("aws", argv)
    verbs, opts = split(argv)
    if len(verbs) < 2:
        die("fake aws: want SERVICE OPERATION", 2)
    service, op = verbs[0], verbs[1]

    class Ctx:
        dry_run = True
        out = Path(os.environ["REAL_FAKE_OUT"])
        env = dict(os.environ)

    sess = boto3.Session(
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
    )
    extra = aws_extra.Extra(Ctx, os.environ.get("REAL_FAKE_MOTO"))
    sess.events.register_first("before-parameter-build", extra.stash)
    sess.events.register_first("before-call", extra.answer)
    client = sess.client(
        {"s3api": "s3"}.get(service, service), endpoint_url=os.environ.get("REAL_FAKE_MOTO")
    )
    model = client.meta.service_model
    opname = next((n for n in model.operation_names if xform_name(n, "-") == op), None)
    if opname is None:
        die(f"fake aws: {service} has no operation {op}", 2)
    shape = model.operation_model(opname).input_shape
    members = shape.members if shape else {}
    params: dict[str, Any] = {}
    for key, vals in opts.items():
        if key in ("--query", "--output", "--region", "--profile", "--endpoint-url"):
            continue
        neg = key.startswith("--no-")
        name = key[5:] if neg else key[2:]
        member = next((m for m in members if xform_name(m, "-") == name), None)
        if member is None:
            die(f"fake aws: {service} {op} has no --{name}", 2)
        params[member] = convert(members[member], vals, neg)
    resp = getattr(client, xform_name(opname))(**params)
    resp.pop("ResponseMetadata", None)
    emit(json.loads(json.dumps(resp, default=str)), opts)


def convert(shape, vals: list[str], neg: bool) -> Any:
    t = shape.type_name
    if t == "boolean":
        return not neg if not vals else vals[0].lower() == "true"
    if not vals:
        die("fake aws: a value is missing", 2)
    raw = " ".join(vals) if t != "list" else vals
    if t == "list":
        if len(vals) == 1 and vals[0].startswith("["):
            return json.loads(vals[0])
        return [convert(shape.member, [x], False) for x in vals]
    if isinstance(raw, str) and raw.startswith("file://"):
        raw = Path(raw[7:]).read_text()
    if t in ("integer", "long"):
        return int(raw)
    if t in ("structure", "map"):
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        return shorthand(raw)
    if t == "blob":
        return raw.encode()
    return raw


def shorthand(text: str) -> dict[str, Any]:
    """The CLI's shorthand `a=b,c={d=e}` (no lists), enough for deploy/aws/README.md."""
    out: dict[str, Any] = {}
    i = 0
    while i < len(text):
        j = text.index("=", i)
        key = text[i:j]
        i = j + 1
        if text[i] == "{":
            depth, k = 0, i
            while True:
                depth += {"{": 1, "}": -1}.get(text[k], 0)
                if depth == 0:
                    break
                k += 1
            out[key] = shorthand(text[i + 1 : k])
            i = k + 1
        else:
            k = text.find(",", i)
            k = len(text) if k < 0 else k
            out[key] = text[i:k]
            i = k
        if i < len(text) and text[i] == ",":
            i += 1
    return out


def main() -> None:
    tool, argv = sys.argv[1], sys.argv[2:]
    if tool == "az":
        record("az", argv)
        fail_injected("az", argv)
        verbs = split(argv)[0]
        st = load()
        # Which az directory each call ran with: a test sees whether a sign-in was copied
        # before the allow-list check.
        st.setdefault("az_config_dirs", []).append(
            [" ".join(verbs), os.environ.get("AZURE_CONFIG_DIR", "")]
        )
        save(st)
        if set(verbs) & MUTATING:
            acted_in()
        az(argv)
        switch_owner_after(argv)
    elif tool == "aws":
        aws(argv)
    elif tool == "no-network":
        record(argv[0] if argv else "?", argv[1:], "refused: no network in a dry run")
        die(
            f"{argv[0] if argv else '?'}: refused: a dry run reaches no network, and nothing "
            "models this tool (tests/real/fakes/guide_models.py)",
            2,
        )
    elif tool == "git":
        record("git", argv, "refused: no network in a dry run")
        die(f"git {argv[0] if argv else ''}: refused: a dry run reaches no remote", 2)
    elif tool in ("psql", "curl", "openssl", "uv-service", "dns"):
        F = sys.modules[__name__]
        if tool == "dns":
            guide_models.dns(F, argv)
        else:
            getattr(guide_models, tool.replace("-", "_"))(F, argv)
    else:
        {"ssh": ssh, "scp": scp, "docker": docker, "node": node, "gh": gh}[tool](argv)


if __name__ == "__main__":
    main()
