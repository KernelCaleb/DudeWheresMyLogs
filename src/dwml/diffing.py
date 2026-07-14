"""Report diffing (v2.6): what changed between two scans.

Compares the machine-readable payloads of two saved reports and shows new
findings, resolved findings, and cost deltas per check. Works on JSON
reports and on HTML reports (which embed the same payload in their
dwml-data script block), in any combination -- so any report anyone ever
saved can be diffed without re-scanning.
"""
import argparse
import json
import sys
from dataclasses import fields

from .checks import CHECK_NAMES, CHECKS, DEFAULT_FAIL_ON
from .costs import fmt_usd
from .diagnostics import DiagnosticResult
from .tenant import SubscriptionAudit
from .term import paint, supports_color
from .workspaces import WorkspaceUsage, workspace_status

_HTML_MARKER = '<script type="application/json" id="dwml-data">'


def load_payload(path):
    """Load a report payload from a dwml JSON report or HTML report."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if content.lstrip().startswith("{"):
        return json.loads(content)
    start = content.find(_HTML_MARKER)
    if start == -1:
        raise ValueError(
            f"{path}: not a DudeWheresMyLogs JSON report or HTML report "
            "with an embedded dwml-data payload")
    start += len(_HTML_MARKER)
    end = content.index("</script>", start)
    return json.loads(content[start:end].replace("<\\/", "</"))


def _revive(cls, data):
    """Rebuild a dataclass from a payload dict, ignoring unknown keys."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _pools(payload):
    """Reconstruct per-scope item pools from a payload.

    Workspace and subscription pools are None (not comparable) when that
    analysis was not part of the scan that produced the payload.
    """
    return {
        "resource": [_revive(DiagnosticResult, r)
                     for r in payload.get("results", [])],
        "workspace": (
            [_revive(WorkspaceUsage, w) for w in payload["workspaces"]]
            if "workspaces" in payload else None),
        "subscription": (
            [_revive(SubscriptionAudit, s) for s in payload["subscription_audits"]]
            if "subscription_audits" in payload else None),
    }


def _item_key_label(item, scope):
    """(dedup key, display label dict) for one flagged item."""
    if scope == "resource":
        return item.resource_id.lower(), {
            "id": item.resource_id,
            "name": item.resource_name,
            "type": item.resource_type,
            "resource_group": item.resource_group,
            "subscription": item.subscription_name,
        }
    if scope == "workspace":
        return item.workspace_id.lower(), {
            "id": item.workspace_id,
            "name": item.name,
            "subscription": item.subscription_id,
            "status": workspace_status(item),
        }
    return item.subscription_id.lower(), {
        "id": item.subscription_id,
        "name": item.subscription_name,
    }


def compute_diff(old, new):
    """Compare two payloads; returns a JSON-serializable diff structure."""
    old_pools, new_pools = _pools(old), _pools(new)

    old_ids = {r.resource_id.lower() for r in old_pools["resource"]}
    new_ids = {r.resource_id.lower() for r in new_pools["resource"]}

    diff = {
        "old_generated_at": old.get("generated_at", ""),
        "new_generated_at": new.get("generated_at", ""),
        "resources": {
            "old_total": len(old_ids),
            "new_total": len(new_ids),
            "added": len(new_ids - old_ids),
            "removed": len(old_ids - new_ids),
        },
        "checks": {},
        "skipped": [],
        "costs": {},
    }

    for check in CHECKS:
        if old_pools[check.scope] is None or new_pools[check.scope] is None:
            diff["skipped"].append(check.name)
            continue
        old_items = dict(
            _item_key_label(i, check.scope)
            for i in old_pools[check.scope] if check.detect(i))
        new_items = dict(
            _item_key_label(i, check.scope)
            for i in new_pools[check.scope] if check.detect(i))
        added = [new_items[k] for k in sorted(new_items.keys() - old_items.keys())]
        resolved = [old_items[k] for k in sorted(old_items.keys() - new_items.keys())]
        diff["checks"][check.name] = {
            "title": check.title,
            "old_count": len(old_items),
            "new_count": len(new_items),
            "added": added,
            "resolved": resolved,
        }

    for key in ("est_monthly_spend_usd", "est_monthly_waste_usd"):
        old_v = old.get("summary", {}).get(key)
        new_v = new.get("summary", {}).get(key)
        if (old_v is not None or new_v is not None) and old_v != new_v:
            diff["costs"][key] = {"old": old_v, "new": new_v}

    return diff


