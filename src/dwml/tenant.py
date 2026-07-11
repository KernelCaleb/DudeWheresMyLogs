"""Subscription-level blind spot checks (v2.4).

The Azure Activity Log records every control-plane operation in a
subscription -- who created, changed, or deleted what. Azure retains it for
only 90 days with no export configured. A subscription without an Activity
Log export (subscription-scope diagnostic settings) has no durable audit
trail and nothing feeding a SIEM.

Notably, Activity Log ingestion into Log Analytics is free, so there is
rarely a good reason not to export it.
"""
import sys
from dataclasses import dataclass, field

try:
    from azure.mgmt.monitor import MonitorManagementClient
except ModuleNotFoundError:  # pragma: no cover - keeps unit tests importable without Azure SDK
    MonitorManagementClient = None

from .azure import _retry_policy_kwargs
from .diagnostics import _dest_name_from_id

# The security-relevant Activity Log categories worth exporting
CORE_CATEGORIES = ("Administrative", "Security", "Policy")


@dataclass
class SubscriptionAudit:
    """Activity Log export state for one subscription."""
    subscription_id: str
    subscription_name: str
    exported: object = None      # True/False, None = could not determine
    destinations: list = field(default_factory=list)  # [{type, name, id}]
    categories: list = field(default_factory=list)    # enabled categories
    missing_core: list = field(default_factory=list)  # CORE_CATEGORIES not enabled
    error: str = ""


def _setting_destinations(setting):
    """Extract destination summaries from a subscription diagnostic setting."""
    destinations = []
    if getattr(setting, "workspace_id", None):
        destinations.append({"type": "Log Analytics",
                             "name": _dest_name_from_id(setting.workspace_id),
                             "id": setting.workspace_id})
    if getattr(setting, "storage_account_id", None):
        destinations.append({"type": "Storage Account",
                             "name": _dest_name_from_id(setting.storage_account_id),
                             "id": setting.storage_account_id})
    eh = (getattr(setting, "event_hub_authorization_rule_id", None)
          or getattr(setting, "event_hub_name", None))
    if eh:
        destinations.append({"type": "Event Hub",
                             "name": _dest_name_from_id(eh), "id": eh})
    if getattr(setting, "marketplace_partner_id", None):
        destinations.append({"type": "Partner Solution",
                             "name": _dest_name_from_id(setting.marketplace_partner_id),
                             "id": setting.marketplace_partner_id})
    return destinations


def audit_from_settings(sub_id, sub_name, settings_list):
    """Build a SubscriptionAudit from subscription diagnostic settings.

    Exported means: at least one setting has at least one enabled category
    AND at least one destination.
    """
    audit = SubscriptionAudit(subscription_id=sub_id, subscription_name=sub_name)
    enabled_categories = set()

    for setting in settings_list:
        destinations = _setting_destinations(setting)
        setting_categories = {
            log.category for log in (setting.logs or [])
            if log.enabled and log.category
        }
        if destinations and setting_categories:
            audit.destinations.extend(
                d for d in destinations if d not in audit.destinations)
            enabled_categories.update(setting_categories)

    audit.exported = bool(enabled_categories)
    audit.categories = sorted(enabled_categories)
    audit.missing_core = [c for c in CORE_CATEGORIES if c not in enabled_categories]
    return audit


def audit_subscription(credential, sub_id, sub_name):
    """Check one subscription's Activity Log export configuration."""
    if MonitorManagementClient is None:
        raise RuntimeError("azure-mgmt-monitor is required for the activity log check")

    audit = SubscriptionAudit(subscription_id=sub_id, subscription_name=sub_name)
    try:
        client = MonitorManagementClient(credential, sub_id, **_retry_policy_kwargs())
        settings_list = list(client.subscription_diagnostic_settings.list())
    except Exception as e:
        audit.error = str(e)[:120]
        return audit

    result = audit_from_settings(sub_id, sub_name, settings_list)
    sys.stderr.write(
        f"Activity Log export for {sub_name}: "
        f"{'configured' if result.exported else 'NOT configured'}\n")
    sys.stderr.flush()
    return result
