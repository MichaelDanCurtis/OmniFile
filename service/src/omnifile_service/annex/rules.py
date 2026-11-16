"""Annex rules (annex.md sections 4.1-4.4): the templates, and the checks a
rule set passes before anyone can approve it.

A rule set's body is ``{"rules": [{id, trigger, conditions, actions}]}``.
The edge evaluates it (``edge/…/annex/rules.rs``), one implementation for
the dry-run preview and for real; the service only checks it, here, and
never evaluates it. Both refuse what they don't know (fail closed).

- **Triggers** (A2): ``arrived``, ``changed``, ``age`` and
  ``deleted_at_source``; and the return path's ``recall_completed`` (A3):
  where a recalled file is placed. Return rules are the service's to
  evaluate (``annex/returns.py``), for the preview and for real; the edge
  skips them and places a file only on a ticket.
- **Conditions** (all must match; none means "any"): ``path`` (globs),
  ``extension``, ``size_gte``, ``size_lt``, ``age_since_arrival_days_gt``,
  ``modified_age_days_gt``, ``accessed_age_days_gt``, ``owner_sids``,
  ``group_sids``, ``content_type`` (``segy``, ``zip``, ``hdf5``).
- **Actions** (one key each): ``exclude``, ``copy_to``,
  ``remove_from_source``, ``remove_after_days``, and ``keep_copy`` for
  ``deleted_at_source``. ``policy`` and ``tag`` are refused until the
  service applies them (A3): a rule must do what it says.
  - Only ``arrived`` rules remove at once or after N days: a change is
    copied again, and leaving the NAS is an arrival's or an age rule's call.
- **Limits** (the edge enforces them too): 200 rules, unique ids, 100
  values per list, globs of 512 characters, 32 segments and at most 4
  ``**``, never two in a row.
- **Order:** the first matching rule of a trigger wins, and each trigger in
  use ends with a default rule (no conditions), so every file is accounted
  for.

Every rule set starts with a fixed, hidden rule that excludes OmniFile's own
files; the edge applies it whatever the body says.

**Return rules** (``recall_completed``, annex.md sections 4.4 and 4.6):
- Conditions: ``path``, ``extension``, ``size_gte``, ``size_lt``,
  ``owner_sids``, ``group_sids`` (the file's), and ``requester_in`` (the
  requester's SID or groups; return rules only).
- Actions: ``place_at`` (a location), ``on_conflict`` (``same_content``,
  ``restored_name``, ``overwrite_keep_version``; default ``restored_name``),
  ``hold_days``, ``notify`` (``requester``, ``owner``, or ``{group_sid}``),
  ``let_requester_choose`` (other locations the requester may pick), or
  ``exclude`` (recalled to Azure only, placed nowhere).
- Locations: ``{kind: nas_folder, origin: true}`` (where it came from, under
  its own name); ``{kind: nas_folder, path}`` (a folder of this annex);
  ``{kind: nas_folder, annex_id, path}`` (another annex's); ``{kind:
  nas_folder, filer_id, path}`` (a folder of a filer, relative to its
  export); ``{kind: share_folder, share_id, path}``. A folder keeps the
  file's path below the annex (``keep_relative_path``, default true).
"""

from __future__ import annotations

import json
import re
from typing import Any

#: OmniFile's own files on a NAS (pointer files, removals and placements in
#: progress): never copied, never counted.
OWN_FILES = ("*.omnifile.txt", ".omnifile-removing-*", ".omnifile-restoring-*")

TEMPLATES = ("copy", "archive_after", "move_now")
RETURN_TEMPLATES = ("origin", "restore_folder", "share")
OUTBOUND_TRIGGERS = ("arrived", "changed", "age", "deleted_at_source")
RETURN_TRIGGERS = ("recall_completed",)
TRIGGERS = OUTBOUND_TRIGGERS + RETURN_TRIGGERS
CONFLICTS = ("same_content", "restored_name", "overwrite_keep_version")
#: Conditions a return rule may have: what the catalog knows of a removed file.
RETURN_CONDITIONS = ("path", "extension", "size_gte", "size_lt", "owner_sids", "group_sids",
                     "requester_in")  # fmt: skip
