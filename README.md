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
- HTML report with filtering, sorting, and color-coded status rows
- CSV export for further analysis

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

# Output as CSV instead of HTML
DudeWheresMyLogs -f csv

# Specify output file
DudeWheresMyLogs -o report.html

# Adjust parallel workers (default: 10)
DudeWheresMyLogs -w 20
```

### Options

| Flag | Description |
|------|-------------|
| `-s`, `--subscription` | Subscription ID to scan (repeatable) |
| `-a`, `--all` | Scan all accessible subscriptions |
| `-f`, `--format` | Output format: `html` (default) or `csv` |
| `-o`, `--output` | Output file path (auto-generated if omitted) |
| `-w`, `--workers` | Number of parallel workers (default: 10) |
| `--version` | Show version and exit |

### Tips for large environments
- Use `-w 20` (or higher) to increase parallelism when scanning thousands of resources. Stay at or below 20 to avoid excessive ARM throttling.
- The tool automatically retries throttled requests (HTTP 429) with exponential backoff, so it will work through rate limits without failing.
- Subscriptions are scanned sequentially, which naturally spreads API load across subscription-level throttle buckets.

## Output
- **HTML report** (default): Self-contained file with summary cards, collapsible sections grouped by subscription and resource type (Missing, Duplicate, Healthy, Not Supported/Errors), per-resource diagnostic detail, and a destination map showing all resources streaming to each destination.
- **CSV report** (`-f csv`): Flat export with subscription, resource, status, destination, and duplicate flag columns.

## License
MIT
