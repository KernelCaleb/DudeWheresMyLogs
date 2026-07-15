# DudeWheresMyLogs

An Azure log health tool. One scan answers the four questions every cloud security and platform team eventually asks about their logging pipeline:

1. **Configured?** Which resources have no diagnostic settings at all, which subscriptions never export their Activity Log, and which settings still ship to destinations that no longer exist.
2. **Flowing?** Which resources are configured to ship logs but whose data never actually arrives at the workspace.
3. **Used?** Which destination workspaces nobody has queried in the last month -- logs paid for and read by no one.
4. **Worth it?** What each workspace costs per month, and what redundant or misrouted shipping is wasting, in dollars.

Organizations routinely pay for logs shipped twice, logs shipped to deleted workspaces, and workspaces no human or detection rule ever reads. DudeWheresMyLogs finds all of it in one pass and puts a list-price dollar figure on the waste.

## Checks

| Check | Question | Fails CI by default |
|---|---|---|
| `missing` | Resources with no diagnostic settings | Yes |
| `duplicates` | Same destination type shipping to two or more different destinations | Yes |
| `dead-destinations` | Settings shipping to deleted workspaces/storage/event hubs | Yes |
| `no-activity-log-export` | Subscriptions whose control-plane audit trail is exported nowhere (Azure keeps it only 90 days; export to Log Analytics is free) | Yes |
| `cross-region` | Destination region differs from resource region (egress cost, data residency) | No |
| `silent-resources` | Configured to ship, but no data arrived in the lookback window | No (advisory) |
| `unqueried-workspaces` | Workspaces receiving logs that nobody queried in the lookback window | No |
| `no-query-auditing` | Workspaces where query auditing is off, so usage cannot be assessed | No |

On top of the checks, every scan maps where logs are going (Log Analytics, Storage, Event Hubs, Partner Solutions) and estimates costs: per-workspace monthly ingestion (by table plan, Sentinel-aware) and retention overage, per-finding monthly waste (redundant duplicate flows, cross-region bandwidth), and destinations subject to the platform log export fee billed since June 2026. All figures are list-price estimates from a configurable price table (`--price-file`), not bills.

## Highlights

- Policy checks: declare your own log health rules in YAML and they become first-class checks -- own report sections, CI categories, and diff support (see below)
- Azure SDK native (no `az` CLI dependency), parallel scanning, automatic retry/backoff on ARM throttling -- built for enterprise-scale tenants
- Graceful degradation everywhere: whatever the credential cannot see is reported as unknown, never guessed and never fatal to the scan
- Self-contained HTML report (light and dark mode) with collapsible findings, a destination map, and the full machine-readable payload embedded inside
- `diff` subcommand: compare any two saved reports -- JSON or HTML, in any combination -- and see new findings, resolved findings, and cost deltas
- CI mode with meaningful exit codes and per-category `--fail-on` control, for scheduled scans that alert only on what you care about
- CSV, JSON, and Markdown exports for analysis, automation, and pasting findings into tickets
- Clean terminal output: progress bars and colors on a TTY, plain text in CI (`NO_COLOR` respected)

## Prerequisites

- Python 3.9+
- ARM `Reader` on the scanned subscriptions. Workspace usage, liveness, and cost analysis additionally need data-plane query access on destination workspaces (`Log Analytics Reader`); without it those workspaces report as unknown rather than failing the scan
- Azure credentials via any method supported by [DefaultAzureCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential):
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

### Scanning

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

# Scope the scan to specific resources
DudeWheresMyLogs --include-types "Microsoft.KeyVault/*" --resource-group "prod-*"

# Longer usage lookback window (default 30 days)
DudeWheresMyLogs --lookback-days 90
```

### Scheduled / CI scans

```bash
# CI-friendly exit codes: 0=clean, 1=findings, 2=errors
DudeWheresMyLogs -a --ci

# Choose which finding categories fail the scan
DudeWheresMyLogs -a --ci --fail-on duplicates,dead-destinations

# Keep reports small in large tenants: counts only for healthy/informational
DudeWheresMyLogs -a --summary-only
```

### Diffing reports

Every JSON report -- and every HTML report, which embeds the same payload -- can be compared against any later one without re-scanning:

```bash
# What changed since last week's scan? (JSON or HTML, in any combination)
DudeWheresMyLogs diff last-week.json today.html

# Markdown diff for a ticket or PR comment
DudeWheresMyLogs diff old.json new.json -f md -o diff.md

