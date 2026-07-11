import csv
import html
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime

from .checks import get_checks, is_healthy
from .diagnostics import has_cross_region, has_dead_destination
from .workspaces import workspace_status


def generate_report(results, fmt="html", output=None, summary_only=False, checks=None,
                    ws_results=None, sub_audits=None):
    """Generate a report in the specified format.

    Args:
        results: list of DiagnosticResult objects
        fmt: "html", "csv", "json", or "md"
        output: output file path (auto-generated if None)
        summary_only: omit per-resource detail for non-finding sections
            (HTML and Markdown only)
        checks: iterable of active check names (None = all); controls which
            finding sections appear in HTML/Markdown reports
        ws_results: list of WorkspaceUsage from workspace analysis (None if
            workspace checks were not run)
        sub_audits: list of SubscriptionAudit from the activity log check
            (None if not run)

    Returns:
        The output file path.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "csv":
        if output is None:
            output = f"DudeWheresMyLogs_{timestamp}.csv"
        return generate_csv(results, output)
    if fmt == "json":
        if output is None:
            output = f"DudeWheresMyLogs_{timestamp}.json"
        return generate_json(results, output, ws_results=ws_results,
                             sub_audits=sub_audits)
    if fmt == "md":
        if output is None:
            output = f"DudeWheresMyLogs_{timestamp}.md"
        return generate_markdown(results, output, summary_only=summary_only,
                                 checks=checks, ws_results=ws_results,
                                 sub_audits=sub_audits)
    if output is None:
        output = f"DudeWheresMyLogs_{timestamp}.html"
    return generate_html(results, output, summary_only=summary_only, checks=checks,
                         ws_results=ws_results, sub_audits=sub_audits)


def build_payload(results, ws_results=None, sub_audits=None):
    """Build the machine-readable report payload (shared by JSON and HTML)."""
    status_counts = Counter(r.status for r in results)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total_resources": len(results),
            "subscriptions_scanned": len({r.subscription_id for r in results}),
            "status_counts": dict(status_counts),
            "duplicate_count": sum(1 for r in results if r.duplicate),
            "dead_destination_count": sum(1 for r in results if has_dead_destination(r)),
            "cross_region_count": sum(1 for r in results if has_cross_region(r)),
            "silent_resource_count": sum(
                1 for r in results
                if any(d.get("silent") for d in r.destinations)),
        },
        "results": [asdict(result) for result in results],
    }
    if ws_results is not None:
        for check in get_checks(scope="workspace"):
            key = check.name.replace("-", "_") + "_count"
            payload["summary"][key] = sum(1 for ws in ws_results if check.detect(ws))
        payload["workspaces"] = [asdict(ws) for ws in ws_results]
    if sub_audits is not None:
        payload["summary"]["no_activity_log_export_count"] = sum(
            1 for s in sub_audits if s.exported is False)
        payload["subscription_audits"] = [asdict(s) for s in sub_audits]
    return payload


def generate_csv(results, output):
    """Write results to CSV."""
    fieldnames = [
        "Subscription",
        "Subscription ID",
        "Resource Group",
        "Resource Type",
        "Resource Name",
        "Resource ID",
        "Location",
        "Status",
        "Destinations",
        "Log Categories",
        "Duplicate",
        "Dead Destination",
        "Cross Region",
        "Error",
    ]

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            dest_str = "; ".join(
                f"[{d.get('setting_name', '')}] {d['type']}: "
                f"{d.get('name', '')} ({d.get('region', '')}) ({d['id']})"
                + (" (NOT FOUND)" if d.get("not_found") else "")
                for d in r.destinations
            ) if r.destinations else ""
            cats_str = "; ".join(
                f"[{d.get('setting_name', '')}] "
                + ", ".join(d.get("log_categories", []))
                for d in r.destinations
                if d.get("log_categories")
            ) if r.destinations else ""
            writer.writerow({
                "Subscription": r.subscription_name,
                "Subscription ID": r.subscription_id,
                "Resource Group": r.resource_group,
                "Resource Type": r.resource_type,
                "Resource Name": r.resource_name,
                "Resource ID": r.resource_id,
                "Location": r.resource_location,
                "Status": r.status,
                "Destinations": dest_str,
                "Log Categories": cats_str,
                "Duplicate": "Yes" if r.duplicate else "",
                "Dead Destination": "Yes" if has_dead_destination(r) else "",
                "Cross Region": "Yes" if has_cross_region(r) else "",
                "Error": r.error_message,
            })

    return output


def generate_json(results, output, ws_results=None, sub_audits=None):
    """Write results to a machine-readable JSON report."""
    payload = build_payload(results, ws_results=ws_results, sub_audits=sub_audits)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")

    return output


def _md_escape(value):
    """Escape a value for use inside a Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _dest_inline(r, dest_filter=None):
    """One-line destination summary for a result, for Markdown/CSV-style views."""
    parts = []
    for d in r.destinations:
        if dest_filter is not None and not dest_filter(d):
            continue
        label = f"{d.get('type', '')}: {d.get('name', '')}"
        region = d.get("region", "")
        if d.get("not_found"):
            label += " (not found)"
        elif region:
            label += f" ({region})"
        parts.append(label)
    return "; ".join(parts)