MAX_HOLD_DAYS = 365
CONTENT_TYPES = ("segy", "zip", "hdf5")
MAX_RULES = 200
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SID = re.compile(r"^S-1-[0-9]+(-[0-9]+){1,15}$")
_EXT = re.compile(r"^\.?[A-Za-z0-9._-]{1,32}$")
_DAYS = ("age_since_arrival_days_gt", "modified_age_days_gt", "accessed_age_days_gt")
_LISTS = ("path", "extension", "owner_sids", "group_sids", "content_type", "requester_in")
MAX_GLOB_SEGMENTS = 32
MAX_GLOB_STARSTARS = 4


class RulesError(ValueError):
    """A rule set that isn't well formed (`400 invalid_request`)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


# --- templates ------------------------------------------------------------------------


def _tier(dataset_id: int) -> dict[str, Any]:
    return {"kind": "tier", "dataset_id": str(dataset_id)}


def copy_template(dataset_id: int) -> dict[str, Any]:
    """`arrived` -> copy_to · `changed` -> copy_to, into the annex's dataset."""
    tier = _tier(dataset_id)
    return {
        "template": "copy",
        "exclude_always": list(OWN_FILES),
        "rules": [
            {"id": "copy", "trigger": "arrived", "conditions": {},
             "actions": [{"copy_to": tier}]},
            {"id": "recopy", "trigger": "changed", "conditions": {},
             "actions": [{"copy_to": tier}]},
            {"id": "keep", "trigger": "deleted_at_source", "conditions": {},
             "actions": [{"keep_copy": True}]},
        ],
    }  # fmt: skip


def archive_after_template(dataset_id: int, days: int) -> dict[str, Any]:
    """As Copy, plus `age > N days` -> remove_from_source."""
    body = copy_template(dataset_id)
    body["template"] = "archive_after"
    body["archive_after_days"] = days
    body["rules"][2:2] = [
        {"id": "archive", "trigger": "age",
         "conditions": {"age_since_arrival_days_gt": days},
         "actions": [{"remove_from_source": True}]},
        {"id": "stay", "trigger": "age", "conditions": {}, "actions": [{"exclude": True}]},
    ]  # fmt: skip
    return body


def move_now_template(dataset_id: int) -> dict[str, Any]:
    """`arrived` -> copy_to, remove_from_source."""
    body = copy_template(dataset_id)
    body["template"] = "move_now"
    body["rules"][0] = {
        "id": "move",
        "trigger": "arrived",
        "conditions": {},
        "actions": [{"copy_to": _tier(dataset_id)}, {"remove_from_source": True}],
    }
    return body


ORIGIN: dict[str, Any] = {"kind": "nas_folder", "origin": True}


