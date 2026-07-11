import argparse
import fnmatch
import sys
from collections import Counter

from . import __version__
from .checks import CHECK_NAMES, DEFAULT_FAIL_ON, get_check, get_checks
from .reporting import generate_report


def _positive_int(value):
    """argparse type: integer >= 1."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser():
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="DudeWheresMyLogs",
        description="Azure diagnostic logging auditor - find missing logs, "
                    "duplicate log shipping, and wasted spend.",
    )
    sub_group = parser.add_mutually_exclusive_group()
    sub_group.add_argument(
        "-s", "--subscription",
        action="append",
        metavar="SUB_ID",
        help="Subscription ID(s) to scan. Can be specified multiple times. "
             "If omitted, shows an interactive picker.",
    )
    sub_group.add_argument(
        "-a", "--all",
        action="store_true",
        dest="all_subs",
        help="Scan all accessible subscriptions (non-interactive).",
    )
    parser.add_argument(
        "-f", "--format",
        choices=["html", "csv", "json", "md"],
        default="html",
        help="Output format (default: html).",
    )
    parser.add_argument(
        "-o", "--output",
        metavar="FILE",
        help="Output file path (auto-generated if omitted).",
    )
    parser.add_argument(
        "-w", "--workers",
        type=_positive_int,
        default=10,
        metavar="N",
        help="Number of parallel workers (default: 10).",
    )
    parser.add_argument(
        "--include-types",
        action="append",
        metavar="PATTERN",
        help="Only scan matching resource types. Supports wildcards and can be repeated.",
    )
    parser.add_argument(
        "--exclude-types",
        action="append",
        metavar="PATTERN",
        help="Skip matching resource types. Supports wildcards and can be repeated.",
    )
    parser.add_argument(
        "--resource-group",
        action="append",
        metavar="NAME",
        help="Only scan resources in matching resource groups. Supports wildcards and can be repeated.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Use CI-friendly exit codes: 0=clean, 1=findings, 2=errors.",
    )
    parser.add_argument(
        "--checks",
        action="append",
        metavar="CHECK",
        help="Finding checks to run and report. "
             f"Choices: {', '.join(CHECK_NAMES)}. "
             "Repeatable or comma-separated. Default: all checks.",
    )
    parser.add_argument(
        "--fail-on",
        action="append",
        metavar="CATEGORY",
        help="Finding categories that count as findings in --ci mode. "
             f"Choices: {', '.join(CHECK_NAMES)}. "
             "Repeatable or comma-separated. Must be active checks. "
             f"Default: {','.join(DEFAULT_FAIL_ON)}.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Omit per-resource detail for healthy and informational sections "
             "in HTML/Markdown reports (findings stay fully detailed).",
    )
    parser.add_argument(
        "--lookback-days",
        type=_positive_int,
        default=30,
        metavar="N",
        help="Lookback window for workspace usage analysis (default: 30).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _normalize_patterns(values):
    """Flatten repeated and comma-separated CLI pattern values."""
    if not values:
        return []
    patterns = []
    for value in values:
        parts = [part.strip() for part in value.split(",")]
        patterns.extend(part for part in parts if part)
    return patterns


def _matches_any(value, patterns):
    """Case-insensitive wildcard/exact match against any pattern."""
    normalized = value.lower()
    return any(fnmatch.fnmatch(normalized, pattern.lower()) for pattern in patterns)


def filter_resources(resources, include_types=None, exclude_types=None, resource_groups=None):
    """Filter resources by type and resource group."""
    include_types = _normalize_patterns(include_types)
    exclude_types = _normalize_patterns(exclude_types)
    resource_groups = _normalize_patterns(resource_groups)

    filtered = []
    for resource in resources:
        resource_type = resource.get("type", "")
        resource_group = resource.get("resource_group", "")

        if include_types and not _matches_any(resource_type, include_types):
            continue
        if exclude_types and _matches_any(resource_type, exclude_types):
            continue
        if resource_groups and not _matches_any(resource_group, resource_groups):
            continue

        filtered.append(resource)

    return filtered


def _determine_exit_code(results, ci_mode=False, fail_on=None, ws_results=None,
                         sub_audits=None):
    """Return the appropriate process exit code for the scan results.

    fail_on is an iterable of check names controlling what counts as a
    finding (exit 1). Scan errors always take precedence (exit 2).
    """
    if not ci_mode:
        return 0

    has_errors = any(result.status == "Error" for result in results)
    if has_errors:
        return 2

    pools = {
        "resource": results,
        "workspace": ws_results or [],
        "subscription": sub_audits or [],
    }
    categories = DEFAULT_FAIL_ON if fail_on is None else fail_on
    for category in categories:
        check = get_check(category)
        if any(check.detect(item) for item in pools[check.scope]):
            return 1
    return 0


def _parse_checks(values, parser):
    """Validate --checks values; returns a tuple of check names (all if unset)."""
    if not values:
        return CHECK_NAMES
    names = _normalize_patterns(values)
    invalid = [c for c in names if c not in CHECK_NAMES]
    if invalid:
        parser.error(
            f"invalid --checks value: {', '.join(invalid)} "
            f"(choose from {', '.join(CHECK_NAMES)})"
        )
    return tuple(dict.fromkeys(names))


def _parse_fail_on(values, parser, active_checks):
    """Validate --fail-on values against active checks; returns a tuple of names."""
    if not values:
        return tuple(c for c in DEFAULT_FAIL_ON if c in active_checks)
    categories = _normalize_patterns(values)
    invalid = [c for c in categories if c not in CHECK_NAMES]
    if invalid:
        parser.error(
            f"invalid --fail-on category: {', '.join(invalid)} "
            f"(choose from {', '.join(CHECK_NAMES)})"
        )
    inactive = [c for c in categories if c not in active_checks]
    if inactive:
        parser.error(
            f"--fail-on category not in active --checks: {', '.join(inactive)}"
        )
    return tuple(dict.fromkeys(categories))


def select_subscriptions_interactive(subscriptions):
    """Display subscriptions and let the user pick one or more."""
    print("\nAvailable Subscriptions:")
    for i, sub in enumerate(subscriptions):
        print(f"  [{i}] {sub['name']} ({sub['id']})")

    print("\n  Enter one or more numbers (comma-separated, e.g. 0,2,5)")
    print("  [A] Scan ALL subscriptions")
    print("  [Q] Quit")

    while True:
        choice = input("\nEnter your choice: ").strip().upper()

        if choice == "Q":
            sys.exit(0)

        if choice == "A":
            return subscriptions

        try:
            indices = [int(part) for part in choice.replace(" ", "").split(",") if part]
        except ValueError:
            print("Enter numbers (comma-separated), A, or Q.")
            continue

        if indices and all(0 <= idx < len(subscriptions) for idx in indices):
            # Preserve order, drop duplicates
            return [subscriptions[idx] for idx in dict.fromkeys(indices)]
        print("Invalid selection. Try again.")


def run(argv=None):
    """Run DudeWheresMyLogs and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    active_checks = _parse_checks(args.checks, parser)
    fail_on = _parse_fail_on(args.fail_on, parser, active_checks)

    from .azure import get_credential, list_resources, list_subscriptions
    from .diagnostics import check_all_diagnostics

    print(f"DudeWheresMyLogs v{__version__}")
    print("Azure Diagnostic Logging Auditor\n")

    # Authenticate
    credential = get_credential()
    print("Authenticated successfully.\n")

    # Resolve subscriptions. In CI mode an empty subscription list is an
    # operational error (exit 2), not a findings result (exit 1).
    all_subs = list_subscriptions(credential)
    if not all_subs:
        print("No subscriptions found.")
        return 2 if args.ci else 1

    if args.all_subs:
        selected = all_subs
        print(f"Scanning all {len(selected)} subscriptions.\n")
    elif args.subscription:
        sub_map = {s["id"].lower(): s for s in all_subs}
        selected = []
        for sid in args.subscription:
            if sid.lower() in sub_map:
                selected.append(sub_map[sid.lower()])
            else:
                print(f"Warning: subscription {sid} not found or not accessible, skipping.")
        if not selected:
            print("No valid subscriptions to scan.")
            return 2 if args.ci else 1
    else:
        selected = select_subscriptions_interactive(all_subs)

    # Scan each subscription
    all_results = []
    sub_audits = []
    audit_activity_log = "no-activity-log-export" in active_checks

    for sub in selected:
        print(f"Scanning subscription: {sub['name']} ({sub['id']})")

        if audit_activity_log:
            from .tenant import audit_subscription
            sub_audits.append(audit_subscription(credential, sub["id"], sub["name"]))

        sys.stderr.write("Enumerating resources... ")
        sys.stderr.flush()
        resources = list_resources(credential, sub["id"])
        total_resources = len(resources)
        resources = filter_resources(
            resources,
            include_types=args.include_types,
            exclude_types=args.exclude_types,
            resource_groups=args.resource_group,
        )
        sys.stderr.write(f"found {total_resources} resources, scanning {len(resources)} after filters.\n")
        sys.stderr.flush()

        if not resources:
            print("  No resources found, skipping.\n")
            continue

        results = check_all_diagnostics(
            credential, sub["id"], sub["name"], resources,
            max_workers=args.workers,
        )
        all_results.extend(results)
        print()

    if not all_results and not sub_audits:
        print("No results to report.")
        return 0

    # Workspace usage analysis: needed by workspace-scope checks and by the
    # resource-scope silent-resources reconciliation
    ws_results = []
    needs_ws_analysis = (
        get_checks(active_checks, scope="workspace")
        or "silent-resources" in active_checks
    )
    if needs_ws_analysis:
        from .workspaces import analyze_workspaces, flag_silent_resources
        ws_results, seen_map = analyze_workspaces(
            credential, all_results,
            max_workers=args.workers, lookback_days=args.lookback_days,
        )
        if "silent-resources" in active_checks:
            flag_silent_resources(all_results, seen_map)

    # Print summary
    status_counts = Counter(r.status for r in all_results)

    print("--- Summary ---")
    print(f"  {'Total entries:':<24}{len(all_results)}")
    print(f"  {'Enabled:':<24}{status_counts.get('Enabled', 0)}")
    for check in get_checks(active_checks, scope="resource"):
        count = sum(1 for r in all_results if check.detect(r))
        print(f"  {check.title + ':':<24}{count}")
    for check in get_checks(active_checks, scope="workspace"):
        count = sum(1 for ws in ws_results if check.detect(ws))
        print(f"  {check.title + ':':<24}{count}")
    for check in get_checks(active_checks, scope="subscription"):
        count = sum(1 for s in sub_audits if check.detect(s))
        print(f"  {check.title + ':':<24}{count}")
    print(f"  {'Not Supported:':<24}{status_counts.get('Not Supported', 0)}")
    print(f"  {'Errors:':<24}{status_counts.get('Error', 0)}")

    # Generate report
    output_path = generate_report(
        all_results, fmt=args.format, output=args.output,
        summary_only=args.summary_only, checks=active_checks,
        ws_results=ws_results, sub_audits=sub_audits,
    )
    print(f"\nReport saved to: {output_path}")
    return _determine_exit_code(all_results, ci_mode=args.ci, fail_on=fail_on,
                                ws_results=ws_results, sub_audits=sub_audits)


def main():
    """CLI entry point."""
    sys.exit(run())
