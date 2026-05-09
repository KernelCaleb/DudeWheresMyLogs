import sys

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.resource import ResourceManagementClient
    from azure.mgmt.subscription import SubscriptionClient
except ModuleNotFoundError:  # pragma: no cover - keeps unit tests importable without Azure SDK
    DefaultAzureCredential = None
    ResourceManagementClient = None
    SubscriptionClient = None


def _resource_group_from_id(resource_id):
    """Extract the resource group name from an Azure resource ID."""
    parts = resource_id.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _retry_policy_kwargs():
    """Return kwargs that configure a retry policy tuned for enterprise-scale scanning.

    Azure ARM enforces read throttling (~12,000 requests per 5 minutes per
    subscription).  The default SDK retry policy handles 429s but gives up
    quickly.  These settings allow the scanner to ride out throttling bursts
    when scanning thousands of resources.
    """
    return {
        "retry_total": 10,
        "retry_backoff_factor": 1,
        "retry_backoff_max": 60,
        "retry_on_status_codes": [429, 500, 502, 503, 504],
    }


def get_credential():
    """Authenticate using DefaultAzureCredential with eager validation."""
    if DefaultAzureCredential is None:
        print("Authentication failed: Azure SDK dependencies are not installed.")
        print("Install the project requirements or run inside the configured virtual environment.")
        sys.exit(1)

    try:
        credential = DefaultAzureCredential()
        # Force a token fetch to validate credentials early
        credential.get_token("https://management.azure.com/.default")
        return credential
    except Exception as e:
        print(f"Authentication failed: {e}")
        print("Ensure you are logged in via 'az login', have a managed identity, "
              "or have set AZURE_CLIENT_ID/AZURE_TENANT_ID/AZURE_CLIENT_SECRET.")
        sys.exit(1)


def list_subscriptions(credential):
    """List all accessible Azure subscriptions."""
    if SubscriptionClient is None:
        raise RuntimeError("azure-mgmt-subscription is required to list subscriptions")

    client = SubscriptionClient(credential, **_retry_policy_kwargs())
    subscriptions = []
    for sub in client.subscriptions.list():
        subscriptions.append({
            "id": sub.subscription_id,
            "name": sub.display_name,
        })
    return subscriptions


def list_resources(credential, subscription_id):
    """List all resources in a subscription."""
    if ResourceManagementClient is None:
        raise RuntimeError("azure-mgmt-resource is required to list resources")

    client = ResourceManagementClient(credential, subscription_id,
                                      **_retry_policy_kwargs())
    resources = []
    for resource in client.resources.list():
        resources.append({
            "id": resource.id,
            "name": resource.name,
            "type": resource.type,
            "location": resource.location,
            "resource_group": _resource_group_from_id(resource.id),
        })
    return resources