def return_rules(name: str, location: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The return templates (annex.md section 4.4), as `recall_completed` rules."""
    notify = {"notify": ["requester"]}
    if name == "origin":
        return [{"id": "back", "trigger": "recall_completed", "conditions": {},
                 "actions": [{"place_at": dict(ORIGIN)}, {"on_conflict": "restored_name"},
                             {"hold_days": 14}, notify]}]  # fmt: skip
    if location is None:
        raise RulesError("return_location", f"the {name} return template needs a location")
    if name == "restore_folder":
        return [{"id": "restore", "trigger": "recall_completed", "conditions": {},
                 "actions": [{"place_at": location}, {"on_conflict": "restored_name"},
                             notify]}]  # fmt: skip
    if name == "share":
        return [{"id": "to_share", "trigger": "recall_completed", "conditions": {},
                 "actions": [{"place_at": location},
                             {"on_conflict": "overwrite_keep_version"}, notify]}]  # fmt: skip
    raise RulesError("return_template", f"no return template {name!r}")


#: The return template each outbound mode starts with (section 4.4). Move now
#: has none until a restore folder outside the annex is chosen: a drop
#: folder would only send the file out again.
DEFAULT_RETURN = {"copy": "origin", "archive_after": "origin"}


def template(name: str, dataset_id: int, days: int | None = None) -> dict[str, Any]:
    """An outbound template with its default return rules."""
    if name == "copy":
        body = copy_template(dataset_id)
    elif name == "archive_after":
        if days is None:
            raise RulesError("archive_after_days", "Archive after N days needs N")
        body = archive_after_template(dataset_id, days)
    elif name == "move_now":
        body = move_now_template(dataset_id)
    else:
        raise RulesError("mode", f"no template {name!r}")
    back = DEFAULT_RETURN.get(name)
    if back is not None:
        body["rules"] += return_rules(back)
        body["return_template"] = back
    return body


def outbound(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rules if r.get("trigger") not in RETURN_TRIGGERS]


def returns(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rules if r.get("trigger") in RETURN_TRIGGERS]


def return_template_of(rules: list[dict[str, Any]]) -> str | None:
    """Which return template the return rules are, if one."""
    back = returns(rules)
    if not back:
        return None
    if json.dumps(back, sort_keys=True) == json.dumps(return_rules("origin"), sort_keys=True):
        return "origin"
    if len(back) == 1:
        loc = next((a["place_at"] for a in back[0]["actions"] if "place_at" in a), None)
        for name in ("restore_folder", "share"):
            if loc is not None and json.dumps(back, sort_keys=True) == json.dumps(
                return_rules(name, loc), sort_keys=True
            ):
                return name
    return None


def mode_of(body: dict[str, Any]) -> str:
    """The annex mode a rule set runs: its template's, or `custom`."""
    t = body.get("template")
    return t if t in TEMPLATES else "custom"


# --- shape ---------------------------------------------------------------------------------


def _strs(v: Any, field: str, *, pattern: re.Pattern[str] | None = None) -> list[str]:
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not items or len(items) > 100:
        raise RulesError(field, f"{field} must be a string or a list of 1 to 100")
    for x in items:
        if not isinstance(x, str) or not x or len(x) > 512:
            raise RulesError(field, f"{field} must be strings of 1 to 512 characters")
        if pattern is not None and not pattern.match(x):
            raise RulesError(field, f"{field}: {x!r} isn't valid")
    return list(items)


def _number(v: Any, field: str, *, integer: bool = False) -> float | int:
    ok = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok or v < 0 or v > (2**62 if integer else 36500):
        raise RulesError(field, f"{field} must be a number, 0 or more")
    return v


def _glob(p: str, field: str) -> str:
    parts = p.split("/")
    if p.startswith("/") or any(x in ("", ".", "..") for x in parts) or "\\" in p:
        raise RulesError(field, f"{field}: {p!r} must be a relative glob without '.' or '..'")
    if len(parts) > MAX_GLOB_SEGMENTS:
        raise RulesError(field, f"{field}: at most {MAX_GLOB_SEGMENTS} folders deep")
    if parts.count("**") > MAX_GLOB_STARSTARS:
        raise RulesError(field, f"{field}: at most {MAX_GLOB_STARSTARS} `**` in a glob")
    if any(a == b == "**" for a, b in zip(parts, parts[1:], strict=False)):
        raise RulesError(field, f"{field}: `**/**` is `**`; write it once")
    return p


def _conditions(c: Any, where: str, trigger: str = "arrived") -> dict[str, Any]:
    if c is None:
        return {}
    if not isinstance(c, dict):
        raise RulesError(f"{where}.conditions", "conditions must be an object")
    back = trigger in RETURN_TRIGGERS
    out: dict[str, Any] = {}
    for k, v in c.items():
        f = f"{where}.conditions.{k}"
        if back and k not in RETURN_CONDITIONS:
            raise RulesError(f, f"a return rule can't test {k!r}: it isn't known for a"
                                " removed file")  # fmt: skip
        if k == "requester_in":
            if not back:
                raise RulesError(f, "requester_in is for return rules (recall_completed)")
            out[k] = _strs(v, f, pattern=_SID)
        elif k == "path":
            out[k] = [_glob(p, f) for p in _strs(v, f)]
        elif k == "extension":
            out[k] = [e.lstrip(".").lower() for e in _strs(v, f, pattern=_EXT)]
        elif k in ("size_gte", "size_lt"):
            out[k] = _number(v, f, integer=True)
        elif k in _DAYS:
            out[k] = _number(v, f)
        elif k in ("owner_sids", "group_sids"):
            out[k] = _strs(v, f, pattern=_SID)
        elif k == "content_type":
            vals = _strs(v, f)
            for x in vals:
                if x not in CONTENT_TYPES:
                    raise RulesError(f, f"content_type is one of {', '.join(CONTENT_TYPES)}")
            out[k] = vals
        else:
            raise RulesError(f, f"no condition {k!r}")
    return out


_LOCATION_KEYS = {"kind", "origin", "annex_id", "filer_id", "share_id", "path",
                  "keep_relative_path"}  # fmt: skip


def _folder(p: Any, field: str) -> str:
    """A folder path: relative, no '.', '..' or backslash, no OmniFile names."""
    if not isinstance(p, str) or len(p) > 1024:
        raise RulesError(field, "a folder is a relative path of up to 1024 characters")
    p = p.strip("/")
    if not p:
        return ""
    parts = p.split("/")
    if any(x in ("", ".", "..") or x.startswith(".omnifile-") for x in parts) or "\\" in p:
        raise RulesError(field, f"{p!r} must be a plain relative folder")
    return p


def _ref(v: Any, field: str) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).isdigit():
        raise RulesError(field, "an id")
    return str(int(v))


