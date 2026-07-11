# DudeWheresMyLogs

## Overview
DudeWheresMyLogs is a Python CLI tool that audits Azure diagnostic logging configurations across subscriptions. It finds resources missing diagnostic settings, detects duplicate log shipping, and shows where logs are being sent -- helping organizations cut wasted spend on redundant logging.

## Features
- Scan all resources across one or more Azure subscriptions
- Identify resources without diagnostic logging enabled
- Detect duplicate log shipping (same destination type sending to two or more different destinations)
- Detect dead destinations (diagnostic settings still shipping logs to deleted workspaces, storage accounts, or event hubs)
- Flag cross-region log shipping (egress cost and data-residency concern)
- Workspace usage analysis: flag destination workspaces nobody has queried in the lookback window, and workspaces where query auditing is disabled so usage cannot be assessed (needs Log Analytics Reader on the workspaces; degrades gracefully without it)
- Ingestion liveness reconciliation: compare resources *configured* to ship against the `_ResourceId`s actually present in each workspace, flagging "configured but silent" pipelines (advisory: an idle resource legitimately emits nothing)
- Map log destinations (Log Analytics, Storage Accounts, Event Hubs, Partner Solutions)
- Storage account sub-service scanning (blob, queue, table, file)
- Parallel scanning with configurable worker count
- Retry with exponential backoff for Azure ARM throttling (enterprise-scale ready)
- HTML audit report with numbered sections, collapsible groupings, in-page anchor links, and an embedded machine-readable JSON payload
- CSV export for further analysis
- Markdown export for pasting findings into tickets, PRs, and chat
- JSON export for automation, diffing, and CI workflows
- Resource filtering by type and resource group
- CI mode with meaningful exit codes and configurable finding categories (`--fail-on`)

## Prerequisites
- Python 3.9+
- ARM `Reader` on the scanned subscriptions. Workspace usage analysis additionally needs data-plane query access on destination workspaces (`Log Analytics Reader`); without it those workspaces report as unknown rather than failing the scan
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
# Interactive mode -- pick one or more subscriptions from a list (e.g. 0,2,5)
DudeWheresMyLogs

# Scan all accessible subscriptions (non-interactive)
DudeWheresMyLogs -a

# Scan specific subscriptions
DudeWheresMyLogs -s <subscription-id>
DudeWheresMyLogs -s <sub-1> -s <sub-2>

# Output as CSV, JSON, or Markdown instead of HTML
DudeWheresMyLogs -f csv
DudeWheresMyLogs -f json
DudeWheresMyLogs -f md

# Specify output file
DudeWheresMyLogs -o report.html

# Adjust parallel workers (default: 10)
DudeWheresMyLogs -w 20

# Scope the scan to specific resources
DudeWheresMyLogs --include-types "Microsoft.KeyVault/*" --resource-group "prod-*"

# Use CI-friendly exit codes: 0=clean, 1=findings, 2=errors
DudeWheresMyLogs -a --ci

# Choose which finding categories fail a CI scan
DudeWheresMyLogs -a --ci --fail-on duplicates,dead-destinations

# Keep reports small in large tenants: counts only for healthy/informational
DudeWheresMyLogs -a --summary-only
```

### Options

| Flag | Description |
|------|-------------|
| `-s`, `--subscription` | Subscription ID to scan (repeatable) |
| `-a`, `--all` | Scan all accessible subscriptions |
| `-f`, `--format` | Output format: `html` (default), `csv`, `json`, or `md` |
| `-o`, `--output` | Output file path (auto-generated if omitted) |
| `-w`, `--workers` | Number of parallel workers (default: 10) |
| `--include-types` | Only scan matching resource types (supports wildcards, repeatable) |
| `--exclude-types` | Skip matching resource types (supports wildcards, repeatable) |
| `--resource-group` | Only scan matching resource groups (supports wildcards, repeatable) |
| `--ci` | Return `0` for clean, `1` for findings, `2` for scan errors |
| `--checks` | Which finding checks to run and report: `missing`, `duplicates`, `dead-destinations`, `cross-region`, `silent-resources`, `unqueried-workspaces`, `no-query-auditing` (default: all). Raw scan data in CSV/JSON is unaffected |
| `--fail-on` | Finding categories that trigger exit code `1` in `--ci` mode; must be active checks (default: `missing,duplicates,dead-destinations`) |
| `--lookback-days` | Lookback window for workspace usage analysis (default: 30) |
| `--summary-only` | Omit per-resource detail for healthy/informational sections in HTML and Markdown reports |
| `--version` | Show version and exit |

### Tips for large environments
- Use `-w 20` (or higher) to increase parallelism when scanning thousands of resources. Stay at or below 20 to avoid excessive ARM throttling.
- The tool automatically retries throttled requests (HTTP 429) with exponential backoff, so it will work through rate limits without failing.
- Subscriptions are scanned sequentially, which naturally spreads API load across subscription-level throttle buckets.

## Output
- **HTML report** (default): Self-contained file with a findings overview, scope block, and seven numbered sections (Missing Diagnostics, Duplicate Shipping, Dead Destinations, Cross-Region Shipping, Healthy Resources, Informational, Destination Map). Findings are grouped by subscription and resource group; each resource expands inline to show its full ID, configured destinations, and log categories. Sections have stable anchor IDs (`#missing`, `#duplicate`, `#dead`, `#cross-region`, `#healthy`, `#informational`, `#destinations`) for sharing direct links. The full machine-readable payload is embedded in a `<script type="application/json" id="dwml-data">` block, so any saved report can be re-parsed or diffed later without re-scanning.
- **CSV report** (`-f csv`): Flat export with subscription, resource, status, destination, duplicate, dead destination, and cross-region columns.
- **JSON report** (`-f json`): Structured export with summary metadata and full per-resource detail for automation and comparisons.
- **Markdown report** (`-f md`): Findings-focused tables (missing, duplicates, dead destinations, cross-region) ready to paste into tickets, PRs, or chat.

## License
MIT
