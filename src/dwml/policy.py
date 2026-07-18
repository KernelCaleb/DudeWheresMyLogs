"""Policy checks (v3.0): user-defined log health rules.

A policy file (YAML or JSON) declares rules that become first-class checks
for the scan: they get their own report sections, summary lines, --checks /
--fail-on names, CI exit-code behavior, and diff support -- all through the
same registry the built-in checks use.

Positioning: Azure Policy owns enforcement of configuration *intent*
(deny/deployIfNotExists at deploy time). These rules assert things Azure
Policy structurally cannot evaluate: whether data actually flows, whether
anyone reads it, what it costs, and conditions aggregated across multiple
diagnostic settings. Configuration-shaped rules are still supported because
this tool runs with plain Reader rights, where Policy assignment is not an
option.

Unknowns never violate: whatever the credential could not see (unresolved
regions, unqueryable workspaces, missing cost data) is skipped, never
guessed -- consistent with every built-in check.
"""
import fnmatch
import json
from dataclasses import dataclass, field

from .checks import Check
from .diagnostics import _norm_region

_SEVERITIES = ("fail", "warn", "info")
_SCOPES = ("resource", "workspace")

_RULE_KEYS = {"name", "title", "description", "severity", "scope",
              "match", "require", "forbid"}
_MATCH_KEYS = {
    "resource": {"type", "name", "resource_group", "region", "subscription"},
    "workspace": {"name", "resource_group", "region", "subscription"},
}
_REQUIRE_KEYS = {
    "resource": {"diagnostics", "categories", "destination_type",
                 "destination_region", "la_destination_type", "flowing"},
    "workspace": {"retention_days_at_least", "query_auditing", "queried",
                  "max_monthly_cost", "sentinel", "daily_cap"},
}
_FORBID_KEYS = {
    "resource": {"duplicate", "cross_region", "dead_destination", "silent",
                 "destination_type", "destination_region", "la_destination_type"},
    "workspace": set(),
}


class PolicyError(ValueError):
    """A policy file could not be loaded or validated."""


@dataclass(frozen=True)
class PolicyRule:
    name: str
    title: str
    description: str
    severity: str            # "fail", "warn", or "info"
    scope: str               # "resource" or "workspace"
    match: dict = field(default_factory=dict)
    require: dict = field(default_factory=dict)
    forbid: dict = field(default_factory=dict)


def _patterns(value):
    """Normalize a match/require value to a list of string patterns."""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _matches_any(value, patterns):
    """Case-insensitive wildcard/exact match against one or more patterns."""
    normalized = str(value).lower()
    return any(fnmatch.fnmatch(normalized, str(p).lower())
               for p in _patterns(patterns))


def _validate_rule(raw, index, seen_names):
    if not isinstance(raw, dict):
        raise PolicyError(f"rule #{index + 1}: expected a mapping")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise PolicyError(f"rule #{index + 1}: 'name' is required")
    label = f"rule '{name}'"
    if name in seen_names:
        raise PolicyError(f"{label}: duplicate rule name")

    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise PolicyError(f"{label}: unknown key(s): {', '.join(sorted(unknown))}")

    severity = raw.get("severity", "fail")
    if severity not in _SEVERITIES:
        raise PolicyError(
            f"{label}: severity must be one of {', '.join(_SEVERITIES)}")
    scope = raw.get("scope", "resource")
    if scope not in _SCOPES:
        raise PolicyError(f"{label}: scope must be one of {', '.join(_SCOPES)}")

    match = raw.get("match") or {}
    require = raw.get("require") or {}
    forbid = raw.get("forbid") or {}
    for section, value, allowed in (
        ("match", match, _MATCH_KEYS[scope]),
        ("require", require, _REQUIRE_KEYS[scope]),
        ("forbid", forbid, _FORBID_KEYS[scope]),
    ):
        if not isinstance(value, dict):
            raise PolicyError(f"{label}: '{section}' must be a mapping")
        unknown = set(value) - allowed
        if unknown:
            raise PolicyError(
                f"{label}: unknown {section} key(s) for scope '{scope}': "
                f"{', '.join(sorted(unknown))}")
    if not require and not forbid:
        raise PolicyError(f"{label}: needs a 'require' or 'forbid' section")

    return PolicyRule(
        name=name,
        title=str(raw.get("title") or name),
        description=str(raw.get("description") or raw.get("title") or name),
        severity=severity,
        scope=scope,
        match=match,
        require=require,
        forbid=forbid,
    )