def location(v: Any, field: str) -> dict[str, Any]:
    """A return location in its normal form (section 4.1)."""
    if not isinstance(v, dict) or v.get("kind") not in ("nas_folder", "share_folder"):
        raise RulesError(field, "a location is a nas_folder or a share_folder")
    extra = set(v) - _LOCATION_KEYS
    if extra:
        raise RulesError(field, f"no location field {sorted(extra)[0]!r}")
    keep = v.get("keep_relative_path", True)
    if not isinstance(keep, bool):
        raise RulesError(field, "keep_relative_path is true or false")
    if v["kind"] == "share_folder":
        if "share_id" not in v or {"origin", "annex_id", "filer_id"} & set(v):
            raise RulesError(field, "a share_folder names a share_id (and a path)")
        out = {"kind": "share_folder", "share_id": _ref(v["share_id"], field),
               "path": _folder(v.get("path", ""), field)}  # fmt: skip
    elif v.get("origin") is not None:
        if v["origin"] is not True or set(v) - {"kind", "origin"}:
            raise RulesError(field, "the origin location is {kind: nas_folder, origin: true}")
        return dict(ORIGIN)
    else:
        if "annex_id" in v and "filer_id" in v:
            raise RulesError(field, "a nas_folder names an annex or a filer, not both")
        out = {"kind": "nas_folder", "path": _folder(v.get("path", ""), field)}
        if "annex_id" in v:
            out["annex_id"] = _ref(v["annex_id"], field)
        if "filer_id" in v:
            out["filer_id"] = _ref(v["filer_id"], field)
    if not keep:
        out["keep_relative_path"] = False
    return out


def _notify(v: Any, field: str) -> list[Any]:
    items = v if isinstance(v, list) else [v]
    if not items or len(items) > 10:
        raise RulesError(field, "notify is 1 to 10 of requester, owner or {group_sid}")
    out: list[Any] = []
    for x in items:
        if x in ("requester", "owner"):
            out.append(x)
        elif (
            isinstance(x, dict)
            and set(x) == {"group_sid"}
            and isinstance(x["group_sid"], str)
            and _SID.match(x["group_sid"])
        ):
            out.append({"group_sid": x["group_sid"]})
        else:
            raise RulesError(field, "notify is requester, owner or {group_sid: <SID>}")
    return out


