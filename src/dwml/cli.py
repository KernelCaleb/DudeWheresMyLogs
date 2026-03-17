import argparse
import sys
from collections import Counter

from . import __version__
from .azure import get_credential, list_subscriptions, list_resources
from .diagnostics import check_all_diagnostics
from .reporting import generate_report


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
        choices=["html", "csv"],
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
        type=int,
        default=10,
        metavar="N",
        help="Number of parallel workers (default: 10).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def select_subscriptions_interactive(subscriptions):
    """Display subscriptions and let the user pick."""
    print("\nAvailable Subscriptions:")
    for i, sub in enumerate(subscriptions):
        print(f"  [{i}] {sub['name']} ({sub['id']})")

    print("\n  [A] Scan ALL subscriptions")
    print("  [Q] Quit")

    while True:
        choice = input("\nEnter your choice: ").strip().upper()

        if choice == "Q":
            sys.exit(0)

        if choice == "A":
            return subscriptions

        try:
            idx = int(choice)
            if 0 <= idx < len(subscriptions):
                return [subscriptions[idx]]
            print("Invalid selection. Try again.")
        except ValueError:
            print("Enter a number, A, or Q.")


def main():
    """Entry point for DudeWheresMyLogs."""
    parser = build_parser()
    args = parser.parse_args()

    print(f"DudeWheresMyLogs v{__version__}")
    print("Azure Diagnostic Logging Auditor\n")

    # Authenticate
    credential = get_credential()
    print("Authenticated successfully.\n")

    # Resolve subscriptions
    all_subs = list_subscriptions(credential)
    if not all_subs:
        print("No subscriptions found.")
        sys.exit(1)

    if args.all_subs:
        selected = all_subs
        print(f"Scanning all {len(selected)} subscriptions.\n")
    elif args.subscription:
        sub_map = {s["id"]: s for s in all_subs}
        selected = []
        for sid in args.subscription:
            if sid in sub_map:
                selected.append(sub_map[sid])
            else:
                print(f"Warning: subscription {sid} not found or not accessible, skipping.")
        if not selected:
            print("No valid subscriptions to scan.")
            sys.exit(1)
    else:
        selected = select_subscriptions_interactive(all_subs)

    # Scan each subscription
    all_results = []

    for sub in selected:
        print(f"Scanning subscription: {sub['name']} ({sub['id']})")

        sys.stderr.write("Enumerating resources... ")
        sys.stderr.flush()
        resources = list_resources(credential, sub["id"])
        sys.stderr.write(f"found {len(resources)} resources.\n")
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

    if not all_results:
        print("No results to report.")
        sys.exit(0)

    # Print summary
    status_counts = Counter(r.status for r in all_results)
    dup_count = sum(1 for r in all_results if r.duplicate)

    print("--- Summary ---")
    print(f"  Total entries:   {len(all_results)}")
    print(f"  Enabled:         {status_counts.get('Enabled', 0)}")
    print(f"  Missing:         {status_counts.get('Missing', 0)}")
    print(f"  Duplicates:      {dup_count}")
    print(f"  Not Supported:   {status_counts.get('Not Supported', 0)}")
    print(f"  Errors:          {status_counts.get('Error', 0)}")

    # Generate report
    output_path = generate_report(all_results, fmt=args.format, output=args.output)
    print(f"\nReport saved to: {output_path}")
