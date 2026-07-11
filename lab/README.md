# Verification Lab

A deliberately misconfigured, near-zero-cost Azure environment where every
finding category the tool claims to detect has a known-expected instance.
Use it to verify changes end-to-end against real ARM behavior instead of
mocks.

## Cost

Negligible: Log Analytics workspaces bill per GB ingested (near zero here),
Key Vaults and NSGs are free, the storage account is pennies. Tear it down
when done anyway.

## Usage

```bash
./deploy.sh <subscription-id>     # create rg-dwml-lab and all resources
./seed.sh <subscription-id>       # create intentional drift (deletes the doomed workspace)
DudeWheresMyLogs -s <subscription-id> --resource-group rg-dwml-lab --ci
./teardown.sh <subscription-id>   # destroy everything, purge soft-deleted remnants
```

## Expected findings

| Resource | Expected result |
|----------|----------------|
| `kvmiss*` | Missing Diagnostics |
| `nsg-dwml-missing` | Missing Diagnostics |
| `kvdup*` | Duplicate Shipping (two different workspaces) |
| `kvdead*` | Dead Destination (setting points at the deleted `law-dwml-doomed`) |
| `kvcross*` | Cross-Region Shipping (eastus vault -> `law-dwml-west` in westus2) |
| `kvok*` | Healthy -- negative control: two settings to the SAME workspace split by category; must NOT be flagged duplicate |
| `stdwml*` | blob sub-service Enabled; queue/table/file sub-services Missing |
| `law-dwml-primary` | Workspace Usage: Unqueried until a real (unmarked) query lands in LAQueryLogs (~5-15 min ingestion lag), then Active. The tool's own queries are self-excluded via the dwml-usage-check marker |
| `law-dwml-secondary` | Unqueried Workspaces finding (auditing on, never queried) |
| `law-dwml-west` | No Query Auditing finding (Audit category not enabled) |
| `stdwml*/blob` | Configured But Silent: blob diagnostics enabled but no blob operations ever occur, so no data arrives |

For silent-resource verification: generate activity on some vaults (e.g.
`az keyvault secret set`) and wait out the ~5-15 min ingestion lag; touched
vaults flip to flowing while the blob service stays silent. Note KV
AuditEvent includes control-plane operations (VaultGet from `az keyvault
list`), so merely listing vaults makes them look active.

A full lab scan in `--ci` mode must exit `1` (findings). Scoping to
`--include-types "Microsoft.Network/*" --fail-on duplicates` must exit `0`.

## Notes

- Deleting the resource group alone leaves workspaces soft-deleted for 14
  days and Key Vault names reserved; `teardown.sh` force-purges both.
- `seed.sh` exists because the dead-destination finding *is* drift: it can't
  be expressed in the template, only done to it afterwards.