def _return_actions(a: list[Any], f: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    keys: list[str] = []
    for i, item in enumerate(a):
        fi = f"{f}[{i}]"
        if not isinstance(item, dict) or len(item) != 1:
            raise RulesError(fi, "each action is an object with one key")
        ((k, v),) = item.items()
        if k in keys:
            raise RulesError(fi, f"{k} appears twice")
        keys.append(k)
        if k == "exclude":
            if v is not True:
                raise RulesError(fi, "exclude is `true`")
            out.append({k: True})
        elif k == "place_at":
            out.append({k: location(v, fi)})
        elif k == "on_conflict":
            if v not in CONFLICTS:
                raise RulesError(fi, f"on_conflict is one of {', '.join(CONFLICTS)}")
            out.append({k: v})
        elif k == "hold_days":
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_HOLD_DAYS:
                raise RulesError(fi, f"hold_days is 0 to {MAX_HOLD_DAYS}")
            out.append({k: v})
        elif k == "notify":
            out.append({k: _notify(v, fi)})
        elif k == "let_requester_choose":
            if not isinstance(v, list) or not 1 <= len(v) <= 20:
                raise RulesError(fi, "let_requester_choose is a list of 1 to 20 locations")
            locs = [location(x, f"{fi}[{j}]") for j, x in enumerate(v)]
            out.append({k: locs})
        else:
            raise RulesError(fi, f"a return rule can't {k!r}")
    has = set(keys)
    if "exclude" in has and len(has) > 1:
        raise RulesError(f, "exclude can't be combined with other actions")
    if "exclude" not in has and "place_at" not in has:
        raise RulesError(f, "a return rule places the file (place_at) or excludes it")
    return out


def _actions(a: Any, where: str, trigger: str, dataset_id: int) -> list[dict[str, Any]]:
    f = f"{where}.actions"
    if not isinstance(a, list) or not a or len(a) > 20:
        raise RulesError(f, "actions must be a list of 1 to 20")
    if trigger in RETURN_TRIGGERS:
        return _return_actions(a, f)
    out: list[dict[str, Any]] = []
    keys: list[str] = []
    for i, item in enumerate(a):
        fi = f"{f}[{i}]"
        if not isinstance(item, dict) or len(item) != 1:
            raise RulesError(fi, "each action is an object with one key")
        ((k, v),) = item.items()
        if k in keys and k != "tag":
            raise RulesError(fi, f"{k} appears twice")
        keys.append(k)
        if k in ("exclude", "remove_from_source", "keep_copy"):
            if v is not True:
                raise RulesError(fi, f"{k} is `true`")
            out.append({k: True})
        elif k == "copy_to":
            if not isinstance(v, dict) or v.get("kind") != "tier":
                raise RulesError(fi, "copy_to is a tier location")
            if str(v.get("dataset_id", dataset_id)) != str(dataset_id):
                raise RulesError(fi, "copy_to is the annex's own dataset in A2")
            out.append({"copy_to": _tier(dataset_id)})
        elif k == "remove_after_days":
            out.append({k: _number(v, fi)})
        elif k in ("policy", "tag"):
            # Accepted, they would say something nothing does yet.
            raise RulesError(fi, f"{k} actions come with phase A3; nothing would apply it yet")
        else:
            raise RulesError(fi, f"no action {k!r}")
    has = set(keys)
    if "exclude" in has and len(has) > 1:
        raise RulesError(f, "exclude can't be combined with other actions")
    allowed = {
        "arrived": {"exclude", "copy_to", "remove_from_source", "remove_after_days"},
        "changed": {"exclude", "copy_to"},
        "age": {"exclude", "remove_from_source"},
        "deleted_at_source": {"keep_copy"},
    }[trigger]  # fmt: skip
    bad = has - allowed
    if bad:
        raise RulesError(f, f"a {trigger} rule can't {sorted(bad)[0]}")
    return out


def normalize(body: Any, dataset_id: int) -> dict[str, Any]:
    """The rule set, well formed, with ids; `RulesError` if it isn't."""
    if not isinstance(body, dict):
        raise RulesError("body", "the rule set is an object")
    rules = body.get("rules")
    if not isinstance(rules, list) or not rules or len(rules) > MAX_RULES:
        raise RulesError("body.rules", f"rules must be a list of 1 to {MAX_RULES}")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    per: dict[str, int] = {}
    for i, r in enumerate(rules):
        w = f"body.rules[{i}]"
        if not isinstance(r, dict):
            raise RulesError(w, "a rule is an object")
        extra = set(r) - {"id", "trigger", "conditions", "actions", "label"}
        if extra:
            raise RulesError(w, f"no rule field {sorted(extra)[0]!r}")
        trigger = r.get("trigger")
        if trigger not in TRIGGERS:
            raise RulesError(f"{w}.trigger", f"trigger is one of {', '.join(TRIGGERS)}")
        per[trigger] = per.get(trigger, 0) + 1
        rid = r.get("id") or f"{trigger}.{per[trigger]}"
        if not isinstance(rid, str) or not _ID.match(rid) or rid in seen:
            raise RulesError(f"{w}.id", "ids are unique: letters, digits and ._- up to 64")
        seen.add(rid)
        label = r.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 200):
            raise RulesError(f"{w}.label", "a label is up to 200 characters")
        rule = {"id": rid, "trigger": trigger,
                "conditions": _conditions(r.get("conditions"), w, trigger),
                "actions": _actions(r.get("actions"), w, trigger, dataset_id)}  # fmt: skip
        if label:
            rule["label"] = label
        out.append(rule)
    norm: dict[str, Any] = {"exclude_always": list(OWN_FILES), "rules": out}
    t = body.get("template")
    days = body.get("archive_after_days")
    if t in TEMPLATES and (t != "archive_after" or (isinstance(days, int) and days > 0)):
        if body_equal({"rules": out}, template(t, dataset_id, days)):
            norm["template"] = t
            if t == "archive_after":
                norm["archive_after_days"] = days
    back = return_template_of(out)
    if back is not None:
        norm["return_template"] = back
    return norm


