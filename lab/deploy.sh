#!/usr/bin/env bash
# Deploy the DudeWheresMyLogs verification lab.
# Usage: ./deploy.sh <subscription-id> [resource-group] [location]
set -euo pipefail

SUB="${1:?usage: deploy.sh <subscription-id> [resource-group] [location]}"
RG="${2:-rg-dwml-lab}"
LOCATION="${3:-eastus}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Deploying lab to subscription $SUB, resource group $RG ($LOCATION)"
az group create --name "$RG" --location "$LOCATION" --subscription "$SUB" --output none
az deployment group create \
  --resource-group "$RG" \
  --subscription "$SUB" \
  --template-file "$SCRIPT_DIR/main.bicep" \
  --parameters location="$LOCATION" \
  --output none

echo "Deployed. Now run seed.sh to create the intentional drift (dead destination)."
