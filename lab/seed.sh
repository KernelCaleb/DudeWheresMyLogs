#!/usr/bin/env bash
# Create the intentional drift the lab needs: delete the "doomed" workspace
# so the diagnostic setting on kvdead* becomes a dead destination.
# --force permanently deletes (skips the 14-day soft-delete) so ARM returns
# 404 for the workspace immediately.
# Usage: ./seed.sh <subscription-id> [resource-group]
set -euo pipefail

SUB="${1:?usage: seed.sh <subscription-id> [resource-group]}"
RG="${2:-rg-dwml-lab}"

echo "Deleting law-dwml-doomed (permanently) to create the dead-destination finding"
az monitor log-analytics workspace delete \
  --resource-group "$RG" \
  --workspace-name "law-dwml-doomed" \
  --subscription "$SUB" \
  --force true --yes --output none

echo "Seeded. The lab is ready to scan."