def body_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """The same outbound rules (the template's; return rules apart)."""
    return json.dumps(outbound(a["rules"]), sort_keys=True) == json.dumps(
        outbound(b["rules"]), sort_keys=True
    )


# --- the checks that block saving (section 4.2) ------------------------------------------------


def _removes(rule: dict[str, Any]) -> bool:
    return any("remove_from_source" in a or "remove_after_days" in a for a in rule["actions"])


def _copies(rule: dict[str, Any]) -> bool:
    return any("copy_to" in a for a in rule["actions"])


def removes(body: dict[str, Any]) -> bool:
    return any(_removes(r) for r in body["rules"])


def checks(body: dict[str, Any], *, quiet_minutes: int) -> list[dict[str, str]]:
    """Problems that block saving: `{rule, check, message}` each."""
    out: list[dict[str, str]] = []

    def bad(rule: str, check: str, message: str) -> None:
        out.append({"rule": rule, "check": check, "message": message})

    by: dict[str, list[dict[str, Any]]] = {}
    for r in body["rules"]:
        by.setdefault(r["trigger"], []).append(r)
    for trigger, rules in by.items():
        if rules[-1]["conditions"]:
            bad(rules[-1]["id"], "missing_default",
                f"the {trigger} rules must end with a rule for everything else")  # fmt: skip
        for i, later in enumerate(rules):
            for earlier in rules[:i]:
                if _covers(earlier["conditions"], later["conditions"]):
                    msg = f"never matches: rule {earlier['id']} comes first and covers it"
                    bad(later["id"], "unreachable", msg)
                    break
    out.extend(return_checks(body))
    copies_on_arrival = any(_copies(r) for r in by.get("arrived", []))
    for r in body["rules"]:
        if not _removes(r):
            continue
        if r["trigger"] in ("arrived", "changed") and not _copies(r):
            bad(r["id"], "remove_without_copy", "a rule that removes copies first")
        if r["trigger"] == "age":
            if not copies_on_arrival:
                bad(r["id"], "remove_without_copy",
                    "an age rule removes only what an arrival rule copied")  # fmt: skip
            c = r["conditions"]
            if not any(k in c for k in _DAYS):
                bad(r["id"], "remove_without_age", "an age rule that removes needs an age")
            elif "age_since_arrival_days_gt" in c and c["age_since_arrival_days_gt"] * 1440 < (
                quiet_minutes
            ):
                bad(r["id"], "remove_before_quiet", "removes before the quiet period ends")
        for a in r["actions"]:
            d = a.get("remove_after_days")
            if d is not None and d * 1440 < quiet_minutes:
                bad(r["id"], "remove_before_quiet", "removes before the quiet period ends")
    return out


# --- return rules: what they place, and where (sections 4.2 and 4.6) -------------------


def rule_get(rule: dict[str, Any], key: str, default: Any = None) -> Any:
    return next((a[key] for a in rule["actions"] if key in a), default)


