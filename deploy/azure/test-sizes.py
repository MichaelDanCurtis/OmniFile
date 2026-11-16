#!/usr/bin/env python3
"""The deployment sizes (main.bicep `deploymentSize`), checked offline against the compiled template.

    python3 deploy/azure/test-sizes.py MAIN.json
    uv run --project service python deploy/azure/test-sizes.py --evaluate MAIN.json

Without --evaluate it reads the compiled `sizes` table: standard must be today's sizing exactly (STANDARD below,
the values main.bicep deployed before the parameter existed), and small must keep what no size may change: every
app and the job on Consumption sizes, the relay, api and the worker warm, a Burstable server. It also reads the
customer guide's parameters (docs/guide/customer.bicepparam): the client's OMNI_DEPLOYMENT_SIZE picks the size, and
nothing there overrides it.

With --evaluate it also evaluates the template with the service's ARM evaluator (service/tests/test_deploy_env.py,
the one the service's deploy tests use) and checks what each module is given: standard as STANDARD, small as SMALL,
a parameter overriding its size, and the two refusals (a dedicated verifier profile at small, high availability on a
Burstable server). check.sh runs both when uv is there. Calls nothing; changes nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: What each module got before deploymentSize existed (origin/main at 9f24aa2): standard must stay exactly this.
STANDARD = {
    "app-api": {"cpu": "1", "memory": "2Gi", "minReplicas": 2, "maxReplicas": 10},
    "app-worker": {"cpu": "1", "memory": "2Gi", "minReplicas": 1, "maxReplicas": 4},
    "app-web": {"cpu": "0.5", "memory": "1Gi", "minReplicas": 2, "maxReplicas": 10},
    "app-enrol": {"cpu": "0.5", "memory": "1Gi", "minReplicas": 1, "maxReplicas": 3},
    "app-relay": {"cpu": "0.5", "memory": "1Gi", "minReplicas": 1, "maxReplicas": 3},
    "job-verifier": {"cpu": "1", "memory": "2Gi"},
    "postgres": {"skuName": "Standard_D4ds_v5", "skuTier": "GeneralPurpose", "storageSizeGB": 128,
                 "highAvailabilityMode": "Disabled"},
    "monitoring": {"retentionInDays": 90, "dailyQuotaGb": "-1"},
    "app-metrics": {"alertEvaluationFrequency": "PT5M"},
    "cae-internal": {"dedicatedProfileType": ""},
}
#: small, as wp16.md prices it.
SMALL = {
    "app-api": {"cpu": "0.5", "memory": "1Gi", "minReplicas": 1, "maxReplicas": 2},
    "app-worker": {"cpu": "0.5", "memory": "1Gi", "minReplicas": 1, "maxReplicas": 1},
    "app-web": {"cpu": "0.25", "memory": "0.5Gi", "minReplicas": 0, "maxReplicas": 1},
    "app-enrol": {"cpu": "0.25", "memory": "0.5Gi", "minReplicas": 0, "maxReplicas": 1},
    "app-relay": {"cpu": "0.25", "memory": "0.5Gi", "minReplicas": 1, "maxReplicas": 2},
    "job-verifier": {"cpu": "0.5", "memory": "1Gi"},
    "postgres": {"skuName": "Standard_B1ms", "skuTier": "Burstable", "storageSizeGB": 32,
                 "highAvailabilityMode": "Disabled"},
    "monitoring": {"retentionInDays": 30, "dailyQuotaGb": "0.5"},
    "app-metrics": {"alertEvaluationFrequency": "PT15M"},
    "cae-internal": {"dedicatedProfileType": ""},
}
#: Consumption-profile sizes: vCPU -> GiB (0.5 GiB per 0.25 vCPU).
CONSUMPTION = {"0.25": "0.5Gi", "0.5": "1Gi", "0.75": "1.5Gi", "1": "2Gi", "1.25": "2.5Gi", "1.5": "3Gi",
               "1.75": "3.5Gi", "2": "4Gi", "2.5": "5Gi", "3": "6Gi", "3.5": "7Gi", "4": "8Gi"}
#: B1ms user connections (max_connections 50, less 15 Azure reserves; PostgreSQL "Limits" page).
B1MS_USER_CONNECTIONS = 35
#: A pooled app's pool when OMNIFILE_PG_POOL_MAX is unset (api/db.py DEFAULT_POOL_MAX).
POOL_PER_REPLICA = 10
#: The verifier job: an advisory-lock session and its queue's (worker/verifier.py).
VERIFIER_CONNECTIONS = 2
#: small's catalog connections may never pass this, leaving B1ms room for an administrator and the migrations.
SMALL_BUDGET = 30
#: OMNIFILE_PG_POOL_MAX each app is given at small (standard gives none).
SMALL_POOLS = {"app-api": "4", "app-worker": "6", "app-web": "4", "app-enrol": "2"}

fails: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails.append(what)


def static(template: dict) -> None:
    sizes = template["variables"]["sizes"]
    check(set(sizes) == {"small", "standard"}, "sizes: small and standard, nothing else")
    allowed = template["parameters"]["deploymentSize"]
    check(allowed.get("defaultValue") == "standard" and sorted(allowed["allowedValues"]) == ["small", "standard"],
          "deploymentSize: small or standard, standard by default")
    st, sm = sizes["standard"], sizes["small"]
    table = {"api": "app-api", "worker": "app-worker", "web": "app-web", "enrol": "app-enrol", "relay": "app-relay",
             "verifier": "job-verifier"}
    for key, dep in table.items():
        want = STANDARD[dep]
        got = {"cpu": st[key]["cpu"], "memory": st[key]["memory"]}
        check(got == {"cpu": want["cpu"], "memory": want["memory"]}, f"standard {key}: {got} is today's")
    for key, dep in table.items():
        cpu, mem = sm[key]["cpu"], sm[key]["memory"]
        check(CONSUMPTION.get(cpu) == mem, f"small {key}: {cpu} vCPU / {mem} is a Consumption size")
        check({"cpu": cpu, "memory": mem} == {k: SMALL[dep][k] for k in ("cpu", "memory")}, f"small {key} as priced")
    check(st["postgres"] == {k: STANDARD["postgres"][k] for k in ("skuName", "skuTier", "storageSizeGB")},
          "standard PostgreSQL is today's D4ds_v5, 128 GB")
    check(sm["postgres"]["skuTier"] == "Burstable", "small PostgreSQL is Burstable")
    # The relay answers Teams within its timeout only when warm; api and the worker always run.
    for key in ("relay", "api"):
        check(sm[key]["min"] >= 1, f"small {key} keeps a warm replica")
    # B1ms has 35 user connections: small's catalog connections, at every app's maximum replicas, stay at 30 or
    # below. Each pooled app's pool (OMNIFILE_PG_POOL_MAX) times its replicas, the verifier's 2, and the short ones:
    # api's start-up read per replica and web's Settings storage run.
    pooled = {k: sm[k]["max"] * sm[k].get("pool", POOL_PER_REPLICA) for k in ("api", "web", "enrol", "worker")}
    check(all("pool" in sm[k] for k in pooled), "small sets a pool for api, web, enrol and the worker")
    check(all("pool" not in st[k] for k in pooled), "standard sets no pool (api, web, enrol 10 each; the worker's own)")
    total = sum(pooled.values()) + VERIFIER_CONNECTIONS + sm["api"]["max"] + 1
    check(total <= SMALL_BUDGET,
          f"small's catalog connections at most {total} ({pooled}, verifier {VERIFIER_CONNECTIONS}, "
          f"{sm['api']['max'] + 1} short): at most {SMALL_BUDGET} of B1ms's {B1MS_USER_CONNECTIONS}")
    check(st["logDailyCapGb"] == "-1" and st["logRetentionDays"] == 90 and st["alertFrequency"] == "PT5M",
          "standard logs: no cap, 90 days, alerts every 5 minutes (today's)")
    # The workspace always gets its cap, -1 (none) included, so small's 0.5 GB can't stay on after a move to
    # standard (review of #59).
    caps = [
        r["properties"].get("workspaceCapping")
        for d in _deployments(template, "monitoring")
        for r in _resources(d["properties"]["template"])
        if r["type"] == "Microsoft.OperationalInsights/workspaces"
    ]
    check(caps == [{"dailyQuotaGb": "[json(parameters('dailyQuotaGb'))]"}],
          f"the workspace is always sent its daily cap, -1 for none ({caps})")


def _resources(template: dict) -> list[dict]:
    res = template["resources"]
    return list(res.values()) if isinstance(res, dict) else res


def _deployments(template: dict, name: str) -> list[dict]:
    return [r for r in _resources(template)
            if r.get("type") == "Microsoft.Resources/deployments" and r.get("name") == name]  # fmt: skip


#: The sized parameters the customer guide passes (its test values; null for a client, so the size decides).
GUIDE_SIZED = ("postgresSkuTier", "postgresSkuName", "postgresStorageSizeGB", "apiMinReplicas", "apiMaxReplicas",
               "workerMaxReplicas")


def guide(repo: Path) -> None:
    """docs/guide/customer.bicepparam: the size is the client's OMNI_DEPLOYMENT_SIZE (standard when empty), and
    the sized parameters are null unless a test run sets them, so a client's size is never overridden.
    docs/guide/check.sh builds the file for a small site too and checks the same on the result."""
    path = repo / "docs" / "guide" / "customer.bicepparam"
    if not path.exists():
        print("skip the guide's parameters (no docs/guide)")
        return
    text = path.read_text()
    check(re.search(r"^var sizeChoice = readEnvironmentVariable\('OMNI_DEPLOYMENT_SIZE', ''\)$", text, re.M)
          is not None and re.search(r"^param deploymentSize = empty\(sizeChoice\) \? 'standard' : sizeChoice$",
                                    text, re.M) is not None,
          "the guide: the size is OMNI_DEPLOYMENT_SIZE, standard when empty")
    for name in GUIDE_SIZED:
        check(re.search(rf"^param {name} = empty\(\w+\) \? null : ", text, re.M) is not None,
              f"the guide leaves {name} to the size unless a test run sets it")


def evaluate(template: dict, repo: Path) -> None:
    sys.path.insert(0, str(repo / "service" / "tests"))
    import test_deploy_env as t  # the service's ARM evaluator

    class Arm(t.Arm):
        def parameter(self, name):  # a nullable parameter with no value is null, as in ARM
            spec = self.t["parameters"].get(name, {})
            if name not in self.params and "defaultValue" not in spec and spec.get("nullable"):
                return None
            return super().parameter(name)

    def given(params: dict) -> dict:
        a = Arm(template, {**t.REQUIRED_PARAMS, **params})
        return {dep: {n: a.module_param(dep, n) for n in names} for dep, names in STANDARD.items()}

    def refused(params: dict) -> str:
        try:
            given(params)
        except t.DeploymentFails as e:
            return str(e)
        return ""

    check(given({}) == STANDARD, "evaluated: the default (standard) deploys today's sizing")
    check(given({"deploymentSize": "standard"}) == STANDARD, "evaluated: standard deploys today's sizing")
    check(given({"deploymentSize": "small"}) == SMALL, "evaluated: small deploys what wp16.md prices")
    o = given({"deploymentSize": "small", "postgresSkuName": "Standard_B2s", "apiMaxReplicas": 1,
               "logDailyCapGb": "-1", "logRetentionDays": 60})
    check(o["postgres"]["skuName"] == "Standard_B2s" and o["app-api"]["maxReplicas"] == 1
          and o["monitoring"] == {"retentionInDays": 60, "dailyQuotaGb": "-1"},
          "evaluated: a parameter that is set overrides its size")
    check(given({"apiMaxReplicas": 4})["app-web"]["maxReplicas"] == 4, "evaluated: standard web's max follows api's")
    check("Consumption profile only" in refused({"deploymentSize": "small", "verifierDedicatedProfileType": "D4"}),
          "evaluated: small refuses a dedicated verifier profile")
    check("no high availability" in refused({"deploymentSize": "small", "postgresHighAvailability": "SameZone"}),
          "evaluated: small refuses PostgreSQL high availability")
    check("no high availability" in refused({"postgresSkuTier": "Burstable", "postgresSkuName": "Standard_B2s",
                                             "postgresHighAvailability": "ZoneRedundant"}),
          "evaluated: any Burstable server refuses high availability")
    check(given({"postgresHighAvailability": "ZoneRedundant"})["postgres"]["highAvailabilityMode"] == "ZoneRedundant",
          "evaluated: standard keeps high availability when asked")

    def pools(params: dict) -> dict:
        a = Arm(template, {**t.REQUIRED_PARAMS, **params})
        return {dep: a.env(dep).get("OMNIFILE_PG_POOL_MAX") for dep in SMALL_POOLS}

    # The connection budget main.bicep checks itself on a B1ms (review of #59).
    def connections(params: dict) -> int:
        return Arm(template, {**t.REQUIRED_PARAMS, **params}).variable("catalogConnections")

    small_sum = connections({"deploymentSize": "small"})
    check(small_sum == 25, f"evaluated: small's apps can open {small_sum} catalog connections (want 25)")
    check(connections({"deploymentSize": "small", "verifierTrigger": "Event"}) == 25,
          "evaluated: small's Event-triggered verifier runs one execution at a time")
    check(refused({"deploymentSize": "small"}) == "" and refused({}) == "",
          "evaluated: small on its B1ms, and standard on its D4ds_v5, pass the connection check")
    for name, params in (
        ("standard on a B1ms, with no pool set", {"postgresSkuTier": "Burstable",
                                                  "postgresSkuName": "Standard_B1ms"}),
        ("small with api's replicas raised", {"deploymentSize": "small", "apiMaxReplicas": 4}),
        ("small with the worker's replicas raised", {"deploymentSize": "small", "workerMaxReplicas": 2}),
        ("standard's Event verifier on a B1ms", {"postgresSkuTier": "Burstable", "postgresSkuName": "Standard_B1ms",
                                                 "apiMaxReplicas": 1, "workerMaxReplicas": 1,
                                                 "verifierTrigger": "Event"}),
    ):
        check("35 user connections" in refused(params), f"evaluated: refused on a B1ms: {name}")
    check(refused({"deploymentSize": "small", "apiMaxReplicas": 4, "postgresSkuName": "Standard_B2s"}) == "",
          "evaluated: a bigger server takes the raised replicas")

    check(pools({}) == dict.fromkeys(SMALL_POOLS), "evaluated: standard sets no OMNIFILE_PG_POOL_MAX")
    check(pools({"deploymentSize": "small"}) == SMALL_POOLS,
          f"evaluated: small gives each app its OMNIFILE_PG_POOL_MAX {SMALL_POOLS}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("main_json")
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args()
    template = json.loads(Path(args.main_json).read_text())
    repo = Path(__file__).resolve().parents[2]
    static(template)
    guide(repo)
    if args.evaluate:
        evaluate(template, repo)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
