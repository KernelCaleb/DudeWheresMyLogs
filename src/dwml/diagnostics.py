import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

try:
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
    from azure.mgmt.monitor import MonitorManagementClient
except ModuleNotFoundError:  # pragma: no cover - keeps unit tests importable without Azure SDK
    class HttpResponseError(Exception):
        """Fallback exception used when Azure SDK is unavailable."""

    class ResourceNotFoundError(HttpResponseError):
        """Fallback exception used when Azure SDK is unavailable."""

    MonitorManagementClient = None

from .azure import _resource_group_from_id, _retry_policy_kwargs


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


def has_dead_destination(result):
    """True if any destination of this result no longer exists in Azure."""
    return any(d.get("not_found") for d in result.destinations)


def has_cross_region(result):
    """True if any destination of this result lives in a different region."""
    return any(d.get("cross_region") for d in result.destinations)


# Thread-local storage for MonitorManagementClient instances
_thread_local = threading.local()


def _get_monitor_client(credential, subscription_id):
    """Get or create a thread-local MonitorManagementClient."""
    if MonitorManagementClient is None:
        raise RuntimeError("azure-mgmt-monitor is required to scan diagnostic settings")

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
    appears with two or more *different* destination IDs -- meaning logs are
    being shipped to multiple places of the same kind and the org is paying
    for it more than once.  Multiple settings pointing at the *same*
    destination are not flagged: Azure deduplicates those at the platform
    level, and splitting categories across settings is a legitimate pattern.

    Each destination dict contains:
        setting_name: name of the diagnostic setting
        type: destination type (Log Analytics, Storage Account, etc.)
        name: human-readable destination resource name
        id: full Azure resource ID of the destination
        log_categories: list of enabled log category names
        la_destination_type: "Dedicated" or "AzureDiagnostics" (Log Analytics only)
    """
    destinations = []
    ids_by_type = {}

    for setting in settings_list:
        setting_name = setting.name or ""
        log_cats = _extract_log_categories(setting)
        la_dest_type = getattr(setting, "log_analytics_destination_type", None) or ""

        if setting.workspace_id:
            ids_by_type.setdefault("Log Analytics", set()).add(setting.workspace_id.lower())
            destinations.append({
                "setting_name": setting_name,
                "type": "Log Analytics",
                "name": _dest_name_from_id(setting.workspace_id),
                "id": setting.workspace_id,
                "log_categories": log_cats,
                "la_destination_type": la_dest_type,
            })

        if setting.storage_account_id:
            ids_by_type.setdefault("Storage Account", set()).add(setting.storage_account_id.lower())
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
            ids_by_type.setdefault("Event Hub", set()).add(eh_id.lower())
            destinations.append({
                "setting_name": setting_name,
                "type": "Event Hub",
                "name": _dest_name_from_id(eh_id),
                "id": eh_id,
                "log_categories": log_cats,
                "la_destination_type": "",
            })

        if setting.marketplace_partner_id:
            ids_by_type.setdefault("Partner Solution", set()).add(setting.marketplace_partner_id.lower())
            destinations.append({
                "setting_name": setting_name,
                "type": "Partner Solution",
                "name": _dest_name_from_id(setting.marketplace_partner_id),
                "id": setting.marketplace_partner_id,
                "log_categories": log_cats,
                "la_destination_type": "",
            })

    duplicate = any(len(ids) > 1 for ids in ids_by_type.values())

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


def _norm_region(value):
    """Normalize an Azure region name for comparison ('East US' -> 'eastus')."""
    return (value or "").replace(" ", "").lower()


# Used only when provider metadata lookup fails; valid for Microsoft.Resources
# but typically rejected by other providers, in which case region stays "".
_FALLBACK_API_VERSION = "2021-04-01"


def _provider_type_from_id(resource_id):
    """Parse (namespace, resource_type) from a resource ID.

    e.g. .../providers/Microsoft.EventHub/namespaces/ns1/authorizationRules/r1
    -> ("Microsoft.EventHub", "namespaces/authorizationRules")
    """
    parts = [p for p in resource_id.split("/") if p]
    idx = next((i for i, p in enumerate(parts) if p.lower() == "providers"), None)
    if idx is None or idx + 2 >= len(parts):
        return "", ""
    namespace = parts[idx + 1]
    type_segments = parts[idx + 2::2]
    return namespace, "/".join(type_segments)


def _lookup_api_version(client, namespace, resource_type):
    """Find a usable API version for a resource type via provider metadata.

    get_by_id requires an API version valid for the *destination's* provider,
    not ARM's own. Prefers the newest stable version.
    """
    try:
        provider = client.providers.get(namespace)
        for rt in provider.resource_types or []:
            if (rt.resource_type or "").lower() == resource_type.lower():
                versions = rt.api_versions or []
                stable = [v for v in versions if "preview" not in v.lower()]
                if stable or versions:
                    return (stable or versions)[0]
    except Exception:
        pass
    return _FALLBACK_API_VERSION


def _resolve_destination_regions(credential, results, max_workers=10):
    """Resolve the region/location for each unique destination resource.

    Collects all unique destination resource IDs, groups by subscription,
    and uses the ResourceManagementClient to look up each resource's location
    in parallel.  Updates destination dicts in-place with:
        region: the destination resource's region ("" if unresolvable)
        not_found: True if the destination no longer exists (404) --
                   the diagnostic setting is shipping logs to a dead end
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

    # Resolve each destination resource in parallel
    resolved = {}
    lock = threading.Lock()

    def _resolve(client, did, api_version):
        try:
            resource = client.resources.get_by_id(did, api_version=api_version)
            info = {"region": getattr(resource, "location", "") or "",
                    "not_found": False}
        except ResourceNotFoundError:
            # Destination was deleted but the diagnostic setting still points at it
            info = {"region": "", "not_found": True}
        except Exception:
            # No access, unusable API version, etc. -- unknown, don't flag
            info = {"region": "", "not_found": False}
        with lock:
            resolved[did] = info

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for sub_id, dest_ids in dest_ids_by_sub.items():
            try:
                client = ResourceManagementClient(credential, sub_id,
                                                  **_retry_policy_kwargs())
            except Exception:
                continue

            # Resolve API versions serially first: one provider-metadata call
            # per unique resource type, shared by all lookups in this sub
            api_versions = {}
            for did in dest_ids:
                namespace, resource_type = _provider_type_from_id(did)
                key = (namespace.lower(), resource_type.lower())
                if key not in api_versions:
                    api_versions[key] = _lookup_api_version(
                        client, namespace, resource_type)

            for did in dest_ids:
                namespace, resource_type = _provider_type_from_id(did)
                api_version = api_versions[(namespace.lower(), resource_type.lower())]
                executor.submit(_resolve, client, did, api_version)

    # Write resolved info back into destination dicts
    for r in results:
        for d in r.destinations:
            info = resolved.get(d.get("id", ""), {"region": "", "not_found": False})
            d["region"] = info["region"]
            d["not_found"] = info["not_found"]

    total = len(resolved)
    if total:
        sys.stderr.write(f"Resolved regions for {total} destination resources\n")
        sys.stderr.flush()


def _flag_cross_region(results):
    """Mark destinations whose region differs from the source resource's region.

    Skips resources in the 'global' pseudo-region and destinations whose
    region could not be resolved.
    """
    for r in results:
        src = _norm_region(r.resource_location)
        for d in r.destinations:
            dest = _norm_region(d.get("region", ""))
            d["cross_region"] = bool(
                src and dest and src != "global" and dest != src
            )


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

    # Resolve destination resource regions and derive findings from them
    sys.stderr.write("Resolving destination resource regions...\n")
    sys.stderr.flush()
    _resolve_destination_regions(credential, all_results, max_workers=max_workers)
    _flag_cross_region(all_results)

    return all_results