def load_policy(path):
    """Load and validate one policy file; returns a tuple of PolicyRules."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise PolicyError(f"cannot read policy file: {e}") from e

    if str(path).lower().endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise PolicyError(f"{path}: invalid JSON: {e}") from e
    else:
        try:
            import yaml
        except ModuleNotFoundError as e:  # pragma: no cover
            raise PolicyError(
                "pyyaml is required for YAML policy files "
                "(pip install pyyaml), or use a .json policy file") from e
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise PolicyError(f"{path}: invalid YAML: {e}") from e

    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise PolicyError(f"{path}: expected a top-level 'rules' list")
    rules = []
    seen = set()
    for i, raw in enumerate(data["rules"]):
        rule = _validate_rule(raw, i, seen)
        seen.add(rule.name)
        rules.append(rule)
    if not rules:
        raise PolicyError(f"{path}: policy has no rules")
    return tuple(rules)


def load_policy_files(paths):
    """Load several policy files; rule names must be unique across all."""
    rules = []
    seen = set()
    for path in paths:
        for rule in load_policy(path):
            if rule.name in seen:
                raise PolicyError(
                    f"rule '{rule.name}' defined in more than one policy file")
            seen.add(rule.name)
            rules.append(rule)
    return tuple(rules)


def rules_need_workspace_analysis(rules):
    """True if any rule depends on workspace analysis (scope or liveness)."""
    return any(
        rule.scope == "workspace"
        or rule.require.get("flowing")
        or rule.forbid.get("silent")
        for rule in rules)


# ---------------------------------------------------------------------------
# Evaluation

def _match_resource(match, r):
    for key, value in (
        ("type", r.resource_type),
        ("name", r.resource_name),
        ("resource_group", r.resource_group),
        ("region", r.resource_location),
    ):
        if key in match and not _matches_any(value, match[key]):
            return False
    if "subscription" in match:
        if not (_matches_any(r.subscription_name, match["subscription"])
                or _matches_any(r.subscription_id, match["subscription"])):
            return False
    return True


def _match_workspace(match, ws):
    for key, value in (
        ("name", ws.name),
        ("resource_group", ws.resource_group),
        ("region", ws.region),
        ("subscription", ws.subscription_id),
    ):
        if key in match and not _matches_any(value, match[key]):
            return False
    return True


def _dest_satisfies(d, require):
    """True / False / None(unknown) for one destination against the
    require.destination_* constraints."""
    dt = require.get("destination_type")
    if dt and not _matches_any(d.get("type", ""), dt):
        return False
    la = require.get("la_destination_type")
    if la:
        if d.get("type") != "Log Analytics":
            return False
        value = d.get("la_destination_type") or ""
        if not value:
            return None  # mode not reported by ARM: unknown, never violates
        if not _matches_any(value, la):
            return False
    dr = require.get("destination_region")
    if dr:
        region = _norm_region(d.get("region", ""))
        if not region:
            return None  # region unresolved (no access): unknown
        if str(dr).lower() == "same":
            src = _norm_region(d.get("_src_region", ""))
            if not src or src == "global":
                return None
            if region != src:
                return False
        elif not _matches_any(region, dr):
            return False
    return True


def _resource_violates(rule, r):
    """Whether one matched resource violates the rule. Unknowns never violate."""
    if r.status in ("Not Supported", "Error"):
        return False
    require, forbid = rule.require, rule.forbid

    if require:
        if require.get("diagnostics") and r.status == "Missing":
            return True

        dests = [d for d in r.destinations if not d.get("not_found")]
        needs_dest = any(k in require for k in (
            "destination_type", "destination_region", "la_destination_type"))
        verdicts = [(d, _dest_satisfies(d, require)) for d in dests]

        if needs_dest:
            if not any(v is True for _, v in verdicts):
                if any(v is None for _, v in verdicts):
                    pass  # unknown: cannot claim violation
                else:
                    return True

        pool = [d for d, v in verdicts if v is not False]

        categories = require.get("categories")
        if categories:
            enabled = {c.lower() for d in pool
                       for c in d.get("log_categories", [])}
            if "alllogs" not in enabled:
                if any(str(c).lower() not in enabled for c in _patterns(categories)):
                    return True

        if require.get("flowing"):
            la = [d for d in pool if d.get("type") == "Log Analytics"]
            if not la:
                return True
            known = [d for d in la if "silent" in d]
            if known and all(d["silent"] for d in known):
                return True
            # liveness unknown (workspace not queryable): never violates

    if forbid:
        if forbid.get("duplicate") and r.duplicate:
            return True
        for d in r.destinations:
            if forbid.get("cross_region") and d.get("cross_region"):
                return True
            if forbid.get("dead_destination") and d.get("not_found"):
                return True
            if forbid.get("silent") and d.get("silent"):
                return True
            ft = forbid.get("destination_type")
            if ft and _matches_any(d.get("type", ""), ft):
                return True
            fr = forbid.get("destination_region")
            if fr and d.get("region") and _matches_any(
                    _norm_region(d["region"]), fr):
                return True
            fl = forbid.get("la_destination_type")
            if fl and d.get("la_destination_type") and _matches_any(
                    d["la_destination_type"], fl):
                return True
    return False


def _workspace_violates(rule, ws):
    """Whether one matched workspace violates the rule. Unknowns never violate."""
    require = rule.require
    config_known = bool(ws.sku or ws.retention_days)

    minimum = require.get("retention_days_at_least")
    if minimum and ws.retention_days and ws.retention_days < int(minimum):
        return True
    if require.get("query_auditing") and ws.audit_enabled is False:
        return True
    if (require.get("queried") and ws.audit_enabled is True
            and ws.query_count == 0):
        return True
    ceiling = require.get("max_monthly_cost")
    if (ceiling is not None and ws.est_monthly_total is not None
            and ws.est_monthly_total > float(ceiling)):
        return True
    if "sentinel" in require and ws.sentinel_enabled is not None:
        if bool(ws.sentinel_enabled) != bool(require["sentinel"]):
            return True
    if require.get("daily_cap") and config_known and not ws.daily_cap_gb:
        return True
    return False


def evaluate_policy(rules, results, ws_results):
    """Annotate results/workspaces in place with violated rule names."""
    for rule in rules:
        if rule.scope == "resource":
            if rule.require.get("destination_region"):
                # "same" comparisons need the source region on each dest
                for r in results:
                    for d in r.destinations:
                        d["_src_region"] = r.resource_location
            for r in results:
                if (_match_resource(rule.match, r)
                        and _resource_violates(rule, r)):
                    r.policy_violations.append(rule.name)
        else:
            for ws in ws_results:
                if (_match_workspace(rule.match, ws)
                        and _workspace_violates(rule, ws)):
                    ws.policy_violations.append(rule.name)
    # Drop the evaluation-only helper key so it never reaches reports
    for r in results:
        for d in r.destinations:
            d.pop("_src_region", None)


# ---------------------------------------------------------------------------
# Registry integration

_SEVERITY_STYLE = {"fail": "err", "warn": "warn", "info": ""}


def make_checks(rules):
    """Turn policy rules into Check objects for the shared registry."""
    checks = []
    for rule in rules:
        checks.append(Check(
            name=rule.name,
            title=rule.title,
            description=rule.description,
            detect=(lambda item, n=rule.name:
                    n in (getattr(item, "policy_violations", None) or ())),
            default_fail_on=rule.severity == "fail",
            severity=_SEVERITY_STYLE[rule.severity],
            row_kind="duplicate",
            default_open=rule.severity == "fail",
            anchor=("workspace-usage" if rule.scope == "workspace"
                    else f"policy-{rule.name}"),
            dest_label="Destinations" if rule.scope == "resource" else "",
            scope=rule.scope,
            policy_severity=rule.severity,
        ))
    return tuple(checks)
