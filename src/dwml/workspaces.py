"""Log Analytics workspace usage analysis (v2.2).

For every workspace that appears as a Log Analytics destination in the scan,
answer two questions the resource-level scan cannot:

- Is anyone actually querying this workspace? (LAQueryLogs via the data plane;
  requires the workspace's own "Audit" diagnostic category to be enabled)
- How much data is it ingesting? (Usage table, informational)

Access model: workspace config and audit detection need only ARM Reader.
The query/ingestion lookups need data-plane access (Log Analytics Reader).
Both degrade gracefully: what the credential cannot see is reported as
unknown, never guessed and never fatal to the scan.
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

try:
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
    from azure.mgmt.loganalytics import LogAnalyticsManagementClient
    from azure.mgmt.monitor import MonitorManagementClient
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus
except ModuleNotFoundError:  # pragma: no cover - keeps unit tests importable without Azure SDK
    HttpResponseError = None
    ResourceNotFoundError = None
    LogAnalyticsManagementClient = None
    MonitorManagementClient = None
    LogsQueryClient = None
    LogsQueryStatus = None

from .azure import _resource_group_from_id, _retry_policy_kwargs
from .diagnostics import _dest_name_from_id, _subscription_from_id

# Self-identifying token: it appears inside our own KQL, so the LAQueryLogs
# filter that excludes it automatically excludes the tool's own queries.
_SELF_MARKER = "dwml-usage-check"

_QUERY_COUNT_KQL = (
    f'LAQueryLogs | where QueryText !has "{_SELF_MARKER}" | count'
)
_INGEST_KQL = (
    "Usage | where IsBillable == true "
    f"| summarize IngestGB = sum(Quantity) / 1024.0 // {_SELF_MARKER}"
)


@dataclass
class WorkspaceUsage:
    """Usage assessment for one Log Analytics destination workspace."""
    workspace_id: str          # ARM resource ID
    name: str
    subscription_id: str
    resource_group: str
    region: str = ""
    retention_days: int = 0
    sku: str = ""
    daily_cap_gb: float = 0.0  # 0 = no cap
    shipping_resources: int = 0
    audit_enabled: object = None   # True/False, None = could not determine
    query_count: object = None     # int, None = no data-plane access
    ingest_gb: object = None       # float, None = no data-plane access
    lookback_days: int = 30
    access_error: str = ""


def workspace_status(ws):
    """Human-readable assessment used by reports."""
    if ws.access_error:
        return f"Unknown ({ws.access_error})"
    if ws.audit_enabled is False:
        return "Unknown (query auditing not enabled)"
    if ws.audit_enabled is True and ws.query_count == 0:
        return f"Unqueried ({ws.lookback_days}d)"
    if isinstance(ws.query_count, int) and ws.query_count > 0:
        return f"Active ({ws.query_count} queries/{ws.lookback_days}d)"
    return "Unknown"


def _workspace_audit_enabled(settings_list):
    """True if any diagnostic setting enables the workspace Audit category.

    The Audit category feeds LAQueryLogs; without it, query activity is
    invisible and usage cannot be assessed.
    """
    for setting in settings_list:
        for log in setting.logs or []:
            if not log.enabled:
                continue
            category = (log.category or "").lower()
            group = (getattr(log, "category_group", None) or "").lower()
            if category == "audit" or group in ("audit", "alllogs"):
                return True
    return False


def _collect_destination_workspaces(results):
    """Map workspace ARM ID -> count of scanned resources shipping to it.

    Destinations flagged not_found are excluded: they are already reported
    as dead destinations.
    """
    shipping = {}
    for r in results:
        for d in r.destinations:
            if d.get("type") != "Log Analytics" or d.get("not_found"):
                continue
            wid = d.get("id", "")
            if wid:
                shipping[wid] = shipping.get(wid, 0) + 1
    return shipping


def _scalar(response):
    """Extract the single scalar value from a one-row/one-column KQL result."""
    for table in response.tables or []:
        for row in table.rows or []:
            if row and row[0] is not None:
                return row[0]
    return 0


def _analyze_one(ws, mgmt_clients, monitor_clients, logs_client, lookback_days, lock):
    """Fill in one WorkspaceUsage in place. Never raises."""
    # Management plane: workspace config + customer ID
    customer_id = None
    try:
        with lock:
            client = mgmt_clients[ws.subscription_id]
        workspace = client.workspaces.get(ws.resource_group, ws.name)
        ws.region = getattr(workspace, "location", "") or ""
        ws.retention_days = getattr(workspace, "retention_in_days", 0) or 0
        sku = getattr(workspace, "sku", None)
        ws.sku = str(getattr(sku, "name", "") or "")
        capping = getattr(workspace, "workspace_capping", None)
        cap = getattr(capping, "daily_quota_gb", None)
        ws.daily_cap_gb = float(cap) if cap and cap > 0 else 0.0
        customer_id = getattr(workspace, "customer_id", None)
    except ResourceNotFoundError:
        ws.access_error = "workspace not found"
        return
    except Exception as e:
        ws.access_error = f"config lookup failed: {str(e)[:80]}"
        return

    # Management plane: is the workspace's own Audit category enabled?
    try:
        with lock:
            monitor = monitor_clients[ws.subscription_id]
        settings = list(monitor.diagnostic_settings.list(resource_uri=ws.workspace_id))
        ws.audit_enabled = _workspace_audit_enabled(settings)
    except Exception:
        ws.audit_enabled = None

    # Data plane: query activity and ingestion volume
    if logs_client is None or not customer_id:
        ws.access_error = "no data-plane client"
        return
    timespan = timedelta(days=lookback_days)
    try:
        response = logs_client.query_workspace(
            workspace_id=customer_id, query=_QUERY_COUNT_KQL, timespan=timespan)
        if LogsQueryStatus is None or response.status != LogsQueryStatus.FAILURE:
            ws.query_count = int(_scalar(response))
    except HttpResponseError as e:
        message = str(e).lower()
        if "failed to resolve table" in message or "laquerylogs" in message:
            # Audit category never produced a record: no queries have landed
            ws.query_count = 0
        elif getattr(e, "status_code", None) == 403:
            ws.access_error = "no data-plane access"
            return
        # otherwise leave query_count unknown
    except Exception:
        pass

    try:
        response = logs_client.query_workspace(
            workspace_id=customer_id, query=_INGEST_KQL, timespan=timespan)
        if LogsQueryStatus is None or response.status != LogsQueryStatus.FAILURE:
            ws.ingest_gb = round(float(_scalar(response)), 4)
    except Exception:
        pass


def analyze_workspaces(credential, results, max_workers=10, lookback_days=30):
    """Analyze every Log Analytics destination workspace found in the scan.

    Returns a list of WorkspaceUsage sorted by shipping resource count
    (highest impact first).
    """
    if LogAnalyticsManagementClient is None:
        raise RuntimeError(
            "azure-mgmt-loganalytics and azure-monitor-query are required "
            "for workspace usage analysis")

    shipping = _collect_destination_workspaces(results)
    if not shipping:
        return []

    workspaces = []
    for wid, count in shipping.items():
        workspaces.append(WorkspaceUsage(
            workspace_id=wid,
            name=_dest_name_from_id(wid),
            subscription_id=_subscription_from_id(wid),
            resource_group=_resource_group_from_id(wid),
            shipping_resources=count,
            lookback_days=lookback_days,
        ))

    # One management/monitor client per subscription, one shared logs client
    lock = threading.Lock()
    mgmt_clients, monitor_clients = {}, {}
    for ws in workspaces:
        sub = ws.subscription_id
        if sub not in mgmt_clients:
            try:
                mgmt_clients[sub] = LogAnalyticsManagementClient(
                    credential, sub, **_retry_policy_kwargs())
                monitor_clients[sub] = MonitorManagementClient(
                    credential, sub, **_retry_policy_kwargs())
            except Exception:
                pass

    try:
        logs_client = LogsQueryClient(credential)
    except Exception:
        logs_client = None

    analyzable = [ws for ws in workspaces if ws.subscription_id in mgmt_clients]
    for ws in workspaces:
        if ws.subscription_id not in mgmt_clients:
            ws.access_error = "no management-plane access"

    sys.stderr.write(f"Analyzing {len(analyzable)} destination workspace(s)...\n")
    sys.stderr.flush()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for ws in analyzable:
            executor.submit(_analyze_one, ws, mgmt_clients, monitor_clients,
                            logs_client, lookback_days, lock)

    workspaces.sort(key=lambda w: (-w.shipping_resources, w.name.lower()))
    return workspaces
