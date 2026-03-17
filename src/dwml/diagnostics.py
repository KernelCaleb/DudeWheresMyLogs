import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from azure.mgmt.monitor import MonitorManagementClient
from azure.core.exceptions import HttpResponseError

from .azure import _retry_policy_kwargs


def _resource_group_from_id(resource_id):
    """Extract the resource group name from an Azure resource ID."""
    parts = resource_id.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


@dataclass
class DiagnosticResult:
    """Result of checking diagnostic settings for a single resource."""
    resource_id: str
    resource_name: str
    resource_type: str
    resource_group: str
    resource_location: str
    subscription_id: str
    subscription_name: str
    status: str  # "Enabled", "Missing", "Not Supported", "Error"
    destinations: list = field(default_factory=list)
    duplicate: bool = False
    error_message: str = ""


# Thread-local storage for MonitorManagementClient instances
_thread_local = threading.local()


def _get_monitor_client(credential, subscription_id):
    """Get or create a thread-local MonitorManagementClient."""
    key = f"monitor_{subscription_id}"
    if not hasattr(_thread_local, key):
        setattr(_thread_local, key, MonitorManagementClient(credential, subscription_id,
                                                              **_retry_policy_kwargs()))
    return getattr(_thread_local, key)


def _is_not_supported_error(error):
    """Check if an HttpResponseError indicates the resource type doesn't support diagnostics."""
    if not isinstance(error, HttpResponseError):
        return False
    code = getattr(error, "error", None)
    if code is not None:
        code = getattr(code, "code", None)
    not_supported_codes = {
        "ResourceNotOnboarded",
        "BillingScopeNotFound",
        "InvalidResourceType",
        "CategoryNotApplicable",
        "Subscription level diagnostic settings doesn't support this category",
    }
    if code and code in not_supported_codes:
        return True
    msg = str(error).lower()
    not_supported_phrases = [
        "does not support diagnostic settings",
        "not a supported platform",
        "is not supported",
        "resourcenotonboarded",
        "invalidresourcetype",
    ]
    return any(phrase in msg for phrase in not_supported_phrases)


def _dest_name_from_id(resource_id):
    """Extract the resource name (last segment) from an Azure resource ID."""
    if not resource_id:
        return ""
    return resource_id.rstrip("/").split("/")[-1]


def _subscription_from_id(resource_id):
    """Extract the subscription ID from an Azure resource ID."""
    parts = resource_id.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "subscriptions" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _extract_log_categories(setting):
    """Extract enabled log category names from a diagnostic setting."""
    categories = []
    if setting.logs:
        for log in setting.logs:
            if log.enabled:
                # category_group (e.g. "allLogs") or individual category
                name = log.category or log.category_group or ""
                if name:
                    categories.append(name)
    return categories


def _extract_destinations(settings_list):
    """Extract destination info from a list of diagnostic settings.

    Returns (destinations, is_duplicate) where destinations is a list of dicts
    and is_duplicate is True if the same destination type (e.g. Log Analytics)
    appears across multiple settings -- meaning logs are being shipped to
    multiple places and the org is paying for it more than once.

    Each destination dict contains:
        setting_name: name of the diagnostic setting
        type: destination type (Log Analytics, Storage Account, etc.)
        name: human-readable destination resource name
        id: full Azure resource ID of the destination
        log_categories: list of enabled log category names
        la_destination_type: "Dedicated" or "AzureDiagnostics" (Log Analytics only)
    """
    destinations = []
    type_counts = {}

    for setting in settings_list:
        setting_name = setting.name or ""
        log_cats = _extract_log_categories(setting)
        la_dest_type = getattr(setting, "log_analytics_destination_type", None) or ""

        if setting.workspace_id:
            type_counts["Log Analytics"] = type_counts.get("Log Analytics", 0) + 1
            destinations.append({
                "setting_name": setting_name,
                "type": "Log Analytics",
                "name": _dest_name_from_id(setting.workspace_id),
                "id": setting.workspace_id,
                "log_categories": log_cats,
                "la_destination_type": la_dest_type,
            })

        if setting.storage_account_id:
            type_counts["Storage Account"] = type_counts.get("Storage Account", 0) + 1
            destinations.append({
                "setting_name": setting_name,
                "type": "Storage Account",
                "name": _dest_name_from_id(setting.storage_account_id),
                "id": setting.storage_account_id,
                "log_categories": log_cats,
                "la_destination_type": "",
            })

        if setting.event_hub_authorization_rule_id or setting.event_hub_name:
            eh_id = setting.event_hub_authorization_rule_id or setting.event_hub_name
            type_counts["Event Hub"] = type_counts.get("Event Hub", 0) + 1
            destinations.append({
                "setting_name": setting_name,
                "type": "Event Hub",
                "name": _dest_name_from_id(eh_id),
                "id": eh_id,
                "log_categories": log_cats,
                "la_destination_type": "",
            })

        if setting.marketplace_partner_id:
            type_counts["Partner Solution"] = type_counts.get("Partner Solution", 0) + 1
            destinations.append({
                "setting_name": setting_name,
                "type": "Partner Solution",
                "name": _dest_name_from_id(setting.marketplace_partner_id),
                "id": setting.marketplace_partner_id,
                "log_categories": log_cats,
                "la_destination_type": "",
            })

    duplicate = any(count > 1 for count in type_counts.values())

    return destinations, duplicate