def diff_has_changes(diff):
    """True if any check gained or lost findings."""
    return any(c["added"] or c["resolved"] for c in diff["checks"].values())


def new_finding_categories(diff):
    """Check names that gained at least one finding."""
    return [name for name, c in diff["checks"].items() if c["added"]]


def _short_type(resource_type):
    if resource_type.lower().startswith("microsoft."):
        return resource_type[len("microsoft."):]
    return resource_type


def _label_line(label):
    parts = [label.get("name", "")]
    if label.get("type"):
        parts.append(_short_type(label["type"]))
    if label.get("resource_group"):
        parts.append(label["resource_group"])
    if label.get("subscription"):
        parts.append(label["subscription"])
    if label.get("status"):
        parts.append(label["status"])
    return "  ".join(p for p in parts if p)


def _fmt_usd_delta(old_v, new_v):
    if old_v is None or new_v is None:
        return f"{fmt_usd(old_v)} -> {fmt_usd(new_v)}"
    delta = new_v - old_v
    sign = "+" if delta >= 0 else "-"
    return f"{fmt_usd(old_v)} -> {fmt_usd(new_v)} ({sign}{fmt_usd(abs(delta))})"


_COST_TITLES = {
    "est_monthly_spend_usd": "Est. monthly log spend",
    "est_monthly_waste_usd": "Est. monthly waste (findings)",
}


def render_text(diff, color=False):
    """Human-readable diff for the terminal."""
    def p(text, *styles):
        return paint(text, *styles, enabled=color)

    lines = [p("DudeWheresMyLogs report diff", "bold")]
    lines.append(
        f"  old  generated {diff['old_generated_at'] or 'unknown'}")
    lines.append(
        f"  new  generated {diff['new_generated_at'] or 'unknown'}")
    res = diff["resources"]
    lines.append(
        f"  resources evaluated  {res['old_total']} -> {res['new_total']}"
        f"  (+{res['added']} added, -{res['removed']} removed)")
    lines.append("")

    for check in diff["checks"].values():
        added, resolved = check["added"], check["resolved"]
        if not added and not resolved:
            lines.append(p(
                f"{check['title']}: unchanged ({check['new_count']})", "dim"))
            continue
        head = (f"{check['title']}: {check['old_count']} -> {check['new_count']}"
                f"  (+{len(added)} new, -{len(resolved)} resolved)")
        lines.append(p(head, "bold", "yellow" if added else "green"))
        for label in added:
            lines.append("  " + p("+ " + _label_line(label), "yellow"))
        for label in resolved:
            lines.append("  " + p("- " + _label_line(label), "green"))

    if diff["skipped"]:
        lines.append("")
        lines.append(p(
            "not compared (analysis missing from one report): "
            + ", ".join(diff["skipped"]), "dim"))

    if diff["costs"]:
        lines.append("")
        for key, values in diff["costs"].items():
            lines.append(
                f"{_COST_TITLES[key]}: {_fmt_usd_delta(values['old'], values['new'])}")

    lines.append("")
    total_added = sum(len(c["added"]) for c in diff["checks"].values())
    total_resolved = sum(len(c["resolved"]) for c in diff["checks"].values())
    if total_added or total_resolved:
        summary = f"{total_added} new finding(s), {total_resolved} resolved."
        lines.append(p(summary, "bold"))
    else:
        lines.append(p("No finding changes.", "bold", "green"))
    return "\n".join(lines) + "\n"


