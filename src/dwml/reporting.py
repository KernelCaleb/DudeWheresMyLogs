import csv
import html
from collections import Counter
from datetime import datetime


def generate_report(results, fmt="html", output=None):
    """Generate a report in the specified format.

    Args:
        results: list of DiagnosticResult objects
        fmt: "html" or "csv"
        output: output file path (auto-generated if None)

    Returns:
        The output file path.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "csv":
        if output is None:
            output = f"DudeWheresMyLogs_{timestamp}.csv"
        return generate_csv(results, output)
    else:
        if output is None:
            output = f"DudeWheresMyLogs_{timestamp}.html"
        return generate_html(results, output)


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
        "Error",
    ]

    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            dest_str = "; ".join(
                f"[{d.get('setting_name', '')}] {d['type']}: "
                f"{d.get('name', '')} ({d.get('region', '')}) ({d['id']})"
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
                "Error": r.error_message,
            })

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


def _group_by_sub_and_type(items):
    """Group results by subscription name, then by resource type.

    Returns: [(sub_name, sub_id, [(type, [results])])]
    Sorted by subscription name, then type.
    """
    from collections import defaultdict

    by_sub = defaultdict(lambda: defaultdict(list))
    sub_ids = {}
    for r in items:
        by_sub[r.subscription_name][r.resource_type].append(r)
        sub_ids[r.subscription_name] = r.subscription_id

    grouped = []
    for sub_name in sorted(by_sub):
        types = []
        for rtype in sorted(by_sub[sub_name]):
            types.append((rtype, by_sub[sub_name][rtype]))
        grouped.append((sub_name, sub_ids[sub_name], types))
    return grouped


def _build_dest_map(results):
    """Build a reverse map: destination -> resources streaming to it.

    Returns: [(dest_type, dest_name, dest_id, [(sub_name, sub_id, [(type, [results])])])]
    Sorted by destination type then name.
    """
    from collections import defaultdict

    dest_to_results = defaultdict(list)
    dest_names = {}
    for r in results:
        for d in r.destinations:
            key = (d["type"], d["id"])
            dest_to_results[key].append(r)
            if d.get("name"):
                dest_names[key] = d["name"]

    dest_map = []
    for (dtype, did) in sorted(dest_to_results):
        dname = dest_names.get((dtype, did), "")
        grouped = _group_by_sub_and_type(dest_to_results[(dtype, did)])
        dest_map.append((dtype, dname, did, grouped))
    return dest_map


def generate_html(results, output):
    """Write results to a self-contained HTML report."""
    e = html.escape
    status_counts = Counter(r.status for r in results)
    total = len(results)
    dup_count = sum(1 for r in results if r.duplicate)
    subs = sorted({(r.subscription_name, r.subscription_id) for r in results})
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Split results into buckets
    missing = [r for r in results if r.status == "Missing"]
    duplicates = [r for r in results if r.duplicate]
    enabled = [r for r in results if r.status == "Enabled" and not r.duplicate]
    informational = [r for r in results if r.status in ("Not Supported", "Error")]

    # Group each bucket
    missing_grouped = _group_by_sub_and_type(missing)
    dup_grouped = _group_by_sub_and_type(duplicates)
    enabled_grouped = _group_by_sub_and_type(enabled)
    info_grouped = _group_by_sub_and_type(informational)

    # Destination map
    dest_map = _build_dest_map(results)

    def _resource_detail(r):
        """Build a key-value detail grid for a resource."""
        return (
            '<div class="res-detail">'
            "<table>"
            f"<tr><td class='detail-key'>Resource Name</td><td>{e(r.resource_name)}</td></tr>"
            f"<tr><td class='detail-key'>Resource Group</td><td>{e(r.resource_group)}</td></tr>"
            f"<tr><td class='detail-key'>Resource Type</td><td>{e(_short_type(r.resource_type))}</td></tr>"
            f"<tr><td class='detail-key'>Region</td><td>{e(r.resource_location)}</td></tr>"
            f'<tr><td class="detail-key">Resource ID</td>'
            f'<td class="res-id-full">{e(r.resource_id)}</td></tr>'
            "</table></div>"
        )

    def _settings_table(destinations):
        """Build the diagnostic settings table for a resource."""
        if not destinations:
            return ""
        rows = []
        for d in destinations:
            cats = d.get("log_categories", [])
            cats_str = ", ".join(cats) if cats else '<span class="no-cats">None</span>'
            la_type = e(d.get("la_destination_type", ""))
            dtype = e(d.get("type", ""))
            type_display = f'{dtype} <span class="la-type">({la_type})</span>' if la_type else dtype
            region = e(d.get("region", ""))
            did = d.get("id", "")
            rows.append(
                "<tr>"
                f"<td>{e(d.get('setting_name', ''))}</td>"
                f"<td>{type_display}</td>"
                f"<td>{e(d.get('name', ''))}</td>"
                f"<td>{region}</td>"
                f'<td class="res-id" title="{e(did)}">{e(_short_id(did))}</td>'
                f'<td class="cats-cell">{cats_str}</td>'
                "</tr>"
            )
        return (
            '<div class="table-wrap"><table class="settings-table">'
            "<thead><tr>"
            "<th>Setting Name</th><th>Destination Type</th>"
            "<th>Destination Name</th><th>Destination Region</th>"
            "<th>Destination ID</th><th>Log Categories</th>"
            "</tr></thead><tbody>\n"
            + "\n".join(rows)
            + "\n</tbody></table></div>"
        )

    def _resource_block(r, open_by_default=False):
        """Build a collapsible block for a single resource."""
        open_attr = " open" if open_by_default else ""
        setting_count = len(r.destinations)
        label = f"{setting_count} setting{'s' if setting_count != 1 else ''}" if setting_count else "no settings"
        block = (
            f"<details class='resource-block'{open_attr}>\n"
            f'<summary class="resource-header">'
            f'{e(r.resource_name)}'
            f'<span class="res-meta">{e(r.resource_group)} | {e(r.resource_location)}</span>'
            f'<span class="badge-sm">{label}</span>'
            f"</summary>\n"
            f"{_resource_detail(r)}\n"
        )
        if r.destinations:
            block += _settings_table(r.destinations)
        if r.error_message:
            block += f'<div class="error-msg">{e(r.error_message)}</div>'
        block += "\n</details>\n"
        return block

    def build_section(title, badge_count, accent_color, grouped,
                      open_by_default=True, show_settings=True):
        """Build a collapsible section with sub/type grouping and per-resource blocks."""
        if not grouped:
            return ""

        open_attr = " open" if open_by_default else ""
        section_html = (
            f'<section class="report-section" style="border-left: 4px solid {accent_color};">\n'
            f"<details{open_attr}>\n"
            f'<summary class="section-header">'
            f"{e(title)}"
            f'<span class="badge" style="background: {accent_color};">{badge_count}</span>'
            f"</summary>\n"
            f'<div class="section-body">\n'
        )

        for sub_name, sub_id, types in grouped:
            sub_count = sum(len(items) for _, items in types)
            section_html += (
                f"<details{open_attr}>\n"
                f'<summary class="sub-header">'
                f"{e(sub_name)}"
                f'<span class="sub-id">{e(sub_id)}</span>'
                f'<span class="badge-sm">{sub_count}</span>'
                f"</summary>\n"
                f'<div class="sub-body">\n'
            )

            for rtype, items in types:
                section_html += (
                    f"<details{open_attr}>\n"
                    f'<summary class="type-header">'
                    f"{e(_short_type(rtype))}"
                    f'<span class="badge-sm">{len(items)}</span>'
                    f"</summary>\n"
                    f'<div class="type-body">\n'
                )
                for r in items:
                    section_html += _resource_block(r, open_by_default=open_by_default)
                section_html += "</div>\n</details>\n"

            section_html += "</div>\n</details>\n"

        section_html += "</div>\n</details>\n</section>\n"
        return section_html

    def build_dest_map_section():
        if not dest_map:
            return ""

        section_html = (
            '<section class="report-section" style="border-left: 4px solid #2980b9;">\n'
            "<details>\n"
            '<summary class="section-header">'
            "Destination Map"
            f'<span class="badge" style="background: #2980b9;">{len(dest_map)}</span>'
            "</summary>\n"
            '<div class="section-body">\n'
        )

        for dtype, dname, did, grouped in dest_map:
            total_resources = sum(
                len(items) for _, _, types in grouped for _, items in types
            )
            display_name = dname if dname else _short_id(did)
            # Look up region from any result that has this destination
            dest_region = ""
            for _, _, types in grouped:
                for _, items in types:
                    for r in items:
                        for d in r.destinations:
                            if d.get("id") == did and d.get("region"):
                                dest_region = d["region"]
                                break
                        if dest_region:
                            break
                    if dest_region:
                        break
                if dest_region:
                    break

            region_label = f' <span class="res-meta">({dest_region})</span>' if dest_region else ""
            section_html += (
                f"<details>\n"
                f'<summary class="sub-header">'
                f'{e(dtype)}: <span class="dest-name">{e(display_name)}</span>'
                f'{region_label}'
                f'<span class="badge-sm">{total_resources} resources</span>'
                f"</summary>\n"
                f'<div class="dest-full-id">{e(did)}</div>\n'
                f'<div class="sub-body">\n'
            )

            for sub_name, sub_id, types in grouped:
                sub_count = sum(len(items) for _, items in types)
                section_html += (
                    f"<details>\n"
                    f'<summary class="type-header">'
                    f"{e(sub_name)}"
                    f'<span class="badge-sm">{sub_count}</span>'
                    f"</summary>\n"
                )

                for rtype, items in types:
                    section_html += (
                        f'<div class="dest-type-group">'
                        f'<div class="dest-type-label">{e(_short_type(rtype))}'
                        f'<span class="badge-sm">{len(items)}</span></div>\n'
                        f'<div class="table-wrap"><table>\n'
                        f"<thead><tr>"
                        f"<th>Resource Name</th><th>Resource Group</th>"
                        f"<th>Region</th><th>Resource ID</th>"
                        f"</tr></thead>\n<tbody>\n"
                    )
                    for r in items:
                        section_html += (
                            f"<tr><td>{e(r.resource_name)}</td>"
                            f"<td>{e(r.resource_group)}</td>"
                            f"<td>{e(r.resource_location)}</td>"
                            f'<td class="res-id" title="{e(r.resource_id)}">'
                            f"{e(_short_id(r.resource_id))}</td></tr>\n"
                        )
                    section_html += "</tbody></table></div></div>\n"

                section_html += "</details>\n"

            section_html += "</div>\n</details>\n"

        section_html += "</div>\n</details>\n</section>\n"
        return section_html

    # Subscriptions scanned
    sub_rows = ""
    for name, sid in subs:
        sub_rows += f"<tr><td>{e(name)}</td><td>{e(sid)}</td></tr>\n"

    missing_section = build_section(
        "Action Required: Missing Diagnostics",
        len(missing), "#c0392b", missing_grouped,
        open_by_default=True,
    )
    dup_section = build_section(
        "Action Required: Duplicate Shipping",
        len(duplicates), "#e67e22", dup_grouped,
        open_by_default=True,
    )
    enabled_section = build_section(
        "Healthy: Enabled Diagnostics",
        len(enabled), "#27ae60", enabled_grouped,
        open_by_default=False,
    )
    info_section = build_section(
        "Informational: Not Supported / Errors",
        len(informational), "#95a5a6", info_grouped,
        open_by_default=False,
    )
    dest_section = build_dest_map_section()

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DudeWheresMyLogs Report</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f0f2f5; color: #333; padding: 0; }}
.report-header {{ background: #1a1a2e; color: #fff; padding: 24px 32px; }}
.report-header h1 {{ font-size: 1.5em; font-weight: 600; margin-bottom: 4px; }}
.report-header .subtitle {{ color: #a0a0b8; font-size: 0.85em; }}
.report-body {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}

.cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 28px; }}
.card {{ background: #fff; border-radius: 8px; padding: 16px 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); min-width: 140px; border-left: 4px solid #ddd; }}
.card .num {{ font-size: 2em; font-weight: 700; line-height: 1.1; }}
.card .label {{ color: #666; font-size: 0.85em; margin-top: 2px; }}
.card.missing {{ border-left-color: #c0392b; }}
.card.missing .num {{ color: #c0392b; }}
.card.dup {{ border-left-color: #e67e22; }}
.card.dup .num {{ color: #e67e22; }}
.card.enabled {{ border-left-color: #27ae60; }}
.card.enabled .num {{ color: #27ae60; }}
.card.notsup {{ border-left-color: #95a5a6; }}
.card.notsup .num {{ color: #95a5a6; }}
.card.err {{ border-left-color: #8e44ad; }}
.card.err .num {{ color: #8e44ad; }}

.report-section {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 20px; overflow: hidden; }}
.section-header {{ font-size: 1.1em; font-weight: 600; padding: 16px 20px; cursor: pointer; user-select: none; display: flex; align-items: center; gap: 10px; list-style: none; }}
.section-header::-webkit-details-marker {{ display: none; }}
.section-header::before {{ content: ""; display: inline-block; width: 0; height: 0; border-left: 6px solid #666; border-top: 5px solid transparent; border-bottom: 5px solid transparent; transition: transform 0.15s; flex-shrink: 0; }}
details[open] > .section-header::before {{ transform: rotate(90deg); }}
.section-body {{ padding: 0 20px 16px; }}

.badge {{ display: inline-flex; align-items: center; justify-content: center; color: #fff; font-size: 0.75em; font-weight: 600; padding: 2px 10px; border-radius: 12px; margin-left: auto; }}
.badge-sm {{ display: inline-flex; align-items: center; justify-content: center; background: #e8e8e8; color: #555; font-size: 0.75em; font-weight: 600; padding: 1px 8px; border-radius: 10px; margin-left: 8px; }}

.sub-header {{ font-size: 0.95em; font-weight: 600; padding: 10px 8px; cursor: pointer; user-select: none; display: flex; align-items: center; gap: 8px; list-style: none; border-bottom: 1px solid #f0f0f0; }}
.sub-header::-webkit-details-marker {{ display: none; }}
.sub-header::before {{ content: ""; display: inline-block; width: 0; height: 0; border-left: 5px solid #999; border-top: 4px solid transparent; border-bottom: 4px solid transparent; transition: transform 0.15s; flex-shrink: 0; }}
details[open] > .sub-header::before {{ transform: rotate(90deg); }}
.sub-id {{ color: #999; font-weight: 400; font-size: 0.85em; font-family: monospace; }}
.sub-body {{ padding-left: 16px; }}

.type-header {{ font-size: 0.85em; font-weight: 500; padding: 8px 4px; cursor: pointer; user-select: none; color: #555; display: flex; align-items: center; gap: 6px; list-style: none; font-family: monospace; }}
.type-header::-webkit-details-marker {{ display: none; }}
.type-header::before {{ content: ""; display: inline-block; width: 0; height: 0; border-left: 4px solid #bbb; border-top: 3px solid transparent; border-bottom: 3px solid transparent; transition: transform 0.15s; flex-shrink: 0; }}
details[open] > .type-header::before {{ transform: rotate(90deg); }}
.type-body {{ padding-left: 8px; }}

.resource-block {{ margin: 4px 0; border: 1px solid #eee; border-radius: 6px; }}
.resource-header {{ font-size: 0.85em; font-weight: 500; padding: 8px 12px; cursor: pointer; user-select: none; display: flex; align-items: center; gap: 8px; list-style: none; }}
.resource-header::-webkit-details-marker {{ display: none; }}
.resource-header::before {{ content: ""; display: inline-block; width: 0; height: 0; border-left: 4px solid #bbb; border-top: 3px solid transparent; border-bottom: 3px solid transparent; transition: transform 0.15s; flex-shrink: 0; }}
details[open] > .resource-header::before {{ transform: rotate(90deg); }}
.resource-block[open] {{ border-color: #ddd; background: #fcfcfc; }}
.res-meta {{ color: #999; font-weight: 400; font-size: 0.85em; }}

.res-detail {{ padding: 8px 12px; }}
.res-detail table {{ margin: 0; width: auto; }}
.res-detail td {{ border: none; padding: 2px 12px 2px 0; font-size: 0.8em; }}
.detail-key {{ color: #888; font-weight: 600; text-transform: uppercase; font-size: 0.7em; letter-spacing: 0.03em; white-space: nowrap; }}
.res-id-full {{ font-family: monospace; font-size: 0.8em; color: #666; word-break: break-all; }}

.settings-table {{ margin: 8px 12px 12px; border: 1px solid #e8e8e8; border-radius: 4px; width: calc(100% - 24px); }}
.settings-table th {{ font-size: 0.7em; padding: 4px 10px; background: #f5f5f5; text-transform: uppercase; letter-spacing: 0.03em; }}
.settings-table td {{ font-size: 0.8em; padding: 4px 10px; border-bottom: 1px solid #f0f0f0; }}
.settings-table tr:last-child td {{ border-bottom: none; }}

.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; }}
th, td {{ text-align: left; padding: 6px 12px; border-bottom: 1px solid #f0f0f0; font-size: 0.85em; }}
th {{ background: #fafafa; font-weight: 600; color: #555; font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.03em; }}
.res-id {{ font-family: monospace; font-size: 0.8em; color: #888; max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: help; }}
.res-id:hover {{ white-space: normal; overflow: visible; color: #333; }}
.cats-cell {{ font-size: 0.75em; max-width: 300px; }}
.no-cats {{ color: #c0392b; font-weight: 500; }}
.la-type {{ color: #2980b9; font-size: 0.85em; font-weight: 400; }}
.error-msg {{ padding: 8px 12px; font-size: 0.8em; color: #c0392b; background: #fdecea; border-radius: 4px; margin: 4px 12px 12px; }}

.subs-section {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 20px; border-left: 4px solid #34495e; }}
.subs-section details {{ padding: 0; }}
.subs-section .section-body {{ padding: 0 20px 12px; }}
.subs-section table th {{ background: #fafafa; }}

.dest-full-id {{ font-family: monospace; font-size: 0.75em; color: #999; padding: 0 12px 8px; word-break: break-all; }}
.dest-name {{ font-family: monospace; font-weight: 400; }}
.dest-type-group {{ margin-left: 16px; margin-bottom: 8px; }}
.dest-type-label {{ font-family: monospace; font-size: 0.8em; color: #666; padding: 4px 0; font-weight: 500; }}

.report-footer {{ text-align: center; color: #999; font-size: 0.8em; padding: 16px; }}
</style>
</head>
<body>
<div class="report-header">
  <h1>DudeWheresMyLogs Report</h1>
  <div class="subtitle">Generated {timestamp} | {len(subs)} subscription{"s" if len(subs) != 1 else ""} scanned | {total} resources evaluated</div>
</div>

<div class="report-body">

<div class="cards">
  <div class="card"><div class="num">{total}</div><div class="label">Total Resources</div></div>
  <div class="card missing"><div class="num">{status_counts.get("Missing", 0)}</div><div class="label">Missing Diagnostics</div></div>
  <div class="card dup"><div class="num">{dup_count}</div><div class="label">Duplicate Shipping</div></div>
  <div class="card enabled"><div class="num">{status_counts.get("Enabled", 0) - dup_count}</div><div class="label">Healthy</div></div>
  <div class="card notsup"><div class="num">{status_counts.get("Not Supported", 0)}</div><div class="label">Not Supported</div></div>
  <div class="card err"><div class="num">{status_counts.get("Error", 0)}</div><div class="label">Errors</div></div>
</div>

<div class="subs-section">
<details>
<summary class="section-header">Subscriptions Scanned<span class="badge" style="background: #34495e;">{len(subs)}</span></summary>
<div class="section-body">
<table><thead><tr><th>Name</th><th>Subscription ID</th></tr></thead>
<tbody>{sub_rows}</tbody></table>
</div>
</details>
</div>

{missing_section}
{dup_section}
{enabled_section}
{info_section}
{dest_section}

<div class="report-footer">DudeWheresMyLogs -- Azure Diagnostic Logging Audit</div>
</div>
</body>
</html>"""

    with open(output, "w") as f:
        f.write(report_html)

    return output
