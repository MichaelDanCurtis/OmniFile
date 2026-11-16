"""What `deploy/azure/main.bicep` deploys is what the service runs.

Two kinds of check:

- From the Bicep source, so they run without the Bicep CLI: a setting the template passes that no
  code reads (or one the code needs that the template never passes) fails here, not on Azure.
- From the compiled ARM template (`bicep build`, skipped without the CLI), evaluated by `Arm`, a
  small evaluator of the ARM template functions the template uses: each app's command and
  settings as deployed for given parameters. `entrypoint_errors` is the entrypoint check; the
  mutation tests feed it a wrong one, so a wrong entrypoint can't pass. The T5 tests
  (`tests/api/test_t5.py`) use the same evaluator to build api's client-certificate source
  exactly as a deployment would.
"""

from __future__ import annotations

import copy
import functools
import importlib.util
import ipaddress
import json
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omnifile_service.catalog.connect import AZURE_ENV

REPO = Path(__file__).resolve().parents[2]
BICEP = REPO / "deploy" / "azure" / "main.bicep"
READERS = [REPO / "service" / "src", REPO / "relay" / "src"]
#: The apps that use the catalog, by module name; the verifier job runs the worker's code.
CATALOG_APPS = ("api", "enrol", "web", "worker", "verifier")

#: Each app's entrypoint: (deployment name in the compiled template, the command it must run).
#: `python -m <module>` needs `<module>/__main__.py` in the service; the relay's command is the
#: console script relay/pyproject.toml declares.
ENTRYPOINTS: dict[str, tuple[str, list[str]]] = {
    "api": ("app-api", ["python", "-m", "omnifile_service.api"]),
    "enrol": ("app-enrol", ["python", "-m", "omnifile_service.enrol"]),
    "web": ("app-web", ["python", "-m", "omnifile_service.web"]),
    "worker": ("app-worker", ["python", "-m", "omnifile_service.worker"]),
    # The verifier job: one pass over the worker's verify queue, then exit (worker/verifier.py).
    "verifier": ("job-verifier", ["python", "-m", "omnifile_service.worker.verifier"]),
    "relay": ("app-relay", ["omnifile-relay"]),
}

#: Parameters a deployment must give (the rest have defaults).
REQUIRED_PARAMS: dict[str, Any] = {
    "postgresEntraAdminObjectId": "00000000-0000-0000-0000-000000000001",
    "postgresEntraAdminName": "OmniFile-DB-Admins",
    "apiImage": "reg.test/omnifile-service@sha256:" + "a" * 64,
    "enrolImage": "reg.test/omnifile-service@sha256:" + "a" * 64,
    "workerImage": "reg.test/omnifile-service@sha256:" + "a" * 64,
    "verifierImage": "reg.test/omnifile-service@sha256:" + "a" * 64,
    "webImage": "reg.test/omnifile-web@sha256:" + "b" * 64,
    "relayImage": "reg.test/omnifile-relay@sha256:" + "c" * 64,
    "relayAllowedCidrs": ["192.0.2.0/24"],
    "enrolAllowedCidrs": ["10.20.0.0/16"],
    "webAllowedCidrs": ["10.30.0.0/16"],
}


# --- the Bicep source -----------------------------------------------------------


def _source() -> str:
    if not BICEP.exists():
        pytest.skip("deploy/azure is not in this checkout")
    return BICEP.read_text()


def _module(text: str, name: str) -> str:
    m = re.search(rf"^module {name} '[^']+' = .*?^}}$", text, re.M | re.S)
    assert m, f"module {name} not found in main.bicep"
    return m.group(0)


def _var(text: str, name: str) -> str:
    m = re.search(rf"^var {name} = \[.*?^\]$", text, re.M | re.S)
    assert m, f"var {name} not found in main.bicep"
    return m.group(0)


@functools.cache
def _python_sources() -> str:
    return "\n".join(p.read_text() for root in READERS for p in root.rglob("*.py"))


def _read_somewhere(name: str) -> bool:
    text = _python_sources()
    return f'"{name}"' in text or f"'{name}'" in text


def test_every_setting_the_template_passes_is_read_by_the_service_or_the_relay() -> None:
    names = sorted(set(re.findall(r"name: '((?:OMNIFILE|AZURE)_[A-Z0-9_]+)'", _source())))
    assert "OMNIFILE_PG_HOST" in names
    unread = [n for n in names if not _read_somewhere(n)]
    assert unread == [], f"main.bicep sets settings no code reads: {unread}"


def test_the_catalog_settings_are_all_passed() -> None:
    text = _source()
    pg = _var(text, "pgEnv")
    for name in ("OMNIFILE_PG_HOST", "OMNIFILE_PG_DATABASE", "OMNIFILE_PG_SSLMODE"):
        assert f"'{name}'" in pg
    assert "value: 'verify-full'" in pg
    assert set(AZURE_ENV) <= set(re.findall(r"name: '([A-Z0-9_]+)'", text))


