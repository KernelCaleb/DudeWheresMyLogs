"""Pluggable finding checks.

Each Check is a named finding category: how to detect it on a
DiagnosticResult, how to present it in reports, and whether it fails a CI
scan by default. The CLI and reporting derive flags, sections, and exit-code
behavior from this registry, so adding a future check (e.g. workspace usage)
means registering it here and implementing its scan/annotation phase in
diagnostics.py.

Note: --checks controls which findings are derived and reported. The
underlying scan data (destinations, regions) is always collected, so CSV and
JSON exports carry the full raw detail regardless of active checks.
"""
from dataclasses import dataclass

from .diagnostics import has_cross_region, has_dead_destination


@dataclass(frozen=True)
class Check:
    name: str              # CLI name used by --checks / --fail-on
    title: str             # report section title
    description: str
    detect: object         # callable(result) -> bool; the result type matches scope
    default_fail_on: bool  # counts as a CI finding unless --fail-on overrides
    severity: str          # report styling: "warn", "dup", or "err"
    row_kind: str          # HTML row layout: "missing" or "duplicate"
    default_open: bool     # HTML section expanded when non-empty
    anchor: str            # stable HTML anchor ID
    dest_label: str = ""   # Markdown destination column header ("" = no column)
    dest_filter: object = None  # callable(dest dict) -> bool for that column
    scope: str = "resource"  # "resource" (DiagnosticResult) or "workspace" (WorkspaceUsage)


CHECKS = (
    Check(
        name="missing",
        title="Missing Diagnostics",
        description="Resources with no diagnostic settings configured",
        detect=lambda r: r.status == "Missing",
        default_fail_on=True,
        severity="warn",
        row_kind="missing",
        default_open=True,
        anchor="missing",
    ),
    Check(
        name="duplicates",
        title="Duplicate Shipping",
        description="Same destination type shipping to two or more different destinations",
        detect=lambda r: r.duplicate,
        default_fail_on=True,
        severity="dup",
        row_kind="duplicate",
        default_open=True,
        anchor="duplicate",
        dest_label="Destinations",
    ),
    Check(
        name="dead-destinations",
        title="Dead Destinations",
        description="Diagnostic settings shipping to destinations that no longer exist",
        detect=has_dead_destination,
        default_fail_on=True,
        severity="err",
        row_kind="duplicate",
        default_open=True,
        anchor="dead",
        dest_label="Dead Destination",
        dest_filter=lambda d: d.get("not_found"),
    ),
    Check(
        name="cross-region",
        title="Cross-Region Shipping",
        description="Destinations in a different region than the source resource",
        detect=has_cross_region,
        default_fail_on=False,
        severity="warn",
        row_kind="duplicate",
        default_open=False,
        anchor="cross-region",
        dest_label="Cross-Region Destination",
        dest_filter=lambda d: d.get("cross_region"),
    ),
    Check(
        name="silent-resources",
        title="Configured But Silent",
        description="Resources shipping to a workspace where none of their data "
                    "arrived in the lookback window (signal, not proof: an idle "
                    "resource legitimately emits nothing)",
        detect=lambda r: any(d.get("silent") for d in r.destinations),
        default_fail_on=False,
        severity="warn",
        row_kind="duplicate",
        default_open=False,
        anchor="silent",
        dest_label="Silent Destination",
        dest_filter=lambda d: d.get("silent"),
    ),
    Check(
        name="unqueried-workspaces",
        title="Unqueried Workspaces",
        description="Workspaces receiving logs that nobody has queried in the lookback window",
        detect=lambda ws: ws.audit_enabled is True and ws.query_count == 0,
        default_fail_on=False,
        severity="warn",
        row_kind="duplicate",
        default_open=False,
        anchor="workspace-usage",
        scope="workspace",
    ),
    Check(
        name="no-query-auditing",
        title="No Query Auditing",
        description="Workspaces without the Audit category enabled; query activity cannot be assessed",
        detect=lambda ws: ws.audit_enabled is False,
        default_fail_on=False,
        severity="warn",
        row_kind="duplicate",
        default_open=False,
        anchor="workspace-usage",
        scope="workspace",
    ),
)

CHECK_NAMES = tuple(c.name for c in CHECKS)
DEFAULT_FAIL_ON = tuple(c.name for c in CHECKS if c.default_fail_on)
_BY_NAME = {c.name: c for c in CHECKS}


def get_checks(names=None, scope=None):
    """Return Check objects for the given names, in registry order.

    None means all checks. scope filters to "resource" or "workspace" checks.
    """
    selected = CHECKS if names is None else tuple(
        c for c in CHECKS if c.name in set(names))
    if scope is not None:
        selected = tuple(c for c in selected if c.scope == scope)
    return selected


def get_check(name):
    """Return a single Check by name."""
    return _BY_NAME[name]


def is_healthy(result, checks=None):
    """A result is healthy if Enabled and no active resource-scope check flags it."""
    return result.status == "Enabled" and not any(
        c.detect(result) for c in get_checks(checks, scope="resource")
    )
