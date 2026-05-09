# DudeWheresMyLogs

## Overview
DudeWheresMyLogs is a Python CLI tool that audits Azure diagnostic logging configurations across subscriptions. It finds resources missing diagnostic settings, detects duplicate log shipping, and shows where logs are being sent -- helping organizations cut wasted spend on redundant logging.

## Features
- Scan all resources across one or more Azure subscriptions
- Identify resources without diagnostic logging enabled
- Detect duplicate logging configurations (same destination configured multiple times)
- Map log destinations (Log Analytics, Storage Accounts, Event Hubs, Partner Solutions)
- Storage account sub-service scanning (blob, queue, table, file)
- Parallel scanning with configurable worker count
- Retry with exponential backoff for Azure ARM throttling (enterprise-scale ready)
- HTML audit report with numbered sections, collapsible groupings, and in-page anchor links
- CSV export for further analysis
- JSON export for automation, diffing, and CI workflows
- Resource filtering by type and resource group
- CI mode with meaningful exit codes for scheduled scans

## Prerequisites
- Python 3.9+
- Azure credentials (any method supported by [DefaultAzureCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential))
  - `az login` (Azure CLI)
  - Managed Identity (VMs, App Service, etc.)
  - Service Principal via environment variables (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`)

## Installation

```bash
git clone https://github.com/KernelCaleb/DudeWheresMyLogs.git
cd DudeWheresMyLogs
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

After installation, the `DudeWheresMyLogs` command is available inside the virtual environment. Run `source .venv/bin/activate` to re-enter the venv in future shell sessions.

For development:
```bash
pip install -e .
```

## Usage

```bash
# Interactive mode -- pick subscriptions from a list
DudeWheresMyLogs

# Scan all accessible subscriptions (non-interactive)
DudeWheresMyLogs -a

# Scan specific subscriptions
DudeWheresMyLogs -s <subscription-id>
DudeWheresMyLogs -s <sub-1> -s <sub-2>

# Output as CSV or JSON instead of HTML
DudeWheresMyLogs -f csv
DudeWheresMyLogs -f json

# Specify output file
DudeWheresMyLogs -o report.html

# Adjust parallel workers (default: 10)
DudeWheresMyLogs -w 20

# Scope the scan to specific resources
DudeWheresMyLogs --include-types "Microsoft.KeyVault/*" --resource-group "prod-*"

# Use CI-friendly exit codes: 0=clean, 1=findings, 2=errors
DudeWheresMyLogs -a --ci
```

### Options

| Flag | Description |
|------|-------------|
| `-s`, `--subscription` | Subscription ID to scan (repeatable) |
| `-a`, `--all` | Scan all accessible subscriptions |
| `-f`, `--format` | Output format: `html` (default), `csv`, or `json` |
| `-o`, `--output` | Output file path (auto-generated if omitted) |
| `-w`, `--workers` | Number of parallel workers (default: 10) |
| `--include-types` | Only scan matching resource types (supports wildcards, repeatable) |
| `--exclude-types` | Skip matching resource types (supports wildcards, repeatable) |
| `--resource-group` | Only scan matching resource groups (supports wildcards, repeatable) |
| `--ci` | Return `0` for clean, `1` for findings, `2` for scan errors |
| `--version` | Show version and exit |

### Tips for large environments
- Use `-w 20` (or higher) to increase parallelism when scanning thousands of resources. Stay at or below 20 to avoid excessive ARM throttling.
- The tool automatically retries throttled requests (HTTP 429) with exponential backoff, so it will work through rate limits without failing.
- Subscriptions are scanned sequentially, which naturally spreads API load across subscription-level throttle buckets.

## Output
- **HTML report** (default): Self-contained file with a findings overview, scope block, and five numbered sections (Missing Diagnostics, Duplicate Shipping, Healthy Resources, Informational, Destination Map). Findings are grouped by subscription and resource group; each resource expands inline to show its full ID, configured destinations, and log categories. Sections have stable anchor IDs (`#missing`, `#duplicate`, `#healthy`, `#informational`, `#destinations`) for sharing direct links.
- **CSV report** (`-f csv`): Flat export with subscription, resource, status, destination, and duplicate flag columns.
- **JSON report** (`-f json`): Structured export with summary metadata and full per-resource detail for automation and comparisons.

## License
MIT