@pytest.mark.parametrize("app", CATALOG_APPS)
def test_each_catalog_app_signs_in_as_its_own_identity(app: str) -> None:
    block = _module(_source(), app)
    assert "pgEnv" in block, f"{app} gets no catalog settings"
    ident = "id" + app.capitalize()
    assert re.search(rf"name: 'AZURE_CLIENT_ID'\s+value: {ident}\.properties\.clientId", block), (
        f"{app}: AZURE_CLIENT_ID is not its own identity"
    )
    assert re.search(rf"name: 'OMNIFILE_PG_USER'\s+value: {ident}\.name", block), (
        f"{app}: OMNIFILE_PG_USER is not its own identity's name"
    )
    assert "OMNIFILE_DATABASE_URL" not in block


def test_the_relay_gets_no_catalog() -> None:
    assert "pgEnv" not in _module(_source(), "relay")


@pytest.mark.parametrize("app", sorted(ENTRYPOINTS))
def test_each_module_runs_its_own_command_in_the_source(app: str) -> None:
    """Without the Bicep CLI too: each app's module names its own entry of appCommands."""
    block = _module(_source(), app)
    assert re.search(rf"^\s+command: appCommands\.{app}$", block, re.M), (
        f"{app}: its module does not run appCommands.{app}"
    )


# --- the commands themselves: they exist in the code -----------------------------


def command_errors(command: list[str]) -> list[str]:
    """Why `command` can't start a service app ([] if it can)."""
    if command[:2] == ["python", "-m"] and len(command) == 3:
        module = command[2]
        if not module.startswith("omnifile_service."):
            return [f"{module} is not a service module"]
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            spec = None
        if spec is None:
            return [f"{module} does not exist"]
        if spec.submodule_search_locations is not None:  # a package: python -m runs __main__
            ok = importlib.util.find_spec(f"{module}.__main__") is not None
        else:  # a module: it must run something when run as __main__
            ok = 'if __name__ == "__main__":' in Path(spec.origin or "").read_text()
        return [] if ok else [f"{module} has nothing to run as __main__"]
    if len(command) == 1:
        scripts = tomllib.loads((REPO / "relay" / "pyproject.toml").read_text())["project"][
            "scripts"
        ]
        return [] if command[0] in scripts else [f"{command[0]} is not a relay console script"]
    return [f"unrecognised command {command}"]


@pytest.mark.parametrize("app", sorted(ENTRYPOINTS))
def test_each_expected_command_exists(app: str) -> None:
    assert command_errors(ENTRYPOINTS[app][1]) == []


def test_a_command_that_does_not_exist_is_caught() -> None:
    assert command_errors(["python", "-m", "omnifile_service.nonesuch"])
    # A module that exists but runs nothing (the verifier job ran the endless worker before).
    assert command_errors(["python", "-m", "omnifile_service.worker.verify"])
    assert command_errors(["omnifile-nonesuch"])


# --- an evaluator for the compiled ARM template ---------------------------------


class Ref:
    """A value only Azure knows at deployment time (`reference(...)`, `tenant()`, ...)."""

    def __init__(self, path: str) -> None:
        self.path = path

    def get(self, key: Any) -> Ref:
        return Ref(f"{self.path}.{key}")

    def __str__(self) -> str:
        return f"<{self.path}>"

    __repr__ = __str__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ref) and other.path == self.path

    def __hash__(self) -> int:
        return hash(self.path)


@dataclass
class Lambda:
    names: list[str]
    body: Any
    scope: dict[str, Any]


_TOKEN = re.compile(r"\s*(?:('(?:[^']|'')*')|(-?\d+)|([A-Za-z_][A-Za-z0-9_]*)|([(),.\[\]]))")


def _tokens(text: str) -> list[tuple[str, Any]]:
    out, i = [], 0
    while i < len(text):
        if text[i:].strip() == "":
            break
        m = _TOKEN.match(text, i)
        if not m:
            raise ValueError(f"cannot tokenize {text[i:]!r}")
        s, n, ident, p = m.groups()
        if s is not None:
            out.append(("lit", s[1:-1].replace("''", "'")))
        elif n is not None:
            out.append(("lit", int(n)))
        elif ident is not None:
            out.append(("id", ident))
        else:
            out.append(("p", p))
        i = m.end()
    return out


def _parse(text: str) -> Any:
    toks = _tokens(text)
    pos = 0

    def peek(kind: str, value: Any = None) -> bool:
        return pos < len(toks) and toks[pos][0] == kind and (value is None or toks[pos][1] == value)

    def take(kind: str, value: Any = None) -> Any:
        nonlocal pos
        if not peek(kind, value):
            raise ValueError(f"expected {value or kind} at {pos} in {text!r}")
        pos += 1
        return toks[pos - 1][1]

    def expr() -> Any:
        nonlocal pos
        if peek("lit"):
            node: Any = ("lit", take("lit"))
        else:
            name = take("id")
            take("p", "(")
            args = []
            while not peek("p", ")"):
                args.append(expr())
                if peek("p", ","):
                    take("p", ",")
            take("p", ")")
            node = ("call", name, args)
        while True:
            if peek("p", "."):
                take("p", ".")
                node = ("prop", node, take("id"))
            elif peek("p", "["):
                take("p", "[")
                index = expr()
                take("p", "]")
                node = ("index", node, index)
            else:
                return node

    tree = expr()
    if pos != len(toks):
        raise ValueError(f"trailing tokens in {text!r}")
    return tree


def _is_expression(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("[")
        and value.endswith("]")
        and not value.startswith("[[")
    )