def _check_single_resource(credential, resource, subscription_id, subscription_name):
    """Check diagnostic settings for a single resource. Returns a list of DiagnosticResults."""
    resource_id = resource["id"]
    resource_name = resource["name"]
    resource_type = resource["type"]
    resource_location = resource.get("location", "")
    resource_group = _resource_group_from_id(resource_id)
    results = []

    # Build list of resource URIs to check (storage accounts have sub-services)
    uris = [resource_id]
    if resource_type.lower() == "microsoft.storage/storageaccounts":
        for svc in ("blobServices/default", "queueServices/default",
                     "tableServices/default", "fileServices/default"):
            uris.append(f"{resource_id}/{svc}")

    client = _get_monitor_client(credential, subscription_id)

    # Common fields for all results from this resource
    common = {
        "resource_type": resource_type,
        "resource_group": resource_group,
        "resource_location": resource_location,
        "subscription_id": subscription_id,
        "subscription_name": subscription_name,
    }

    for uri in uris:
        # Use sub-service name if applicable
        if uri == resource_id:
            display_name = resource_name
        else:
            svc_label = uri.split("/")[-2].replace("Services", "")
            display_name = f"{resource_name}/{svc_label}"

        try:
            settings_list = list(
                client.diagnostic_settings.list(resource_uri=uri)
            )

            if not settings_list:
                results.append(DiagnosticResult(
                    resource_id=uri,
                    resource_name=display_name,
                    status="Missing",
                    **common,
                ))
            else:
                destinations, duplicate = _extract_destinations(settings_list)
                results.append(DiagnosticResult(
                    resource_id=uri,
                    resource_name=display_name,
                    status="Enabled",
                    destinations=destinations,
                    duplicate=duplicate,
                    **common,
                ))

        except HttpResponseError as e:
            if _is_not_supported_error(e):
                results.append(DiagnosticResult(
                    resource_id=uri,
                    resource_name=display_name,
                    status="Not Supported",
                    **common,
                ))
            else:
                results.append(DiagnosticResult(
                    resource_id=uri,
                    resource_name=display_name,
                    status="Error",
                    error_message=str(e)[:200],
                    **common,
                ))
        except Exception as e:
            results.append(DiagnosticResult(
                resource_id=uri,
                resource_name=display_name,
                status="Error",
                error_message=str(e)[:200],
                **common,
            ))

    return results


def _resolve_destination_regions(credential, results):
    """Resolve the region/location for each unique destination resource.

    Collects all unique destination resource IDs, groups by subscription,
    and uses the ResourceManagementClient to look up each resource's location.
    Updates destination dicts in-place with a 'region' key.
    """
    from azure.mgmt.resource import ResourceManagementClient

    # Collect unique destination IDs grouped by subscription
    dest_ids_by_sub = {}
    for r in results:
        for d in r.destinations:
            did = d.get("id", "")
            if not did:
                continue
            sub_id = _subscription_from_id(did)
            if sub_id:
                dest_ids_by_sub.setdefault(sub_id, set()).add(did)

    if not dest_ids_by_sub:
        return

    # Resolve each destination resource
    resolved = {}
    for sub_id, dest_ids in dest_ids_by_sub.items():
        try:
            client = ResourceManagementClient(credential, sub_id,
                                              **_retry_policy_kwargs())
        except Exception:
            continue

        for did in dest_ids:
            try:
                resource = client.resources.get_by_id(did, api_version="2021-04-01")
                resolved[did] = getattr(resource, "location", "") or ""
            except Exception:
                resolved[did] = ""

    # Write resolved regions back into destination dicts
    for r in results:
        for d in r.destinations:
            d["region"] = resolved.get(d.get("id", ""), "")

    total = len(resolved)
    if total:
        sys.stderr.write(f"Resolved regions for {total} destination resources\n")
        sys.stderr.flush()


def check_all_diagnostics(credential, subscription_id, subscription_name, resources,
                          max_workers=10):
    """Check diagnostic settings for all resources using a thread pool.

    Displays a live progress line on the terminal.
    Returns a list of DiagnosticResult objects.
    """
    all_results = []
    total = len(resources)
    completed = 0
    lock = threading.Lock()

    def progress(resource_name):
        nonlocal completed
        with lock:
            completed += 1
            sys.stderr.write(
                f"\r[{completed:>{len(str(total))}}/{total}] "
                f"Checking diagnostics... {resource_name[:60]:<60}"
            )
            sys.stderr.flush()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_resource = {
            executor.submit(
                _check_single_resource, credential, resource,
                subscription_id, subscription_name
            ): resource
            for resource in resources
        }

        for future in as_completed(future_to_resource):
            resource = future_to_resource[future]
            try:
                results = future.result()
                all_results.extend(results)
            except Exception as e:
                all_results.append(DiagnosticResult(
                    resource_id=resource["id"],
                    resource_name=resource["name"],
                    resource_type=resource["type"],
                    resource_group=_resource_group_from_id(resource["id"]),
                    resource_location=resource.get("location", ""),
                    subscription_id=subscription_id,
                    subscription_name=subscription_name,
                    status="Error",
                    error_message=str(e)[:200],
                ))
            progress(resource["name"])

    # Clear the progress line
    sys.stderr.write("\r" + " " * 80 + "\r")
    sys.stderr.flush()

    # Resolve destination resource regions
    sys.stderr.write("Resolving destination resource regions...\n")
    sys.stderr.flush()
    _resolve_destination_regions(credential, all_results)

    return all_results