def generate_markdown(results, output, summary_only=False, checks=None, ws_results=None,
                      sub_audits=None):
    """Write results to a findings-focused Markdown report.

    Healthy and informational resources are summarized as counts only;
    each active check gets a per-resource findings table unless summary_only
    is set. Workspace usage and subscription audits (when analyzed) get
    their own tables.
    """
    status_counts = Counter(r.status for r in results)
    subs = sorted({r.subscription_name for r in results}, key=str.lower)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    active_checks = get_checks(checks, scope="resource")
    ws_checks = get_checks(checks, scope="workspace")
    sub_checks = get_checks(checks, scope="subscription")
    ws_results = ws_results or []
    sub_audits = sub_audits or []
    findings = {c.name: [r for r in results if c.detect(r)] for c in active_checks}
    healthy_count = sum(1 for r in results if is_healthy(r, checks))

    lines = [
        "# Azure Diagnostic Settings Audit",
        "",
        f"Generated {timestamp} | {len(subs)} subscription(s) | "
        f"{len(results)} resource(s) evaluated",
        "",
        "## Summary",
        "",
        "| Finding | Count |",
        "|---|---|",
    ]
    for c in active_checks:
        lines.append(f"| {c.title} | {len(findings[c.name])} |")
    for c in ws_checks:
        lines.append(f"| {c.title} | {sum(1 for ws in ws_results if c.detect(ws))} |")
    for c in sub_checks:
        lines.append(f"| {c.title} | {sum(1 for s in sub_audits if c.detect(s))} |")
    lines.extend([
        f"| Healthy | {healthy_count} |",
        f"| Not supported | {status_counts.get('Not Supported', 0)} |",
        f"| Errors | {status_counts.get('Error', 0)} |",
        "",
    ])

    def _sorted(items):
        return sorted(items, key=lambda r: (
            r.subscription_name.lower(), r.resource_group.lower(),
            r.resource_type.lower(), r.resource_name.lower(),
        ))

    def _table(title, items, dest_col=None, dest_filter=None):
        lines.append(f"## {title} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("None.")
            lines.append("")
            return
        header = "| Subscription | Resource Group | Type | Name | Region |"
        divider = "|---|---|---|---|---|"
        if dest_col:
            header += f" {dest_col} |"
            divider += "---|"
        lines.append(header)
        lines.append(divider)
        for r in _sorted(items):
            row = (
                f"| {_md_escape(r.subscription_name)} "
                f"| {_md_escape(r.resource_group)} "
                f"| {_md_escape(_short_type(r.resource_type))} "
                f"| {_md_escape(r.resource_name)} "
                f"| {_md_escape(r.resource_location)} |"
            )
            if dest_col:
                row += f" {_md_escape(_dest_inline(r, dest_filter=dest_filter))} |"
            lines.append(row)
        lines.append("")

    if not summary_only:
        for c in active_checks:
            _table(c.title, findings[c.name],
                   dest_col=c.dest_label or None, dest_filter=c.dest_filter)

    if sub_checks and sub_audits:
        lines.append(f"## Activity Log Export ({len(sub_audits)} subscription(s))")
        lines.append("")
        lines.append("| Subscription | Exported | Destinations | Categories | Missing Core |")
        lines.append("|---|---|---|---|---|")
        for s in sub_audits:
            exported = "?" if s.exported is None else ("Yes" if s.exported else "NO")
            dests = "; ".join(f"{d['type']}: {d['name']}" for d in s.destinations) or "-"
            lines.append(
                f"| {_md_escape(s.subscription_name)} | {exported} "
                f"| {_md_escape(dests)} | {_md_escape(', '.join(s.categories) or '-')} "
                f"| {_md_escape(', '.join(s.missing_core) or '-')} |"
            )
        lines.append("")

    if ws_checks and ws_results:
        lines.append(f"## Workspace Usage ({len(ws_results)})")
        lines.append("")
        lookback = ws_results[0].lookback_days
        lines.append(f"| Workspace | Region | Resources Shipping | Sources Seen "
                     f"| Retention | SKU | Ingest GB ({lookback}d) | Queries ({lookback}d) | Status |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for ws in ws_results:
            ingest = "?" if ws.ingest_gb is None else f"{ws.ingest_gb:g}"
            queries = "?" if ws.query_count is None else str(ws.query_count)
            seen = "?" if ws.seen_resources is None else str(ws.seen_resources)
            lines.append(
                f"| {_md_escape(ws.name)} | {_md_escape(ws.region)} "
                f"| {ws.shipping_resources} | {seen} | {ws.retention_days}d "
                f"| {_md_escape(ws.sku)} | {ingest} | {queries} "
                f"| {_md_escape(workspace_status(ws))} |"
            )
        lines.append("")

    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return output


def _short_type(resource_type):
    """Strip the Microsoft. provider prefix for readability."""
    if resource_type.lower().startswith("microsoft."):
        return resource_type[len("microsoft."):]
    return resource_type


def _short_id(resource_id):
    """Return a truncated resource ID for display, keeping the last segment."""
    parts = resource_id.split("/")
    if len(parts) > 4:
        return ".../" + "/".join(parts[-2:])
    return resource_id


def _group_by_sub_rg(items):
    """Group results by subscription, then resource group.

    Returns: [(sub_name, sub_id, [(rg_name, [results])])]
    Subscriptions sorted by name, RGs sorted by name, resources within an RG
    sorted by type then name.
    """
    by_sub = defaultdict(lambda: defaultdict(list))
    sub_ids = {}
    for r in items:
        rg = r.resource_group or "(no resource group)"
        by_sub[r.subscription_name][rg].append(r)
        sub_ids[r.subscription_name] = r.subscription_id

    grouped = []
    for sub_name in sorted(by_sub, key=str.lower):
        rgs = []
        for rg in sorted(by_sub[sub_name], key=str.lower):
            sorted_items = sorted(
                by_sub[sub_name][rg],
                key=lambda r: (r.resource_type.lower(), r.resource_name.lower()),
            )
            rgs.append((rg, sorted_items))
        grouped.append((sub_name, sub_ids[sub_name], rgs))
    return grouped


def _build_dest_index(results):
    """Build a list of unique destinations and the resources streaming to each.

    Returns: list of dicts {type, name, region, id, resources}
    sorted by descending resource count, then type, then name.
    """
    dest_to_results = defaultdict(list)
    dest_meta = {}
    for r in results:
        for d in r.destinations:
            key = (d["type"], d["id"])
            dest_to_results[key].append(r)
            prior = dest_meta.get(key, {})
            dest_meta[key] = {
                "name": d.get("name", ""),
                "region": d.get("region", ""),
                "not_found": prior.get("not_found", False) or d.get("not_found", False),
            }

    items = []
    for (dtype, did), rs in dest_to_results.items():
        items.append({
            "type": dtype,
            "name": dest_meta[(dtype, did)]["name"],
            "region": dest_meta[(dtype, did)]["region"],
            "not_found": dest_meta[(dtype, did)]["not_found"],
            "id": did,
            "resources": rs,
        })
    items.sort(
        key=lambda x: (-len(x["resources"]), x["type"].lower(), x["name"].lower()),
    )
    return items


_CSS = """
:root {
  --text: #0f1419;
  --text-soft: #5b6671;
  --text-faint: #8b95a1;
  --rule: #e1e6eb;
  --rule-soft: #eef1f4;
  --bg: #ffffff;
  --bg-soft: #fafbfc;
  --accent: #b54708;
  --error: #991b1b;
  --error-bg: #fef2f2;
  --healthy: #15803d;
  --duplicate: #b45309;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}

* { box-sizing: border-box; }
html { background: var(--bg); }
body {
  margin: 0;
  font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
  color: var(--text);
}
.report { max-width: 1180px; margin: 0 auto; padding: 36px 28px 64px; }

/* Header */
.hd { padding-bottom: 14px; border-bottom: 1px solid var(--rule); margin-bottom: 24px; }
.hd .doc-class {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 4px;
}
.hd h1 { margin: 0 0 6px; font-size: 18px; font-weight: 600; letter-spacing: -0.01em; }
.hd .meta { font-size: 12.5px; color: var(--text-soft); }
.hd .meta strong { color: var(--text); font-weight: 500; font-variant-numeric: tabular-nums; }
.hd .meta-sep { padding: 0 7px; color: var(--text-faint); }

/* Section blocks: scope + findings overview */
.block { margin-bottom: 24px; }
.block-label {
  font-size: 10.5px;
  font-weight: 600;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 8px;
}
.scope-tbl { font-size: 12.5px; border-collapse: collapse; }
.scope-tbl td { padding: 1px 18px 1px 0; vertical-align: top; }
.scope-name { font-weight: 500; }
.scope-id { font-family: var(--mono); font-size: 11.5px; color: var(--text-faint); }

.overview-tbl {
  font-size: 12.5px;
  border-collapse: collapse;
}
.overview-tbl td {
  padding: 2px 0 2px 0;
  vertical-align: baseline;
}
.overview-tbl tr td:first-child {
  color: var(--text-soft);
  padding-right: 28px;
  min-width: 180px;
}
.overview-tbl .v {
  font-variant-numeric: tabular-nums;
  font-weight: 500;
  text-align: right;
  padding-right: 6px;
  min-width: 44px;
}
.overview-tbl .v.f-warn { color: var(--accent); }
.overview-tbl .v.f-dup { color: var(--duplicate); }
.overview-tbl .v.f-err { color: var(--error); }
.overview-tbl .v.f-ok { color: var(--healthy); }
.overview-tbl .v.f-zero { color: var(--text-faint); font-weight: 400; }
.overview-tbl a {
  color: inherit;
  text-decoration: none;
  border-bottom: 1px dashed transparent;
}
.overview-tbl a:hover {
  color: var(--text);
  border-bottom-color: var(--text-faint);
}

/* Section anchor wrapper */
.section-wrap { position: relative; }
.section-wrap > .anchor {
  position: absolute;
  left: -22px;
  top: 17px;
  font-family: var(--mono);
  font-size: 13px;
  color: var(--text-faint);
  text-decoration: none;
  opacity: 0;
  transition: opacity 0.1s;
  padding: 2px 4px;
  z-index: 1;
}
.section-wrap:hover > .anchor,
.section-wrap > .anchor:focus { opacity: 1; }
.section-wrap > .anchor:hover { color: var(--text); }

.section, .section-empty { scroll-margin-top: 18px; }
.section:target,
.section-empty:target { animation: target-flash 1.4s ease-out; }
@keyframes target-flash {
  0% { background: var(--rule-soft); }
  100% { background: transparent; }
}

/* Finding section */
.section {
  margin: 0;
  padding: 14px 0 6px;
  border-top: 1px solid var(--rule);
}
.section.section-empty {
  color: var(--text-faint);
  display: flex;
  gap: 12px;
  align-items: baseline;
  padding: 14px 0 8px;
}
.section.section-empty .sec-num,
.section.section-empty .sec-title { color: var(--text-faint); font-weight: 500; }
.section.section-empty .sec-count { font-variant-numeric: tabular-nums; }
.section.section-empty .sec-desc { color: var(--text-faint); margin-left: 6px; }

.section > .sec-summary {
  display: flex;
  align-items: baseline;
  gap: 10px;
  cursor: pointer;
  user-select: none;
  list-style: none;
  padding: 4px 0;
}
.section > .sec-summary::-webkit-details-marker { display: none; }
.section > .sec-summary::before {
  content: "\\203A";
  display: inline-block;
  width: 10px;
  color: var(--text-faint);
  font-size: 14px;
  line-height: 1;
  transition: transform 0.1s;
  margin-top: 1px;
  flex-shrink: 0;
}
.section[open] > .sec-summary::before { transform: rotate(90deg); }
.sec-num { color: var(--text-faint); font-variant-numeric: tabular-nums; font-size: 13px; }
.sec-title { font-size: 14px; font-weight: 600; }
.sec-count {
  color: var(--text-soft);
  font-weight: 500;
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}
.section.flag-warn .sec-count { color: var(--accent); }
.section.flag-dup .sec-count { color: var(--duplicate); }
.section.flag-err .sec-count { color: var(--error); }
.section.flag-ok .sec-count { color: var(--healthy); }
.sec-body { padding: 8px 0 6px 22px; }

/* Subscription bar (multi-sub only) */
.sub-bar {
  display: flex;
  gap: 12px;
  align-items: baseline;
  padding: 10px 0 4px;
  font-size: 12.5px;
  border-bottom: 1px solid var(--rule-soft);
}
.sub-bar:first-child { padding-top: 0; }
.sub-name { font-weight: 600; }
.sub-id { font-family: var(--mono); font-size: 11.5px; color: var(--text-faint); }
.sub-count { color: var(--text-soft); font-size: 11.5px; margin-left: auto; font-variant-numeric: tabular-nums; }

/* Resource group */
.rg {
  margin: 0;
  padding: 4px 0;
  border-bottom: 1px solid var(--rule-soft);
}
.rg:last-child { border-bottom: none; }
.rg > .rg-summary {
  display: flex;
  align-items: baseline;
  gap: 10px;
  cursor: pointer;
  user-select: none;
  list-style: none;
  padding: 6px 0;
}
.rg > .rg-summary::-webkit-details-marker { display: none; }
.rg > .rg-summary::before {
  content: "\\203A";
  display: inline-block;
  width: 10px;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1;
  transition: transform 0.1s;
  flex-shrink: 0;
}
.rg[open] > .rg-summary::before { transform: rotate(90deg); }
.rg-name { font-family: var(--mono); font-size: 12px; font-weight: 500; }
.rg-count { color: var(--text-soft); font-size: 11.5px; font-variant-numeric: tabular-nums; }
.rg-body { padding: 0 0 8px 22px; }

/* Resource rows */
.row-head, .row {
  display: grid;
  gap: 14px;
  padding: 5px 0 5px 18px;
  font-size: 12.5px;
  align-items: baseline;
}
.row-cols-3 { grid-template-columns: minmax(170px, 240px) minmax(150px, 1fr) 90px; }
.row-cols-4 { grid-template-columns: minmax(170px, 240px) minmax(150px, 1fr) 90px minmax(180px, 1.4fr); }
.row-cols-dest { grid-template-columns: minmax(160px, 1.6fr) minmax(140px, 1fr) minmax(120px, 1fr) minmax(120px, 1fr) 90px; }
.row-head {
  font-size: 10.5px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding-top: 2px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--rule-soft);
  font-weight: 600;
}
.row {
  cursor: pointer;
  list-style: none;
  border-bottom: 1px solid var(--rule-soft);
  position: relative;
}
.row::-webkit-details-marker { display: none; }
.row::before {
  content: "\\203A";
  position: absolute;
  left: 4px;
  top: 5px;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1;
  transition: transform 0.1s;
}
.resource[open] > .row::before { transform: rotate(90deg); }
.resource[open] > .row { background: var(--bg-soft); }
.rc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.rc.rc-type {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-soft);
}
.rc.rc-name { font-weight: 500; }
.rc.rc-region {
  color: var(--text-soft);
  font-family: var(--mono);
  font-size: 11.5px;
}
.rc.rc-detail { color: var(--text-soft); white-space: normal; }

/* Row expand */
.row-expand {
  padding: 10px 0 14px 22px;
  background: var(--bg-soft);
  border-bottom: 1px solid var(--rule-soft);
  font-size: 12px;
}
.full-id {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-soft);
  word-break: break-all;
  margin-bottom: 8px;
  padding-right: 22px;
}
.empty-state {
  padding: 4px 0;
  font-size: 12px;
  color: var(--text-soft);
  font-style: italic;
}
.err-msg {
  padding: 7px 10px;
  font-size: 12px;
  color: var(--error);
  background: var(--error-bg);
  border-left: 2px solid var(--error);
  margin: 6px 22px 6px 0;
  font-family: var(--mono);
  word-break: break-word;
}

/* Settings table */
.settings {
  width: calc(100% - 22px);
  border-collapse: collapse;
  margin-top: 8px;
  font-size: 11.5px;
}
.settings th, .settings td {
  text-align: left;
  padding: 5px 12px 5px 0;
  border-bottom: 1px solid var(--rule-soft);
  vertical-align: top;
}
.settings th {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border-bottom: 1px solid var(--rule);
}
.settings tr:last-child td { border-bottom: none; }
.settings .id-cell {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-soft);
  word-break: break-all;
  max-width: 280px;
  cursor: help;
}
.settings .name-cell { font-weight: 500; }
.settings .cats-cell { font-size: 11.5px; max-width: 320px; }
.no-cats { color: var(--error); font-style: italic; }
.la-tag {
  display: inline-block;
  margin-left: 5px;
  padding: 0 5px;
  background: var(--rule-soft);
  color: var(--text-soft);
  font-size: 10px;
  border-radius: 2px;
  font-weight: 500;
}
.la-tag.tag-warn { background: #fdf1e5; color: var(--accent); }

/* Inline status text */
.t-error { color: var(--error); font-weight: 500; }
.t-info { color: var(--text-soft); }
.dim { color: var(--text-faint); }

/* Destination map */
.dest {
  border-bottom: 1px solid var(--rule-soft);
  padding: 4px 0;
}
.dest:last-child { border-bottom: none; }
.dest > .dest-summary {
  display: flex;
  align-items: baseline;
  gap: 12px;
  cursor: pointer;
  user-select: none;
  list-style: none;
  padding: 6px 0 6px 18px;
  position: relative;
}
.dest > .dest-summary::-webkit-details-marker { display: none; }
.dest > .dest-summary::before {
  content: "\\203A";
  position: absolute;
  left: 4px;
  top: 7px;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1;
  transition: transform 0.1s;
}
.dest[open] > .dest-summary::before { transform: rotate(90deg); }
.dest-type {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-soft);
  font-weight: 500;
  min-width: 130px;
}
.dest-name { font-weight: 500; }
.dest-region { color: var(--text-faint); font-family: var(--mono); font-size: 11.5px; }
.dest-count { color: var(--text-soft); margin-left: auto; font-size: 11.5px; font-variant-numeric: tabular-nums; }
.dest-body { padding: 4px 0 12px 22px; }

/* Footer */
.footer {
  margin-top: 36px;
  padding-top: 14px;
  border-top: 1px solid var(--rule);
  font-size: 11px;
  color: var(--text-faint);
  display: flex;
  justify-content: space-between;
  font-family: var(--mono);
}

/* Print */
@media print {
  body { font-size: 11px; color: #000; }
  .report { padding: 12px 0 24px; max-width: none; }
  .row, .rg, .section, .dest { break-inside: avoid; }
}

/* Narrow */
@media (max-width: 800px) {
  .report { padding: 22px 14px 40px; }
  .row-cols-3, .row-cols-4, .row-cols-dest { grid-template-columns: 1fr; gap: 2px; }
  .row-head { display: none; }
  .row { padding-bottom: 8px; }
  .full-id { padding-right: 0; }
  .section-wrap > .anchor { display: none; }
}
"""


def generate_html(results, output, summary_only=False, checks=None, ws_results=None,
                  sub_audits=None):
    """Write results to a self-contained HTML report."""
    e = html.escape
    status_counts = Counter(r.status for r in results)
    total = len(results)
    notsup_count = status_counts.get("Not Supported", 0)
    error_count = status_counts.get("Error", 0)
    info_count = notsup_count + error_count

    active_checks = get_checks(checks, scope="resource")
    ws_checks = get_checks(checks, scope="workspace")
    sub_checks = get_checks(checks, scope="subscription")
    ws_results = ws_results or []
    sub_audits = sub_audits or []
    findings = {c.name: [r for r in results if c.detect(r)] for c in active_checks}
    healthy_count = sum(1 for r in results if is_healthy(r, checks))

    subs = sorted(
        {(r.subscription_name, r.subscription_id) for r in results},
        key=lambda x: x[0].lower(),
    )
    multi_sub = len(subs) > 1
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    healthy_grouped = _group_by_sub_rg([r for r in results if is_healthy(r, checks)])
    info_grouped = _group_by_sub_rg([
        r for r in results if r.status in ("Not Supported", "Error")
    ])
    dest_index = _build_dest_index(results)

    def _fmt_count(n, kind="default"):
        if n == 0:
            return '<td class="v f-zero">0</td>'
        cls_map = {"warn": "f-warn", "dup": "f-dup", "err": "f-err", "ok": "f-ok"}
        cls = cls_map.get(kind)
        if cls:
            return f'<td class="v {cls}">{n}</td>'
        return f'<td class="v">{n}</td>'

    def _build_settings_table(destinations):
        if not destinations:
            return ""
        rows = []
        for d in destinations:
            cats = d.get("log_categories", [])
            cats_html = (
                ", ".join(e(c) for c in cats)
                if cats
                else '<span class="no-cats">none</span>'
            )
            la_type = d.get("la_destination_type", "")
            la_html = f'<span class="la-tag">{e(la_type)}</span>' if la_type else ""
            did = d.get("id", "")
            if d.get("not_found"):
                region_html = '<span class="t-error">not found</span>'
            else:
                region_html = e(d.get("region", ""))
                if d.get("cross_region"):
                    region_html += ' <span class="la-tag tag-warn">cross-region</span>'
            rows.append(
                "<tr>"
                f'<td class="name-cell">{e(d.get("setting_name", ""))}</td>'
                f"<td>{e(d.get('type', ''))}{la_html}</td>"
                f'<td class="name-cell">{e(d.get("name", ""))}</td>'
                f"<td>{region_html}</td>"
                f'<td class="id-cell" title="{e(did)}">{e(_short_id(did))}</td>'
                f'<td class="cats-cell">{cats_html}</td>'
                "</tr>"
            )
        return (
            '<table class="settings">'
            "<thead><tr>"
            "<th>Setting</th><th>Type</th><th>Name</th>"
            "<th>Region</th><th>ID</th><th>Categories</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    def _render_destinations_inline(destinations):
        if not destinations:
            return '<span class="dim">&mdash;</span>'
        type_to_count = defaultdict(int)
        type_to_first_name = {}
        for d in destinations:
            type_to_count[d["type"]] += 1
            type_to_first_name.setdefault(
                d["type"],
                d.get("name") or _short_id(d.get("id", "")),
            )
        parts = []
        for dtype, count in type_to_count.items():
            if count > 1:
                parts.append(f'{e(dtype)} <span class="dim">&times;{count}</span>')
            else:
                name = type_to_first_name[dtype]
                parts.append(f"{e(dtype)}: {e(name)}")
        return " &middot; ".join(parts)

    def _build_resource_row(r, kind):
        rtype = e(_short_type(r.resource_type))
        rname = e(r.resource_name)
        rregion = e(r.resource_location)
        full_type_attr = e(r.resource_type)

        if kind == "missing":
            cols_html = (
                f'<span class="rc rc-type" title="{full_type_attr}">{rtype}</span>'
                f'<span class="rc rc-name">{rname}</span>'
                f'<span class="rc rc-region">{rregion}</span>'
            )
            cols_class = "row-cols-3"
        elif kind in ("duplicate", "healthy"):
            cols_html = (
                f'<span class="rc rc-type" title="{full_type_attr}">{rtype}</span>'
                f'<span class="rc rc-name">{rname}</span>'
                f'<span class="rc rc-region">{rregion}</span>'
                f'<span class="rc rc-detail">{_render_destinations_inline(r.destinations)}</span>'
            )
            cols_class = "row-cols-4"
        else:  # info
            if r.status == "Error":
                detail_html = '<span class="t-error">Error</span>'
            else:
                detail_html = '<span class="t-info">Not supported</span>'
            cols_html = (
                f'<span class="rc rc-type" title="{full_type_attr}">{rtype}</span>'
                f'<span class="rc rc-name">{rname}</span>'
                f'<span class="rc rc-region">{rregion}</span>'
                f'<span class="rc rc-detail">{detail_html}</span>'
            )
            cols_class = "row-cols-4"

        expand_parts = [f'<div class="full-id">{e(r.resource_id)}</div>']
        if r.error_message:
            expand_parts.append(f'<div class="err-msg">{e(r.error_message)}</div>')
        if r.destinations:
            expand_parts.append(_build_settings_table(r.destinations))
        elif r.status == "Missing":
            expand_parts.append(
                '<div class="empty-state">No diagnostic settings configured.</div>'
            )
        elif r.status == "Not Supported":
            expand_parts.append(
                '<div class="empty-state">'
                "This resource type does not support diagnostic settings."
                "</div>"
            )

        return (
            '<details class="resource">'
            f'<summary class="row {cols_class}">{cols_html}</summary>'
            f'<div class="row-expand">{"".join(expand_parts)}</div>'
            "</details>"
        )

    def _build_rg_block(rg, items, kind):
        if kind == "missing":
            head_html = (
                '<div class="row-head row-cols-3">'
                "<span>Type</span><span>Name</span><span>Region</span>"
                "</div>"
            )
        elif kind in ("duplicate", "healthy"):
            head_html = (
                '<div class="row-head row-cols-4">'
                "<span>Type</span><span>Name</span><span>Region</span><span>Destinations</span>"
                "</div>"
            )
        else:
            head_html = (
                '<div class="row-head row-cols-4">'
                "<span>Type</span><span>Name</span><span>Region</span><span>Status</span>"
                "</div>"
            )

        rows_html = "".join(_build_resource_row(r, kind) for r in items)
        return (
            '<details class="rg" open>'
            '<summary class="rg-summary">'
            f'<span class="rg-name">{e(rg)}</span>'
            f'<span class="rg-count">{len(items)}</span>'
            "</summary>"
            f'<div class="rg-body">{head_html}{rows_html}</div>'
            "</details>"
        )

    def _build_section(num, title, count, grouped, *, kind, default_open, severity, anchor,
                       omit_details=False):
        flag_class = f" flag-{severity}" if severity else ""
        anchor_link = f'<a class="anchor" href="#{anchor}" aria-label="link to {e(title)}">#</a>'
        if count == 0 or omit_details:
            desc = "none" if count == 0 else "details omitted (summary-only)"
            return (
                '<div class="section-wrap">'
                f"{anchor_link}"
                f'<section class="section section-empty{flag_class}" id="{anchor}">'
                f'<span class="sec-num">{num}.</span>'
                f'<span class="sec-title">{e(title)}</span>'
                f'<span class="sec-count">{count}</span>'
                f'<span class="sec-desc">{desc}</span>'
                "</section>"
                "</div>"
            )

        open_attr = " open" if default_open else ""
        body_parts = []
        for sub_name, sub_id, rgs in grouped:
            if multi_sub:
                sub_total = sum(len(items) for _, items in rgs)
                body_parts.append(
                    '<div class="sub-bar">'
                    f'<span class="sub-name">{e(sub_name)}</span>'
                    f'<span class="sub-id">{e(sub_id)}</span>'
                    f'<span class="sub-count">{sub_total}</span>'
                    "</div>"
                )
            for rg, items in rgs:
                body_parts.append(_build_rg_block(rg, items, kind))

        return (
            '<div class="section-wrap">'
            f"{anchor_link}"
            f'<details class="section{flag_class}"{open_attr} id="{anchor}">'
            '<summary class="sec-summary">'
            f'<span class="sec-num">{num}.</span>'
            f'<span class="sec-title">{e(title)}</span>'
            f'<span class="sec-count">{count}</span>'
            "</summary>"
            f'<div class="sec-body">{"".join(body_parts)}</div>'
            "</details>"
            "</div>"
        )

    def _build_dest_section(num, anchor):
        anchor_link = f'<a class="anchor" href="#{anchor}" aria-label="link to Destination Map">#</a>'
        if not dest_index:
            return (
                '<div class="section-wrap">'
                f"{anchor_link}"
                f'<section class="section section-empty" id="{anchor}">'
                f'<span class="sec-num">{num}.</span>'
                '<span class="sec-title">Destination Map</span>'
                '<span class="sec-count">0</span>'
                '<span class="sec-desc">no destinations</span>'
                "</section>"
                "</div>"
            )

        items_html = []
        for d in dest_index:
            display_name = d["name"] or _short_id(d["id"])
            if d.get("not_found"):
                region_html = '<span class="t-error">not found</span>'
            else:
                region_html = (
                    f'<span class="dest-region">{e(d["region"])}</span>'
                    if d["region"] else ""
                )
            resources_sorted = sorted(
                d["resources"],
                key=lambda r: (
                    r.subscription_name.lower(),
                    r.resource_group.lower(),
                    r.resource_type.lower(),
                    r.resource_name.lower(),
                ),
            )
            res_rows = "".join(
                '<div class="row row-cols-dest">'
                f'<span class="rc rc-name">{e(r.resource_name)}</span>'
                f'<span class="rc rc-type" title="{e(r.resource_type)}">{e(_short_type(r.resource_type))}</span>'
                f'<span class="rc">{e(r.resource_group)}</span>'
                f'<span class="rc">{e(r.subscription_name)}</span>'
                f'<span class="rc rc-region">{e(r.resource_location)}</span>'
                "</div>"
                for r in resources_sorted
            )
            items_html.append(
                '<details class="dest">'
                '<summary class="dest-summary">'
                f'<span class="dest-type">{e(d["type"])}</span>'
                f'<span class="dest-name">{e(display_name)}</span>'
                f"{region_html}"
                f'<span class="dest-count">{len(d["resources"])} resources</span>'
                "</summary>"
                '<div class="dest-body">'
                f'<div class="full-id">{e(d["id"])}</div>'
                '<div class="row-head row-cols-dest">'
                "<span>Resource</span><span>Type</span><span>Resource Group</span>"
                "<span>Subscription</span><span>Region</span>"
                "</div>"
                f"{res_rows}"
                "</div></details>"
            )

        return (
            '<div class="section-wrap">'
            f"{anchor_link}"
            f'<details class="section" id="{anchor}">'
            '<summary class="sec-summary">'
            f'<span class="sec-num">{num}.</span>'
            '<span class="sec-title">Destination Map</span>'
            f'<span class="sec-count">{len(dest_index)}</span>'
            "</summary>"
            f'<div class="sec-body">{"".join(items_html)}</div>'
            "</details>"
            "</div>"
        )

    scope_rows = "".join(
        f'<tr><td class="scope-name">{e(name)}</td>'
        f'<td class="scope-id">{e(sid)}</td></tr>'
        for name, sid in subs
    )

    overview_parts = []
    for c in active_checks:
        overview_parts.append(
            f'<tr><td><a href="#{c.anchor}">{e(c.title)}</a></td>'
            f"{_fmt_count(len(findings[c.name]), c.severity)}</tr>"
        )
    for c in ws_checks:
        ws_count = sum(1 for ws in ws_results if c.detect(ws))
        overview_parts.append(
            f'<tr><td><a href="#{c.anchor}">{e(c.title)}</a></td>'
            f"{_fmt_count(ws_count, c.severity)}</tr>"
        )
    for c in sub_checks:
        sub_count = sum(1 for s in sub_audits if c.detect(s))
        overview_parts.append(
            f'<tr><td><a href="#{c.anchor}">{e(c.title)}</a></td>'
            f"{_fmt_count(sub_count, c.severity)}</tr>"
        )
    overview_parts.append(
        f'<tr><td><a href="#healthy">Healthy</a></td>{_fmt_count(healthy_count, "ok")}</tr>'
        f'<tr><td><a href="#informational">Not supported</a></td>{_fmt_count(notsup_count)}</tr>'
        f'<tr><td><a href="#informational">Errors</a></td>{_fmt_count(error_count, "err")}</tr>'
    )
    overview_rows = "".join(overview_parts)

    sections = []
    num = 0
    for c in active_checks:
        num += 1
        count = len(findings[c.name])
        sections.append(_build_section(
            num, c.title, count, _group_by_sub_rg(findings[c.name]),
            kind=c.row_kind, default_open=c.default_open and count > 0,
            severity=c.severity if count > 0 else None,
            anchor=c.anchor,
        ))
    sections.append(_build_section(
        num + 1, "Healthy Resources", healthy_count, healthy_grouped,
        kind="healthy", default_open=False,
        severity="ok" if healthy_count > 0 else None,
        anchor="healthy", omit_details=summary_only,
    ))
    sections.append(_build_section(
        num + 2, "Informational", info_count, info_grouped,
        kind="info", default_open=False,
        severity="err" if error_count > 0 else None,
        anchor="informational", omit_details=summary_only,
    ))
    num += 2

    if sub_checks and sub_audits:
        num += 1
        flagged_subs = sum(1 for s in sub_audits if s.exported is False)
        sub_rows = []
        for s in sub_audits:
            if s.exported is None:
                exported_html = f'<span class="dim">unknown{": " + e(s.error) if s.error else ""}</span>'
            elif s.exported:
                exported_html = "Yes"
            else:
                exported_html = '<span class="t-error">NO</span>'
            dests = "; ".join(
                f"{e(d['type'])}: {e(d['name'])}" for d in s.destinations) or "&mdash;"
            sub_rows.append(
                "<tr>"
                f'<td class="name-cell">{e(s.subscription_name)}</td>'
                f'<td class="id-cell">{e(s.subscription_id)}</td>'
                f"<td>{exported_html}</td>"
                f"<td>{dests}</td>"
                f"<td>{e(', '.join(s.categories)) or '&mdash;'}</td>"
                f"<td>{e(', '.join(s.missing_core)) or '&mdash;'}</td>"
                "</tr>"
            )
        sub_table = (
            '<table class="settings">'
            "<thead><tr>"
            "<th>Subscription</th><th>ID</th><th>Exported</th>"
            "<th>Destinations</th><th>Categories</th><th>Missing Core</th>"
            "</tr></thead><tbody>" + "".join(sub_rows) + "</tbody></table>"
        )
        severity_class = " flag-err" if flagged_subs else ""
        open_attr = " open" if flagged_subs else ""
        sections.append(
            '<div class="section-wrap">'
            '<a class="anchor" href="#activity-log" aria-label="link to Activity Log Export">#</a>'
            f'<details class="section{severity_class}"{open_attr} id="activity-log">'
            '<summary class="sec-summary">'
            f'<span class="sec-num">{num}.</span>'
            '<span class="sec-title">Activity Log Export</span>'
            f'<span class="sec-count">{flagged_subs} of {len(sub_audits)} not exported</span>'
            "</summary>"
            f'<div class="sec-body">{sub_table}</div>'
            "</details></div>"
        )

    if ws_checks:
        num += 1
        flagged = sum(
            1 for ws in ws_results if any(c.detect(ws) for c in ws_checks))
        lookback = ws_results[0].lookback_days if ws_results else 30
        if not ws_results:
            sections.append(
                '<div class="section-wrap">'
                '<a class="anchor" href="#workspace-usage" aria-label="link to Workspace Usage">#</a>'
                f'<section class="section section-empty" id="workspace-usage">'
                f'<span class="sec-num">{num}.</span>'
                '<span class="sec-title">Workspace Usage</span>'
                '<span class="sec-count">0</span>'
                '<span class="sec-desc">no Log Analytics destinations</span>'
                "</section></div>"
            )
        else:
            ws_rows = []
            for ws in ws_results:
                flagged_here = any(c.detect(ws) for c in ws_checks)
                status_html = e(workspace_status(ws))
                if flagged_here:
                    status_html = f'<span class="t-error">{status_html}</span>'
                ingest = "?" if ws.ingest_gb is None else f"{ws.ingest_gb:g}"
                queries = "?" if ws.query_count is None else str(ws.query_count)
                seen = "?" if ws.seen_resources is None else str(ws.seen_resources)
                ws_rows.append(
                    "<tr>"
                    f'<td class="name-cell" title="{e(ws.workspace_id)}">{e(ws.name)}</td>'
                    f"<td>{e(ws.region)}</td>"
                    f"<td>{ws.shipping_resources}</td>"
                    f"<td>{seen}</td>"
                    f"<td>{ws.retention_days}d</td>"
                    f"<td>{e(ws.sku)}</td>"
                    f"<td>{ingest}</td>"
                    f"<td>{queries}</td>"
                    f"<td>{status_html}</td>"
                    "</tr>"
                )
            ws_table = (
                '<table class="settings">'
                "<thead><tr>"
                "<th>Workspace</th><th>Region</th><th>Resources</th>"
                f"<th>Sources Seen</th>"
                f"<th>Retention</th><th>SKU</th><th>Ingest GB ({lookback}d)</th>"
                f"<th>Queries ({lookback}d)</th><th>Status</th>"
                "</tr></thead><tbody>" + "".join(ws_rows) + "</tbody></table>"
            )
            severity_class = " flag-warn" if flagged else ""
            open_attr = " open" if flagged else ""
            sections.append(
                '<div class="section-wrap">'
                '<a class="anchor" href="#workspace-usage" aria-label="link to Workspace Usage">#</a>'
                f'<details class="section{severity_class}"{open_attr} id="workspace-usage">'
                '<summary class="sec-summary">'
                f'<span class="sec-num">{num}.</span>'
                '<span class="sec-title">Workspace Usage</span>'
                f'<span class="sec-count">{len(ws_results)}</span>'
                "</summary>"
                f'<div class="sec-body">{ws_table}</div>'
                "</details></div>"
            )

    sections.append(_build_dest_section(num + 1, anchor="destinations"))
    sections_html = "\n".join(sections)

    sub_label = "subscription" if len(subs) == 1 else "subscriptions"
    res_label = "resource" if total == 1 else "resources"

    # Embed the machine-readable payload so any HTML report can later be
    # re-parsed or diffed without re-scanning. "</" is escaped so result
    # content can never terminate the script block early.
    embedded_json = json.dumps(
        build_payload(results, ws_results=ws_results if ws_checks else None,
                      sub_audits=sub_audits if sub_checks else None),
        sort_keys=True,
    ).replace("</", "<\\/")

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Diagnostic Settings Audit &middot; DudeWheresMyLogs</title>
<style>{_CSS}</style>
</head>
<body>
<div class="report">

<header class="hd">
  <div class="doc-class">DudeWheresMyLogs &middot; audit report</div>
  <h1>Azure Diagnostic Settings Audit</h1>
  <div class="meta">
    Generated <strong>{timestamp}</strong>
    <span class="meta-sep">&middot;</span>
    <strong>{len(subs)}</strong> {sub_label}
    <span class="meta-sep">&middot;</span>
    <strong>{total}</strong> {res_label} evaluated
  </div>
</header>

<section class="block">
  <div class="block-label">Scope</div>
  <table class="scope-tbl"><tbody>{scope_rows}</tbody></table>
</section>

<section class="block">
  <div class="block-label">Findings overview</div>
  <table class="overview-tbl"><tbody>{overview_rows}</tbody></table>
</section>

{sections_html}

<footer class="footer">
  <span>DudeWheresMyLogs &middot; Azure Diagnostic Logging Audit</span>
  <span>{timestamp}</span>
</footer>

</div>
<script type="application/json" id="dwml-data">{embedded_json}</script>
</body>
</html>
"""

    with open(output, "w", encoding="utf-8") as f:
        f.write(body)
    return output