def _get(obj: Any, key: Any) -> Any:
    if isinstance(obj, Ref):
        return obj.get(key)
    if isinstance(obj, dict):
        if key not in obj:
            raise KeyError(key)
        return obj[key]
    if isinstance(obj, list):
        return obj[key]
    raise TypeError(f"cannot read {key!r} of {obj!r}")


def _str(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


class DeploymentFails(Exception):
    """ARM's fail(): the deployment stops with this message."""


class Arm:
    """Evaluates a compiled ARM template's expressions for given parameter values.

    Only what main.bicep uses; an unknown function is an error, never a guess."""

    def __init__(self, template: dict, params: Mapping[str, Any]) -> None:
        self.t = template
        self.params = dict(params)
        self._vars: dict[str, Any] = {}

    # values and expressions ---------------------------------------------------

    def value(self, v: Any, scope: dict[str, Any] | None = None) -> Any:
        if _is_expression(v):
            return self.eval(_parse(v[1:-1]), scope or {})
        if isinstance(v, str) and v.startswith("[["):
            return v[1:]
        if isinstance(v, dict):
            return {k: self.value(x, scope) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x, scope) for x in v]
        return v

    def parameter(self, name: str) -> Any:
        if name in self.params:
            return self.params[name]
        spec = self.t["parameters"][name]
        if "defaultValue" not in spec:
            raise KeyError(f"parameter {name} has no value and no default")
        return self.value(spec["defaultValue"])

    def variable(self, name: str) -> Any:
        if name not in self._vars:
            if name in self.t["variables"]:
                self._vars[name] = self.value(self.t["variables"][name])
            else:
                loops = [c for c in self.t["variables"].get("copy", []) if c["name"] == name]
                if not loops:
                    raise KeyError(f"variable {name}")
                loop = loops[0]
                count = self.value(loop["count"])
                self._vars[name] = [
                    self.value(loop["input"], {"__copy__": {name: i}}) for i in range(count)
                ]
        return self._vars[name]

    def eval(self, node: Any, scope: dict[str, Any]) -> Any:
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "prop":
            return _get(self.eval(node[1], scope), node[2])
        if kind == "index":
            return _get(self.eval(node[1], scope), self.eval(node[2], scope))
        name, args = node[1], node[2]
        lazy = getattr(self, f"_lazy_{name}", None)
        if lazy is not None:
            return lazy(args, scope)
        fn = getattr(self, f"_fn_{name}", None)
        if fn is None:
            raise NotImplementedError(f"ARM function {name}")
        return fn(*[self.eval(a, scope) for a in args])

    # functions that decide what to evaluate -------------------------------------

    def _lazy_if(self, args, scope):
        cond = self.eval(args[0], scope)
        if not isinstance(cond, bool):
            raise TypeError(f"if() on {cond!r}")
        return self.eval(args[1] if cond else args[2], scope)

    def _lazy_and(self, args, scope):
        return all(self._bool(self.eval(a, scope)) for a in args)

    def _lazy_or(self, args, scope):
        return any(self._bool(self.eval(a, scope)) for a in args)

    def _lazy_lambda(self, args, scope):
        names = [self.eval(a, scope) for a in args[:-1]]
        return Lambda(names, args[-1], scope)

    def _lazy_lambdaVariables(self, args, scope):
        return scope[self.eval(args[0], scope)]

    def _lazy_copyIndex(self, args, scope):
        loops = scope.get("__copy__", {})
        name = self.eval(args[0], scope) if args and isinstance(args[0][1], str) else None
        offset = self.eval(args[-1], scope) if args and isinstance(args[-1][1], int) else 0
        index = loops[name] if name else next(iter(loops.values()))
        return index + offset

    @staticmethod
    def _bool(v: Any) -> bool:
        if not isinstance(v, bool):
            raise TypeError(f"not a boolean: {v!r}")
        return v

    def _call(self, fn: Lambda, *values: Any) -> Any:
        return self.eval(fn.body, {**fn.scope, **dict(zip(fn.names, values, strict=True))})

    # plain functions -------------------------------------------------------------

    def _fn_parameters(self, name):
        return self.parameter(name)

    def _fn_variables(self, name):
        return self.variable(name)

    def _fn_reference(self, what, *_):
        return Ref(f"reference({what})")

    def _fn_resourceId(self, *args):
        return f"<resourceId({','.join(map(_str, args))})>"

    def _fn_subscriptionResourceId(self, *args):
        return f"<subscriptionResourceId({','.join(map(_str, args))})>"

    def _fn_extensionResourceId(self, *args):
        return f"<extensionResourceId({','.join(map(_str, args))})>"

    def _fn_resourceGroup(self):
        return Ref("resourceGroup()")

    def _fn_subscription(self):
        return Ref("subscription()")

    def _fn_tenant(self):
        return Ref("tenant()")

    def _fn_environment(self):
        return Ref("environment()")

    def _fn_deployment(self):
        return Ref("deployment()")

    def _fn_uniqueString(self, *args):
        return "uniq0123456789"

    def _fn_guid(self, *args):
        return "00000000-0000-0000-0000-000000000000"

    def _fn_null(self):
        return None

    def _fn_true(self):
        return True

    def _fn_false(self):
        return False

    def _fn_createArray(self, *items):
        return list(items)

    def _fn_createObject(self, *kv):
        return {kv[i]: kv[i + 1] for i in range(0, len(kv), 2)}

    def _fn_concat(self, *items):
        if items and all(isinstance(i, list) for i in items):
            return [x for i in items for x in i]
        return "".join(_str(i) for i in items)

    def _fn_union(self, *items):
        if all(isinstance(i, dict) for i in items):
            out: dict = {}
            for i in items:
                out.update(i)
            return out
        out_list: list = []
        for i in items:
            out_list += [x for x in i if x not in out_list]
        return out_list

    def _fn_format(self, fmt, *args):
        return re.sub(r"\{(\d+)\}", lambda m: _str(args[int(m[1])]), fmt)

    def _fn_string(self, v):
        return _str(v)

    def _fn_int(self, v):
        return int(v)

    def _fn_equals(self, a, b):
        return a == b

    def _fn_not(self, v):
        return not self._bool(v)

    def _fn_empty(self, v):
        return v is None or v == "" or v == [] or v == {}

    def _fn_length(self, v):
        return len(v)

    def _fn_min(self, *items):
        return min(items[0] if len(items) == 1 and isinstance(items[0], list) else items)

    # Integers (main.bicep's connection budget; deploy/azure/test-sizes.py).
    def _fn_add(self, a, b):
        return self._int(a) + self._int(b)

    def _fn_mul(self, a, b):
        return self._int(a) * self._int(b)

    def _fn_greater(self, a, b):
        return self._int(a) > self._int(b)

    @staticmethod
    def _int(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError(f"not an integer: {v!r}")
        return v

    def _fn_parseCidr(self, cidr):  # noqa: N802 - ARM's name
        net = ipaddress.ip_network(cidr, strict=False)
        hosts = (net[1], net[-2]) if net.version == 4 and net.prefixlen < 31 else (net[0], net[-1])
        return {
            "network": str(net.network_address),
            "netmask": str(net.netmask),
            "broadcast": str(net.broadcast_address),
            "firstUsable": str(hosts[0]),
            "lastUsable": str(hosts[1]),
            "cidr": net.prefixlen,
        }

    def _fn_fail(self, message):
        raise DeploymentFails(message)

    def _fn_join(self, items, sep):
        return sep.join(_str(i) for i in items)

    def _fn_split(self, text, sep):
        return text.split(sep) if isinstance(text, str) else Ref(f"split({text})")

    def _fn_take(self, v, n):
        return v[:n] if isinstance(v, (str, list)) else Ref(f"take({v})")

    def _fn_last(self, v):
        return v[-1] if isinstance(v, (str, list)) else Ref(f"last({v})")

    def _fn_first(self, v):
        return v[0] if isinstance(v, (str, list)) else Ref(f"first({v})")

    def _fn_toLower(self, v):
        return v.lower() if isinstance(v, str) else v

    def _fn_contains(self, container, item):
        return item in container

    def _fn_coalesce(self, *items):
        return next((i for i in items if i is not None), None)

    def _fn_tryGet(self, obj, *keys):
        for k in keys:
            if obj is None:
                return None
            if isinstance(obj, Ref):
                obj = obj.get(k)
            elif isinstance(obj, dict):
                obj = obj.get(k)
            elif isinstance(obj, list):
                obj = obj[k] if 0 <= k < len(obj) else None
            else:
                return None
        return obj

    def _fn_range(self, start, count):
        return list(range(start, start + count))

    def _fn_map(self, items, fn):
        return [self._call(fn, i) for i in items]

    def _fn_filter(self, items, fn):
        return [i for i in items if self._call(fn, i)]

    def _fn_toObject(self, items, key_fn, value_fn=None):
        return {self._call(key_fn, i): (self._call(value_fn, i) if value_fn else i) for i in items}

    # the deployment's apps ------------------------------------------------------------

    def deployment(self, name: str) -> dict | None:
        """The module deployment called `name`, or None if its condition is false."""
        resources = self.t["resources"]
        for r in resources.values() if isinstance(resources, dict) else resources:
            if r.get("type") == "Microsoft.Resources/deployments" and r.get("name") == name:
                if "condition" in r and not self.value(r["condition"]):
                    return None
                return r
        raise KeyError(f"no deployment {name}")

    def module_param(self, name: str, param: str) -> Any:
        dep = self.deployment(name)
        assert dep is not None, f"{name} is not deployed with these parameters"
        params = dep["properties"]["parameters"]
        if param not in params:
            inner = dep["properties"]["template"]["parameters"][param]
            return inner.get("defaultValue")
        # A conditional value compiles to an expression yielding the whole {value: ...}.
        entry = self.value(params[param])
        return self.value(entry["value"])

    def env(self, name: str) -> dict[str, Any]:
        """The app's environment: name -> value (`secret:<ref>` for a secret reference)."""
        out: dict[str, Any] = {}
        for e in self.module_param(name, "env"):
            out[e["name"]] = e["value"] if "value" in e else f"secret:{e['secretRef']}"
        return out


def container_apps(template: dict) -> dict[str, dict]:
    """Every module deployment whose template makes a container app or job, by name."""
    out = {}
    resources = template["resources"]
    for r in resources.values() if isinstance(resources, dict) else resources:
        if r.get("type") != "Microsoft.Resources/deployments":
            continue
        inner = r["properties"].get("template", {}).get("resources", {})
        kinds = {x.get("type") for x in (inner.values() if isinstance(inner, dict) else inner)}
        if kinds & {"Microsoft.App/containerApps", "Microsoft.App/jobs"}:
            out[r["name"]] = r
    return out


def _app_resource(dep: dict) -> dict:
    inner = dep["properties"]["template"]["resources"]
    for x in inner.values() if isinstance(inner, dict) else inner:
        if x.get("type") in ("Microsoft.App/containerApps", "Microsoft.App/jobs"):
            return x
    raise KeyError("no container app in the module")


def entrypoint_errors(template: dict, params: Mapping[str, Any]) -> list[str]:
    """Every way an app's entrypoint is wrong in `template` deployed with `params`:
    an app or job not in ENTRYPOINTS, a container that doesn't run its module's
    `command` parameter, or a command that isn't its app's (or doesn't exist)."""
    arm = Arm(template, params)
    errors = []
    expected = {dep: (app, cmd) for app, (dep, cmd) in ENTRYPOINTS.items()}
    for name, dep in container_apps(template).items():
        if name not in expected:
            errors.append(f"{name}: an app or job with no entrypoint check")
            continue
        app, want = expected[name]
        containers = _app_resource(dep)["properties"]["template"]["containers"]
        if any(c.get("command") != "[parameters('command')]" for c in containers):
            errors.append(f"{app}: the container ignores the module's command parameter")
        if "command" not in dep["properties"]["template"]["parameters"]:
            errors.append(f"{app}: its module takes no command, so the image's own CMD runs")
            continue
        if arm.deployment(name) is None:
            continue
        got = arm.module_param(name, "command")
        if got != want:
            errors.append(f"{app} runs {got}, not {want}")
        errors += [f"{app}: {e}" for e in command_errors(got)]
    missing = set(expected) - set(container_apps(template))
    errors += [f"{expected[m][0]}: not in the template" for m in sorted(missing)]
    return errors


# --- the compiled template ------------------------------------------------------------


@functools.cache
def _compiled() -> dict | None:
    bicep = shutil.which("bicep")
    if bicep is None or not BICEP.exists():
        return None
    out = subprocess.run(
        [bicep, "build", str(BICEP), "--stdout"], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


@pytest.fixture
def compiled() -> dict:
    """main.bicep compiled by the Bicep CLI (brew install azure/bicep/bicep)."""
    t = _compiled()
    if t is None:
        pytest.skip("needs the Bicep CLI and deploy/azure")
    return copy.deepcopy(t)


def deployed(template: dict, **overrides: Any) -> Arm:
    return Arm(template, {**REQUIRED_PARAMS, **overrides})


def test_every_app_runs_its_own_entrypoint(compiled: dict) -> None:
    assert entrypoint_errors(compiled, REQUIRED_PARAMS) == []


def test_every_app_and_job_has_an_entrypoint_check(compiled: dict) -> None:
    assert set(container_apps(compiled)) == {dep for dep, _ in ENTRYPOINTS.values()}


@pytest.mark.parametrize("wrong", ["api", "worker"])
def test_a_wrong_entrypoint_fails(compiled: dict, wrong: str) -> None:
    """The bug this check was written for: enrol, the worker and the verifier all ran api."""
    for dep in ("app-enrol", "job-verifier"):
        compiled["resources"][_symbol(compiled, dep)]["properties"]["parameters"]["command"] = {
            "value": f"[variables('appCommands').{wrong}]"
        }
    errors = entrypoint_errors(compiled, REQUIRED_PARAMS)
    assert any(e.startswith("enrol runs") for e in errors), errors
    # The verifier running the endless worker (what origin/main deployed) is wrong too.
    assert any(e.startswith("verifier runs") for e in errors), errors


def test_a_container_without_the_command_fails(compiled: dict) -> None:
    """The image's own CMD would run instead (Containerfile.api's is api)."""
    dep = compiled["resources"][_symbol(compiled, "app-worker")]
    _app_resource(dep)["properties"]["template"]["containers"][0].pop("command")
    assert "worker: the container ignores the module's command parameter" in entrypoint_errors(
        compiled, REQUIRED_PARAMS
    )


def test_a_new_app_without_a_check_fails(compiled: dict) -> None:
    sym = _symbol(compiled, "app-worker")
    clone = copy.deepcopy(compiled["resources"][sym])
    clone["name"] = "app-extra"
    compiled["resources"]["extra"] = clone
    assert "app-extra: an app or job with no entrypoint check" in entrypoint_errors(
        compiled, REQUIRED_PARAMS
    )


def _symbol(template: dict, deployment: str) -> str:
    for sym, r in template["resources"].items():
        if r.get("name") == deployment:
            return sym
    raise KeyError(deployment)


def test_the_evaluator_reads_the_template_as_deployed(compiled: dict) -> None:
    """A check of `Arm` itself: values the template states plainly."""
    arm = deployed(compiled)
    env = arm.env("app-api")
    assert env["OMNIFILE_PG_SSLMODE"] == "verify-full"
    assert env["OMNIFILE_PG_USER"] == "id-omni-api"
    assert str(env["AZURE_CLIENT_ID"]) == "<reference(idApi).clientId>"
    assert arm.module_param("app-api", "image") == REQUIRED_PARAMS["apiImage"]
    assert deployed(compiled, deployEnrol=False).deployment("app-enrol") is None


# --- where edges are told to go ---------------------------------------------------------


def test_enrol_tells_edges_the_api_url_api_listens_on(compiled: dict) -> None:
    """Before, enrol had no OMNIFILE_API_URL, so an enrolled edge sent every call to enrol."""
    arm = deployed(compiled)
    enrol, api = arm.env("app-enrol"), arm.env("app-api")
    assert enrol["OMNIFILE_API_URL"] == api["OMNIFILE_API_URL"]
    host = "omni-api.<reference(caeInternal).outputs.defaultDomain.value>"
    assert enrol["OMNIFILE_API_URL"] == f"https://{host}:8443"
    assert api["PORT"] == "8443"
    assert arm.module_param("app-api", "transport") == "tcp"
    assert arm.module_param("app-api", "exposedPort") == 8443
    # api's certificate is for the name edges call, and enrol tells them to pin the edge CA.
    assert enrol["OMNIFILE_ENROL_API_SERVER_NAMES"] == host
    assert api["OMNIFILE_API_SERVER_CERT_URL"].endswith("/internal/v0/sign")


def test_the_relay_calls_api_on_its_port(compiled: dict) -> None:
    arm = deployed(compiled)
    url = arm.env("app-relay")["OMNIFILE_RELAY_SERVICE_BASE_URL"]
    # The module's outputs are Azure's, so the evaluator can't resolve the port, only its use.
    assert "reference(api).outputs.fqdn.value" in str(url)


def test_the_web_app_gets_its_sign_in_settings(compiled: dict) -> None:
    env = deployed(compiled, webClientId="11111111-2222-3333-4444-555555555555").env("app-web")
    assert env["OMNIFILE_WEB_CLIENT_ID"] == "11111111-2222-3333-4444-555555555555"
    assert env["OMNIFILE_WEB_ORIGIN"].startswith("https://omni-web.")
    assert str(env["OMNIFILE_WEB_TENANT_ID"]) == "<tenant().tenantId>"


def test_the_worker_is_the_bot(compiled: dict) -> None:
    arm = deployed(compiled, teamsAppCatalogId="app-catalog-id")
    worker, relay = arm.env("app-worker"), arm.env("app-relay")
    assert worker["OMNIFILE_TEAMS_BOT_APP_ID"] == relay["OMNIFILE_RELAY_BOT_APP_ID"]
    assert str(worker["OMNIFILE_TEAMS_BOT_APP_ID"]) == "<reference(idWorker).clientId>"
    assert worker["OMNIFILE_TEAMS_APP_ID"] == "app-catalog-id"
    other = deployed(compiled, deployTeamsBot=False, relayBotAppId="bot-app")
    assert other.env("app-worker")["OMNIFILE_TEAMS_BOT_APP_ID"] == "bot-app"


def _inner_variable(arm: Arm, deployment: str, name: str, params: tuple[str, ...]) -> Any:
    """A variable of a module's own template, for the parameters the deployment gives it."""
    inner = arm.deployment(deployment)["properties"]["template"]
    return Arm(inner, {p: arm.module_param(deployment, p) for p in params}).variable(name)


@pytest.mark.parametrize("app", ["app-web", "app-enrol", "app-relay"])
def test_a_waiting_app_is_not_ready_but_not_restarted(compiled: dict, app: str) -> None:
    """Before phase 2, web, enrol and the relay answer 503 on /healthz (enrol/waiting.py,
    relay main.build_or_wait). Readiness is that path, so they take no traffic; liveness is
    the port, so they aren't restarted in a loop."""
    arm = deployed(compiled, healthProbePath="/healthz")
    probes = _inner_variable(
        arm, app, "probes", ("healthProbePath", "targetPort", "transport", "ingress")
    )
    by_type = {p["type"]: p for p in probes}
    port = arm.module_param(app, "targetPort")
    assert by_type["Liveness"] == {"type": "Liveness", "tcpSocket": {"port": port}}
    assert by_type["Readiness"]["httpGet"] == {"path": "/healthz", "port": port}


def test_api_gets_time_to_fetch_its_certificate(compiled: dict) -> None:
    """Review of #34: api binds 8443 only after enrol signs its certificate, and the TCP
    liveness probe would restart it meanwhile. A startup probe holds the others off."""
    arm = deployed(compiled)
    probes = _inner_variable(
        arm, "app-api", "probes", ("healthProbePath", "targetPort", "transport", "ingress")
    )
    by_type = {p["type"]: p for p in probes}
    startup = by_type["Startup"]
    assert startup["tcpSocket"] == {"port": 8443}
    budget = startup["initialDelaySeconds"] + startup["periodSeconds"] * startup["failureThreshold"]
    assert budget >= 300, "at least 5 minutes before liveness counts"
    assert by_type["Liveness"]["tcpSocket"] == {"port": 8443}


def test_admins_reach_web_and_enrol_and_nothing_else(compiled: dict) -> None:
    """The guide's admin workstation (adminAllowedCidrs) reaches the portal and enrol's
    health check; the relay's list is the Bot Service ranges only."""
    admin = ["10.99.0.0/24"]
    arm = deployed(compiled, adminAllowedCidrs=admin)
    web = arm.module_param("app-web", "ingressAllowedCidrs")
    enrol = arm.module_param("app-enrol", "ingressAllowedCidrs")
    relay = arm.module_param("app-relay", "ingressAllowedCidrs")
    assert web == REQUIRED_PARAMS["webAllowedCidrs"] + admin
    assert enrol == REQUIRED_PARAMS["enrolAllowedCidrs"] + admin + ["10.60.0.0/26"]
    assert relay == REQUIRED_PARAMS["relayAllowedCidrs"]
    plain = deployed(compiled).module_param("app-web", "ingressAllowedCidrs")
    assert plain == REQUIRED_PARAMS["webAllowedCidrs"]


def _nsg_rule(arm: Arm, nsg: str, rule: str) -> dict:
    """One NSG rule of the network module, its expressions evaluated."""
    dep = arm.deployment("network")
    assert dep is not None
    inner = dep["properties"]["template"]
    params = {p: arm.module_param("network", p) for p in dep["properties"]["parameters"]}
    net = Arm(inner, params)
    resources = inner["resources"]
    resources = resources.values() if isinstance(resources, dict) else resources
    (r,) = [
        r
        for r in resources
        if r["type"] == "Microsoft.Network/networkSecurityGroups"
        and net.value(r["name"]).endswith(f"-{nsg}-nsg")
    ]
    (found,) = [x for x in r["properties"]["securityRules"] if x["name"] == rule]
    return net.value(found["properties"])


def test_the_relay_reaches_api_on_its_port_only(compiled: dict) -> None:
    """Review of #34: the relay's egress to the internal subnet allowed every ingress port
    (443, 31443 and api's), so a compromised relay could reach web and enrol on 443. It
    calls api only."""
    arm = deployed(compiled)
    rule = _nsg_rule(arm, "cae-relay", "out-allow-internal-api")
    assert rule["access"] == "Allow" and rule["destinationPortRanges"] == ["8443"]
    assert rule["destinationAddressPrefix"] == "10.60.0.0/26"
    deny = _nsg_rule(arm, "cae-relay", "out-deny-all-private")
    assert deny["access"] == "Deny" and deny["priority"] > rule["priority"]
    edges = _nsg_rule(arm, "cae-internal", "allow-https-from-vnet")
    assert edges["destinationPortRanges"] == ["443", "31443", "8443"], "edges and admins keep 443"
    moved = deployed(compiled, apiTcpPort=9443)
    assert _nsg_rule(moved, "cae-relay", "out-allow-internal-api")["destinationPortRanges"] == [
        "9443"
    ]


@pytest.mark.parametrize("cidr", ["10.60.0.64/27", "10.60.0.70", "10.60.0.80/28",
                                  "10.60.0.0/16", "10.0.0.0/8", "0.0.0.0/0"])  # fmt: skip
@pytest.mark.parametrize("param", ["adminAllowedCidrs", "webAllowedCidrs", "enrolAllowedCidrs"])
def test_a_caller_range_over_the_relay_subnet_fails_the_deployment(compiled, param, cidr) -> None:
    """Review of #34 (T1): the parameter descriptions said never to list a range covering the
    relay subnet, and nothing refused one. Now the deployment fails, naming it."""
    arm = deployed(compiled, **{param: [*REQUIRED_PARAMS.get(param, []), cidr]})
    for app in ("app-web", "app-enrol"):
        with pytest.raises(DeploymentFails, match="caeRelaySubnetPrefix") as failed:
            arm.module_param(app, "ingressAllowedCidrs")
        assert cidr in str(failed.value)


@pytest.mark.parametrize("cidr", ["10.60.0.0/26", "10.60.0.96/27", "10.60.0.63", "10.60.1.0/24",
                                  "2001:db8::/48"])  # fmt: skip
def test_a_caller_range_beside_the_relay_subnet_is_fine(compiled, cidr) -> None:
    arm = deployed(compiled, adminAllowedCidrs=[cidr])
    assert cidr in arm.module_param("app-web", "ingressAllowedCidrs")
    moved = deployed(compiled, adminAllowedCidrs=["10.60.0.64/27"],
                     caeRelaySubnetPrefix="10.61.0.0/27")  # fmt: skip
    assert "10.60.0.64/27" in moved.module_param("app-enrol", "ingressAllowedCidrs")


def test_the_verifier_runs_on_a_schedule_by_default(compiled: dict) -> None:
    """Nothing in the code starts a Manual execution, so the job ran never; now a scheduled
    pass every 15 minutes (worker/verifier.py), within the replica timeout."""
    arm = deployed(compiled)
    assert arm.module_param("job-verifier", "triggerType") == "Schedule"
    assert arm.module_param("job-verifier", "cronExpression") == "*/15 * * * *"
    inner = arm.deployment("job-verifier")["properties"]["template"]
    params = {
        p: arm.module_param("job-verifier", p)
        for p in ("triggerType", "cronExpression", "parallelism", "replicaCompletionCount")
    }
    job = Arm(inner, params)
    resources = inner["resources"]
    resources = resources.values() if isinstance(resources, dict) else resources
    (resource,) = [r for r in resources if r["type"] == "Microsoft.App/jobs"]
    config = job.value(resource["properties"]["configuration"])
    assert config["scheduleTriggerConfig"]["cronExpression"] == "*/15 * * * *"
    assert config["manualTriggerConfig"] is None and config["eventTriggerConfig"] is None
    assert config["replicaTimeout"] > 840  # the pass's own budget ends first


def env_reader(env: Mapping[str, Any]) -> Callable[[str], str | None]:
    """For the T5 tests: an app environment as the process sees it."""
    return lambda name: None if env.get(name) is None else str(env[name])


# --- web's Settings (portal-api.md 5.20) ------------------------------------------------

#: Settings names web reads that the template doesn't set: a file the deployment doesn't
#: mount (the Teams package, built on an admin workstation), and the AWS settings' file
#: form and private flag (the template passes the JSON, and private is the default).
SETTINGS_NOT_DEPLOYED = {
    "OMNIFILE_TEAMS_APP_PACKAGE",
    "OMNIFILE_AWS_ACCOUNTS_FILE",
    "OMNIFILE_AWS_PRIVATE_NETWORK",
    # Passed only when awsConnectTemplateUrl is set (cloud-connect.md 2.2); empty by default.
    "OMNIFILE_AWS_CONNECT_TEMPLATE_URL",
}


def test_web_gets_what_its_settings_pages_read(compiled: dict) -> None:
    from omnifile_service.settings.ports import ENV_NAMES

    mover = json.dumps(
        {
            "subscription_id": "sub",
            "resource_group": "rg",
            "storage_mover": "sm",
            "project": "p",
            "source_endpoints": {},
            "target_endpoints": {},
        }
    )
    arm = deployed(
        compiled,
        awsAccounts=json.dumps({"123456789012": {"region": "eu-west-1"}}),
        awsWebIdentityAudience="api://omnifile-x",
        storageMoverConfig=mover,
        notifySender="omnifile@contoso.test",
        teamsAppCatalogId="catalog-id",
    )
    web, worker = arm.env("app-web"), arm.env("app-worker")
    missing = set(ENV_NAMES) - SETTINGS_NOT_DEPLOYED - set(web)
    assert not missing, f"web's Settings read these, and main.bicep doesn't pass them: {missing}"
    assert web["OMNIFILE_SETTINGS_PROBES"] == "1"
    # The same names and ids the worker acts on, so the pages check what is deployed.
    for name in ("OMNIFILE_AWS_ACCOUNTS", "OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE",
                 "OMNIFILE_STORAGE_MOVER", "OMNIFILE_TENANT_ID", "OMNIFILE_TEAMS_BOT_APP_ID",
                 "OMNIFILE_TEAMS_APP_ID", "OMNIFILE_NOTIFY_SENDER"):  # fmt: skip
        assert str(web[name]) == str(worker[name]), name
    # No secret: not the Teams intent key, nothing from Key Vault but web's own cursor key.
    assert "OMNIFILE_TEAMS_INTENT_SIGNING_KEY" not in web
    secrets = [k for k, v in web.items() if str(v).startswith("secret:")]
    assert secrets in ([], ["OMNIFILE_WEB_CURSOR_KEY"]), secrets


def _resource_block(text: str, name: str) -> str:
    m = re.search(rf"^resource {name} '[^']+' = .*?^}}$", text, re.M | re.S)
    assert m, f"no resource {name}"
    return m.group(0)


def test_web_reads_the_control_plane_only_and_no_data() -> None:
    """Settings checks need control-plane reads of the storage accounts, their blob service,
    role assignments and Storage Mover (settings/storage.py, mover.py): a custom role with
    exactly those reads, no data action, no key listing, assigned to web on the group."""
    roles = (BICEP.parent / "modules" / "roles.bicep").read_text()
    block = _resource_block(roles, "settingsReader")
    listed = block.split("actions: [", 1)[1].split("dataActions", 1)[0]
    actions = re.findall(r"'(Microsoft\.[A-Za-z]+/[^']+)'", listed)
    assert set(actions) == {
        "Microsoft.Storage/storageAccounts/read",
        "Microsoft.Storage/storageAccounts/blobServices/read",
        "Microsoft.Authorization/roleAssignments/read",
        "Microsoft.Authorization/roleDefinitions/read",
        "Microsoft.StorageMover/storageMovers/read",
        "Microsoft.StorageMover/storageMovers/agents/read",
        "Microsoft.StorageMover/storageMovers/projects/read",
        # Connect Azure's discovery: the service's privatelink.blob zone and its VNet links.
        "Microsoft.Network/privateDnsZones/read",
        "Microsoft.Network/privateDnsZones/virtualNetworkLinks/read",
    }
    assert re.search(r"\bdataActions: \[\]", block) and "listkeys" not in block.lower()
    main = _source()
    assign = _resource_block(main, "webSettingsReader")
    assert "roles.outputs.settingsReaderRoleId" in assign
    assert "idWeb.properties.principalId" in assign
    # Never a storage data role for web.
    assert "webPrincipal" not in _var(main, "serviceBlobAssignments")