def targets(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """A return rule's locations: its `place_at`, then its choices."""
    out = []
    if (p := rule_get(rule, "place_at")) is not None:
        out.append(p)
    out.extend(rule_get(rule, "let_requester_choose", []))
    return out


def in_this_annex(loc: dict[str, Any]) -> bool:
    """A nas_folder of the annex the rule set is for (no other annex or filer named)."""
    return loc["kind"] == "nas_folder" and "annex_id" not in loc and "filer_id" not in loc


def seg_match(pattern: str, name: str) -> bool:
    """`*` and `?` within one path segment, byte by byte, as the edge's
    `seg_match` (rules.rs): nothing else is special."""
    p, s = pattern.encode(), name.encode()
    pi = si = 0
    star, mark = None, 0
    while si < len(s):
        if pi < len(p) and (p[pi] == ord("?") or p[pi] == s[si]):
            pi += 1
            si += 1
        elif pi < len(p) and p[pi] == ord("*"):
            star, mark = pi, si
            pi += 1
        elif star is not None:
            pi = star + 1
            mark += 1
            si = mark
        else:
            return False
    while pi < len(p) and p[pi] == ord("*"):
        pi += 1
    return pi == len(p)


def glob_match(pattern: str, path: str) -> bool:
    """A path glob relative to the annex's root, as the edge's `glob_match`:
    `/`-separated, `*` and `?` within a segment, `**` for any number of
    segments; a glob without `/` matches the name in any folder."""
    s = path.split("/")
    if "/" not in pattern:
        return seg_match(pattern, s[-1])
    p: list[str] = []
    for x in pattern.lstrip("/").split("/"):
        if not (x == "**" and p and p[-1] == "**"):
            p.append(x)
    n, m = len(p), len(s)
    ok = [[False] * (m + 1) for _ in range(n + 1)]
    ok[n][m] = True
    for i in range(n - 1, -1, -1):
        for j in range(m, -1, -1):
            if p[i] == "**":
                ok[i][j] = ok[i + 1][j] or (j < m and ok[i][j + 1])
            else:
                ok[i][j] = j < m and seg_match(p[i], s[j]) and ok[i + 1][j + 1]
    return ok[0][0]


def ext_match(exts: list[str], path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return any(name.endswith("." + e) for e in exts)


def glob_may_match_under(glob: str, folder: str) -> bool:
    """Whether `glob` may match a file somewhere below `folder` (a file placed
    there keeps its own relative path, so its name can be anything).
    Conservative: True whenever it can't tell."""
    if "/" not in glob:
        return True  # a name, in any folder
    gs = glob.lstrip("/").split("/")
    fs = folder.split("/") if folder else []
    for i, f in enumerate(fs):
        if i >= len(gs):
            return False
        if gs[i] == "**":
            return True
        if not seg_match(gs[i], f):
            return False
    return len(gs) > len(fs)


def removing_rules_over(rules: list[dict[str, Any]], folder: str | None) -> list[dict[str, Any]]:
    """The outbound rules that remove, and may cover files placed in `folder`
    (relative to the annex root; None: the file's own folder, where its
    removal came from)."""
    out = []
    for r in outbound(rules):
        if not _removes(r):
            continue
        globs = r["conditions"].get("path")
        if folder is None or not globs or any(glob_may_match_under(g, folder) for g in globs):
            out.append(r)
    return out


def drop_rule(r: dict[str, Any]) -> bool:
    """Removes on arrival, at once: Move now's drop folder."""
    return r["trigger"] == "arrived" and any("remove_from_source" in a for a in r["actions"])


def target_problems(
    rule: dict[str, Any], folder: str | None, outbound_rules: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """Section 4.2's check for one target in an annex's folder: a placed file
    must not go straight back out. `folder` is the target relative to the
    annex root (None for the origin), `outbound_rules` that annex's rules."""
    out: list[tuple[str, str]] = []
    over = removing_rules_over(outbound_rules, folder)
    if any(drop_rule(r) for r in over):
        out.append(("return_into_drop_folder",
                    "a recalled file placed there would be removed again at once (a Move-now"
                    " folder); place it outside the annex"))  # fmt: skip
    elif over and not rule_get(rule, "hold_days", 0):
        out.append(("return_without_hold",
                    f"rule {over[0]['id']} removes files there: hold a placed file for some"
                    " days (hold_days)"))  # fmt: skip
    return out


def return_checks(body: dict[str, Any]) -> list[dict[str, str]]:
    """The checks for return rules this module can make alone (targets in
    the annex's own folders). Targets in another annex are checked with its
    rules by the caller (`web/routes_annex_rules.py`)."""
    out: list[dict[str, str]] = []
    for r in returns(body["rules"]):
        conflict = rule_get(r, "on_conflict", "restored_name")
        for loc in targets(r):
            if (
                loc["kind"] == "nas_folder"
                and conflict == "overwrite_keep_version"
                and not in_this_annex(loc)
            ):
                out.append(
                    {
                        "rule": r["id"],
                        "check": "overwrite_outside_annex",
                        "message": "a NAS file is replaced only inside this annex, where the file"
                        " it replaces has its own verified copy",
                    }
                )
            if in_this_annex(loc):
                folder = None if loc.get("origin") else loc["path"]
                for check, msg in target_problems(r, folder, body["rules"]):
                    out.append({"rule": r["id"], "check": check, "message": msg})
    return out


def outside_targets(body: dict[str, Any] | None) -> set[str]:
    """Return locations outside the annex's own folders, canonical: adding one
    needs an ArchiveAdmin (section 4.3; anti-pattern 7)."""
    out: set[str] = set()
    for r in returns((body or {}).get("rules", [])):
        for loc in targets(r):
            if not in_this_annex(loc):
                out.add(json.dumps(loc, sort_keys=True))
    return out


def ruleset_lock_key(annex_id: int) -> str:
    """The advisory lock an approval takes before it replaces the running rule set,
    and a replace question takes shared while it reads it (the confirm pass of #98,
    C9). A row lock (`FOR SHARE`) would need the `api` app to hold UPDATE on rule sets."""
    return f"annex_ruleset:{int(annex_id)}"


def replaces_on_nas(body: dict[str, Any] | None) -> list[str]:
    """The return rules that may replace a file on the NAS (A3.4, decision 85): a
    NAS target in this annex with `overwrite_keep_version`. The approval page marks
    them "replaces files on the NAS"; no other switch is needed. Only the file's own
    name at its origin is ever replaced (decision 82)."""
    out: list[str] = []
    for r in returns((body or {}).get("rules", [])):
        if rule_get(r, "on_conflict", "restored_name") != "overwrite_keep_version":
            continue
        if any(loc["kind"] == "nas_folder" and in_this_annex(loc) for loc in targets(r)):
            out.append(r["id"])
    return out


def _covers(ec: dict[str, Any], lc: dict[str, Any]) -> bool:
    """Every file `lc` matches, `ec` matches too (so a later rule with `lc`
    never runs): each of `ec`'s conditions is in `lc`, as narrow or narrower.
    Conservative: globs compare as strings."""
    for k, v in ec.items():
        if k not in lc:
            return False
        w = lc[k]
        if k in _LISTS:
            if not set(w) <= set(v):
                return False
        elif k == "size_lt":
            if w > v:
                return False
        elif w < v:  # size_gte and the ages: at least as large
            return False
    return True


def removal_rules(body: dict[str, Any] | None) -> set[str]:
    """The rule set's removals, as canonical strings: a rule that removes,
    with its trigger, conditions and removing actions, and its position
    among its trigger's rules (an earlier exclude can stop it)."""
    if not body:
        return set()
    out: set[str] = set()
    by: dict[str, list[dict[str, Any]]] = {}
    for r in body.get("rules", []):
        by.setdefault(r.get("trigger", ""), []).append(r)
    for trigger, rules in by.items():
        for i, r in enumerate(rules):
            if not _removes(r):
                continue
            before = [{"c": e.get("conditions", {}), "a": e.get("actions", [])} for e in rules[:i]]
            out.add(json.dumps({"t": trigger, "before": before, "c": r.get("conditions", {}),
                                "a": r.get("actions", [])}, sort_keys=True))  # fmt: skip
    return out


#: The triggers whose rules decide what a removal applies to: an age rule
#: removes what the arrival rules copied, so widening the copy widens it.
_REMOVAL_SCOPE = ("arrived", "changed", "age")


def _scope(body: dict[str, Any] | None) -> str:
    rules = [r for r in (body or {}).get("rules", []) if r.get("trigger") in _REMOVAL_SCOPE]
    return json.dumps([{k: r.get(k) for k in ("trigger", "conditions", "actions")}
                       for r in rules], sort_keys=True)  # fmt: skip


def widens(new: dict[str, Any], old: dict[str, Any] | None) -> bool:
    """Adds or widens a removal (section 4.3), which takes a fresh dry run: a
    removing rule the running set doesn't have exactly as it is and where it
    is, or, when the new set removes at all, any change to its arrived,
    changed or age rules."""
    if not removes(new):
        return False
    return bool(removal_rules(new) - removal_rules(old)) or _scope(new) != _scope(old)


def needs_archive_admin(new: dict[str, Any], old: dict[str, Any] | None) -> bool:
    """Only an ArchiveAdmin approves a rule set that removes anything (A2.2's
    gates read who approved it), anything that widens a removal, one that
    adds a return target outside the annex's own folders (section 4.3), and one
    with a rule that replaces files on the NAS (A3.4, decision 85: a replace
    deletes a NAS file)."""
    return (
        removes(new)
        or widens(new, old)
        or bool(outside_targets(new) - outside_targets(old))
        or bool(replaces_on_nas(new))
    )
