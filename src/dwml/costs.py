"""Cost estimation for log destinations (v2.5).

Attaches estimated monthly dollar figures to workspaces (ingestion +
retention) and to findings (duplicate shipping, cross-region bandwidth).
All numbers are ESTIMATES from a list-price table: regional rates, EA
discounts, free allocations, and commitment tiers change actual bills.
The default table ships in prices.json; override with --price-file.
"""
import json
from pathlib import Path

from .diagnostics import _norm_region

_DEFAULT_PRICES = Path(__file__).parent / "prices.json"

# Region name prefix -> continent group used for bandwidth rates
_CONTINENT_PREFIXES = (
    ("brazil", "southamerica"),
    ("southafrica", "africa"),
    ("uae", "middleeast"), ("qatar", "middleeast"), ("israel", "middleeast"),
    ("australia", "oceania"), ("newzealand", "oceania"),
    ("japan", "asia"), ("korea", "asia"), ("eastasia", "asia"),
    ("southeastasia", "asia"), ("india", "asia"), ("china", "asia"),
    ("indonesia", "asia"), ("malaysia", "asia"), ("taiwan", "asia"),
    ("uk", "europe"), ("northeurope", "europe"), ("westeurope", "europe"),
    ("france", "europe"), ("germany", "europe"), ("switzerland", "europe"),
    ("norway", "europe"), ("sweden", "europe"), ("poland", "europe"),
    ("italy", "europe"), ("spain", "europe"), ("austria", "europe"),
    ("belgium", "europe"), ("denmark", "europe"), ("finland", "europe"),
    ("us", "northamerica"), ("canada", "northamerica"), ("mexico", "northamerica"),
)


def load_prices(path=None):
    """Load the price table (package default when path is None)."""
    with open(path or _DEFAULT_PRICES, encoding="utf-8") as f:
        return json.load(f)


def _continent(region):
    normalized = _norm_region(region)
    for prefix, continent in _CONTINENT_PREFIXES:
        if prefix in normalized:
            return continent
    return ""


def bandwidth_rate(src_region, dst_region, prices):
    """Estimated egress $/GB for cross-region shipping, by source continent."""
    rates = prices.get("bandwidth_per_gb", {})
    src = _continent(src_region)
    dst = _continent(dst_region)
    if not src or not dst:
        return rates.get("intra_continent", 0.0)
    if src == "southamerica":
        return rates.get("south_america", 0.0)
    if src in ("asia", "oceania", "middleeast", "africa"):
        return rates.get("asia_oceania_me_africa", 0.0)
    if src == dst:
        return rates.get("intra_continent", 0.0)
    return rates.get("na_eu_to_other", 0.0)


def _monthly(gb, lookback_days):
    """Normalize a lookback-window GB figure to a 30-day month."""
    if not gb or not lookback_days:
        return 0.0
    return gb * 30.0 / lookback_days


def _analytics_rate(ws, prices):
    key = ("sentinel_analytics_per_gb" if ws.sentinel_enabled
           else "log_analytics_analytics_per_gb")
    return prices.get(key, 0.0)


def _estimate_workspace(ws, prices):
    """Fill est_monthly_* on one WorkspaceUsage. Needs ingest data."""
    if ws.ingest_gb is None:
        return
    plan_gb = ws.ingest_gb_by_plan or {}
    analytics_gb = plan_gb.get("analytics")
    if analytics_gb is None:
        analytics_gb = ws.ingest_gb  # no plan data: assume all Analytics

    ingest = (
        _monthly(analytics_gb, ws.lookback_days) * _analytics_rate(ws, prices)
        + _monthly(plan_gb.get("basic", 0.0), ws.lookback_days)
        * prices.get("basic_logs_per_gb", 0.0)
        + _monthly(plan_gb.get("auxiliary", 0.0), ws.lookback_days)
        * prices.get("auxiliary_logs_per_gb", 0.0)
    )

    free_days = prices.get(
        "free_retention_days_sentinel" if ws.sentinel_enabled else "free_retention_days",
        31)
    extra_days = max((ws.retention_days or 0) - free_days, 0)
    daily_gb = _monthly(ws.ingest_gb, ws.lookback_days) / 30.0
    retention = daily_gb * extra_days * prices.get(
        "interactive_retention_per_gb_month", 0.0)

    ws.est_monthly_ingest = round(ingest, 6)
    ws.est_monthly_retention = round(retention, 6)
    ws.est_monthly_total = round(ingest + retention, 6)


def _estimate_result_impact(r, ws_by_id, resource_gb, prices):
    """Estimated monthly waste for one result: redundant duplicate flows plus
    cross-region bandwidth. Only Log Analytics destinations carry measured
    GB; others contribute nothing."""
    rid = r.resource_id.lower()

    def flow_gb(wid):
        ws = ws_by_id.get(wid)
        if ws is None or wid not in resource_gb:
            return None, None
        return resource_gb[wid].get(rid, 0.0), ws

    impact = 0.0
    measured = False

    # Duplicate shipping: every distinct destination beyond the largest flow
    # of the same type is redundant spend
    if r.duplicate:
        la_ids = list(dict.fromkeys(
            d["id"] for d in r.destinations
            if d.get("type") == "Log Analytics" and not d.get("not_found")))
        if len(la_ids) > 1:
            flows = []
            for wid in la_ids:
                gb, ws = flow_gb(wid)
                if gb is not None:
                    flows.append((gb, ws))
            if flows:
                measured = True
                flows.sort(key=lambda x: -x[0])
                for gb, ws in flows[1:]:  # keep the largest, rest is waste
                    impact += (_monthly(gb, ws.lookback_days)
                               * _analytics_rate(ws, prices))

    # Cross-region: bandwidth on the measured flow
    for d in r.destinations:
        if not d.get("cross_region") or d.get("type") != "Log Analytics":
            continue
        gb, ws = flow_gb(d.get("id", ""))
        if gb is None:
            continue
        measured = True
        impact += (_monthly(gb, ws.lookback_days)
                   * bandwidth_rate(r.resource_location, d.get("region", ""), prices))

    if measured:
        r.est_monthly_impact = round(impact, 6)


def estimate_costs(results, ws_results, resource_gb, prices):
    """Attach cost estimates to workspaces and findings in place.

    resource_gb: workspace ARM ID -> {lowercased resource ID -> GB in window}
    (the seen_map from analyze_workspaces).
    """
    for ws in ws_results:
        _estimate_workspace(ws, prices)

    ws_by_id = {ws.workspace_id: ws for ws in ws_results}
    for r in results:
        if r.duplicate or any(d.get("cross_region") for d in r.destinations):
            _estimate_result_impact(r, ws_by_id, resource_gb, prices)


def export_fee_destinations(results):
    """Distinct Storage/Event Hub destination IDs subject to the platform
    log export fee (billing active since June 2026)."""
    ids = set()
    for r in results:
        for d in r.destinations:
            if d.get("type") in ("Storage Account", "Event Hub") and d.get("id"):
                ids.add(d["id"])
    return ids


def fmt_usd(value):
    """Format an estimate for display."""
    if value is None:
        return "?"
    if 0 < value < 0.005:
        return "<$0.01"
    return f"${value:,.2f}"
