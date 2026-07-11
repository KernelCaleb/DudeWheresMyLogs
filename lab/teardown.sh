#!/usr/bin/env bash
# Destroy the DudeWheresMyLogs verification lab completely.
# Deletes the resource group, force-purges the workspaces (skip soft-delete),
# and purges the soft-deleted Key Vaults so names are freed and nothing lingers.
# Usage: ./teardown.sh <subscription-id> [resource-group]
set -euo pipefail

SUB="${1:?usage: teardown.sh <subscription-id> [resource-group]}"
RG="${2:-rg-dwml-lab}"

if ! az group exists --name "$RG" --subscription "$SUB" | grep -q true; then
  echo "Resource group $RG not found in subscription $SUB; nothing to tear down."
  exit 0
fi

# Capture names before deletion so we can purge soft-deleted remnants after
KVS=$(az keyvault list --resource-group "$RG" --subscription "$SUB" \
  --query "[].name" --output tsv)
WORKSPACES=$(az monitor log-analytics workspace list --resource-group "$RG" \
  --subscription "$SUB" --query "[].name" --output tsv)

# Permanently delete workspaces first (RG delete alone leaves them
# soft-deleted for 14 days)
for ws in $WORKSPACES; do
  echo "Force-deleting workspace $ws"
  az monitor log-analytics workspace delete --resource-group "$RG" \
    --workspace-name "$ws" --subscription "$SUB" \
    --force true --yes --output none || true
done

echo "Deleting resource group $RG"
az group delete --name "$RG" --subscription "$SUB" --yes --output none

for kv in $KVS; do
  echo "Purging soft-deleted key vault $kv"
  az keyvault purge --name "$kv" --subscription "$SUB" --output none || true
done

echo "Lab destroyed."