def render_markdown(diff):
    """Markdown diff for tickets and PRs."""
    lines = [
        "# Log Health Report Diff",
        "",
        f"Old generated {diff['old_generated_at'] or 'unknown'} | "
        f"New generated {diff['new_generated_at'] or 'unknown'}",
        "",
        "## Summary",
        "",
        "| Finding | Old | New | New Findings | Resolved |",
        "|---|---|---|---|---|",
    ]
    res = diff["resources"]
    for check in diff["checks"].values():
        lines.append(
            f"| {check['title']} | {check['old_count']} | {check['new_count']} "
            f"| {len(check['added'])} | {len(check['resolved'])} |")
    lines.append(
        f"| Resources evaluated | {res['old_total']} | {res['new_total']} "
        f"| {res['added']} | {res['removed']} |")
    lines.append("")

    for key, values in diff["costs"].items():
        lines.append(f"{_COST_TITLES[key]}: "
                     f"{_fmt_usd_delta(values['old'], values['new'])}")
    if diff["costs"]:
        lines.append("")

    for check in diff["checks"].values():
        if not check["added"] and not check["resolved"]:
            continue
        lines.append(f"## {check['title']}")
        lines.append("")
        for label in check["added"]:
            lines.append(f"- **New:** {_label_line(label)}")
        for label in check["resolved"]:
            lines.append(f"- Resolved: {_label_line(label)}")
        lines.append("")

    if diff["skipped"]:
        lines.append("Not compared (analysis missing from one report): "
                     + ", ".join(diff["skipped"]))
        lines.append("")

    return "\n".join(lines)


def build_diff_parser():
    parser = argparse.ArgumentParser(
        prog="DudeWheresMyLogs diff",
        description="Compare two saved reports (JSON or HTML, in any "
                    "combination) and show new findings, resolved findings, "
                    "and cost deltas.",
    )
    parser.add_argument("old", help="Earlier report (.json or .html)")
    parser.add_argument("new", help="Later report (.json or .html)")
    parser.add_argument(
        "-f", "--format",
        choices=["text", "md", "json"],
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "-o", "--output",
        metavar="FILE",
        help="Write the diff to a file instead of stdout.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit 1 when new findings appeared in --fail-on categories "
             "(0 otherwise, 2 on read errors).",
    )
    parser.add_argument(
        "--fail-on",
        action="append",
        metavar="CATEGORY",
        help="Categories that count as regressions in --ci mode. "
             f"Choices: {', '.join(CHECK_NAMES)}. Repeatable or "
             f"comma-separated. Default: {','.join(DEFAULT_FAIL_ON)}.",
    )
    return parser


def run_diff(argv):
    """Entry point for the diff subcommand; returns a process exit code."""
    parser = build_diff_parser()
    args = parser.parse_args(argv)

    fail_on = list(DEFAULT_FAIL_ON)
    if args.fail_on:
        fail_on = [part.strip() for value in args.fail_on
                   for part in value.split(",") if part.strip()]
        invalid = [c for c in fail_on if c not in CHECK_NAMES]
        if invalid:
            parser.error(f"invalid --fail-on category: {', '.join(invalid)} "
                         f"(choose from {', '.join(CHECK_NAMES)})")

    try:
        old = load_payload(args.old)
        new = load_payload(args.new)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        sys.stderr.write(f"Error: {e}\n")
        return 2

    diff = compute_diff(old, new)

    if args.format == "json":
        rendered = json.dumps(diff, indent=2, sort_keys=True) + "\n"
    elif args.format == "md":
        rendered = render_markdown(diff)
    else:
        color = args.output is None and supports_color(sys.stdout)
        rendered = render_text(diff, color=color)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"Diff saved to: {args.output}")
    else:
        sys.stdout.write(rendered)

    if args.ci and any(c in fail_on for c in new_finding_categories(diff)):
        return 1
    return 0