# CI regression gate: exit 1 only when NEW findings appeared
DudeWheresMyLogs diff old.json new.json --ci --fail-on missing,dead-destinations
```

The diff shows per-check counts, which findings are new, which were resolved, and how estimated monthly spend and waste moved. Policy rule results recorded in the reports are diffed too, no policy file needed.

### Policy checks

Declare your own rules in a YAML (or JSON) file and they become first-class checks: their own numbered report sections, summary lines, `--checks`/`--fail-on` category names, CI exit-code behavior, and diff support.

```bash
DudeWheresMyLogs -a --policy policies/baseline.yaml
DudeWheresMyLogs -a --ci --policy myrules.yaml --fail-on kv-audit-to-la
```

```yaml
rules:
  - name: kv-audit-to-la
    title: Key Vaults must ship AuditEvent to Log Analytics
    severity: fail            # fail = CI-failing by default; warn/info report only
    match: { type: "Microsoft.KeyVault/vaults" }   # wildcards; also name/resource_group/region/subscription
    require:
      categories: ["AuditEvent"]
      destination_type: "Log Analytics"

  - name: pipelines-must-flow
    severity: warn
    match: { type: "*" }
    require: { flowing: true }     # data must actually arrive, not just be configured

  - name: workspaces-must-be-read
    severity: warn
    scope: workspace
    require: { queried: true }     # someone must have queried it in the lookback window
```

Resource rules can `require` diagnostics, categories (`allLogs` satisfies any), destination type/region (`same` = resource's own region), Log Analytics table mode, and `flowing`; they can `forbid` duplicates, cross-region or dead destinations, silent pipelines, destination types/regions, and the legacy `AzureDiagnostics` mode. Workspace rules can require minimum retention, query auditing, actual query activity, a daily cap, Sentinel on/off, and a maximum estimated monthly cost. Unknowns never violate: whatever the credential could not see is skipped, not flagged.

**How this relates to Azure Policy:** Azure Policy owns enforcement of configuration intent -- deploy-time deny and auto-remediation. These rules assert what Azure Policy structurally cannot evaluate: whether data actually flows, whether anyone reads it, what it costs, and conditions aggregated across multiple diagnostic settings. Configuration-shaped rules are still useful here because this tool runs with plain Reader rights, where Policy assignment is not an option (auditors, consultants, assessments). A starter pack ships in [`policies/baseline.yaml`](policies/baseline.yaml).

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
| `--checks` | Which checks to run and report (see the Checks table, plus any `--policy` rule names; default: all). Raw scan data in CSV/JSON is unaffected |
| `--fail-on` | Finding categories that trigger exit code `1` in `--ci` mode; must be active checks (default: `missing,duplicates,dead-destinations,no-activity-log-export` plus severity-`fail` policy rules) |
| `--policy` | Policy file (YAML or JSON) with custom rules; repeatable. Rules become first-class checks |
| `--lookback-days` | Lookback window for workspace usage analysis (default: 30) |
| `--price-file` | JSON price table overriding the built-in East US list prices used for cost estimates |
| `--summary-only` | Omit per-resource detail for healthy/informational sections in HTML and Markdown reports |
| `--version` | Show version and exit |

`DudeWheresMyLogs diff` takes its own options: `-f text|md|json`, `-o FILE`, `--ci`, and `--fail-on` (see `DudeWheresMyLogs diff --help`).

### Tips for large environments
- Use `-w 20` (or higher) to increase parallelism when scanning thousands of resources. Stay at or below 20 to avoid excessive ARM throttling.
- The tool automatically retries throttled requests (HTTP 429) with exponential backoff, so it will work through rate limits without failing.
- Subscriptions are scanned sequentially, which naturally spreads API load across subscription-level throttle buckets.

## Output

- **HTML report** (default): Self-contained file -- inline CSS, no external dependencies, automatic light/dark mode. A findings overview links to numbered collapsible sections for each active check, followed by Healthy Resources, Informational, Activity Log Export, Workspace Usage (with per-workspace cost estimates), and a Destination Map showing every destination and the resources streaming to it, ordered by impact. Each resource expands inline to its full ID, per-setting destinations, categories, and estimated waste. The full machine-readable payload is embedded in a `<script type="application/json" id="dwml-data">` block, so any saved report can be re-parsed or diffed later without re-scanning.
- **JSON report** (`-f json`): The same structured payload as a standalone file, for automation, diffing, and CI.
- **Markdown report** (`-f md`): Findings-focused tables ready to paste into tickets, PRs, or chat, including workspace usage and cost estimates.
- **CSV report** (`-f csv`): Flat per-resource export for spreadsheet analysis.

## How the deeper checks work

- **Ingestion liveness** (`silent-resources`): per destination workspace, the tool queries which `_ResourceId`s actually landed data in the lookback window and reconciles that against what is configured to ship there. A silent pipeline is a signal, not proof -- an idle resource legitimately emits nothing.
- **Workspace usage** (`unqueried-workspaces`): query counts come from the workspace's `LAQueryLogs` audit table. The tool's own queries carry a self-identifying marker and exclude themselves, so scheduled scans never make a workspace look "used". Workspaces without query auditing are reported as unassessable (`no-query-auditing`) rather than guessed at.
- **Cost estimates**: ingestion is priced per table plan (Analytics/Basic/Auxiliary), with Sentinel-enabled workspaces priced at the combined rate and given Sentinel's longer free retention window. Duplicate-shipping waste keeps the largest flow and counts the rest as redundant. Cross-region waste uses inter-continental bandwidth rates. Prices ship in `prices.json` (US East list prices) and can be overridden with `--price-file`.

## License
MIT
