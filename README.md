# Dude Wheres My Logs

## Overview
DudeWheresMyLogs is a CLI tool designed to help administrators quickly identify and analyze log settings across their cloud resources.

## Features
- Scan all resources across a tenant or subscription
- Identify resources without diagnostic logging
- Detect duplicate logging configurations
- Export detailed results to CSV

## Prerequisites
- Python 3.8+
- Azure CLI installed and configured

## Installation
```bash
pip install .
```

## Usage
```bash
# Run the tool
DudeWheresMyLogs
```

## Example Output
- Displays a summary of resource types
- Highlights resources with duplicate logging
- Shows log destination statistics
- Generates a CSV report

## License
GPL-3.0

## Roadmap
- List retention at log destination
- List permissions at log destination
- AWS support
- GCP support
