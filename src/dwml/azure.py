import sys

from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.subscription import SubscriptionClient


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
    client = ResourceManagementClient(credential, subscription_id,
                                      **_retry_policy_kwargs())
    resources = []
    for resource in client.resources.list():
        resources.append({
            "id": resource.id,
            "name": resource.name,
            "type": resource.type,
            "location": resource.location,
        })
    return resources
