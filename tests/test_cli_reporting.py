import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dwml.cli import _determine_exit_code, filter_resources
from dwml.diagnostics import DiagnosticResult
from dwml.reporting import generate_json


class FilterResourcesTests(unittest.TestCase):
    def test_filters_by_type_and_resource_group(self):
        resources = [
            {
                "id": "/subscriptions/sub-1/resourceGroups/rg-app/providers/Microsoft.KeyVault/vaults/kv-1",
                "name": "kv-1",
                "type": "Microsoft.KeyVault/vaults",
                "location": "eastus",
                "resource_group": "rg-app",
            },
            {
                "id": "/subscriptions/sub-1/resourceGroups/rg-net/providers/Microsoft.Network/networkSecurityGroups/nsg-1",
                "name": "nsg-1",
                "type": "Microsoft.Network/networkSecurityGroups",
                "location": "eastus",
                "resource_group": "rg-net",
            },
            {
                "id": "/subscriptions/sub-1/resourceGroups/rg-app/providers/Microsoft.Storage/storageAccounts/stg1",
                "name": "stg1",
                "type": "Microsoft.Storage/storageAccounts",
                "location": "eastus2",
                "resource_group": "rg-app",
            },
        ]

        filtered = filter_resources(
            resources,
            include_types=["Microsoft.*/*", "Microsoft.KeyVault/*"],
            exclude_types=["Microsoft.Storage/*"],
            resource_groups=["rg-app"],
        )

        self.assertEqual([resource["name"] for resource in filtered], ["kv-1"])


class ExitCodeTests(unittest.TestCase):
    def _result(self, status, duplicate=False):
        return DiagnosticResult(
            resource_id="/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Example/type/name",
            resource_name="name",
            resource_type="Microsoft.Example/type",
            resource_group="rg",
            resource_location="eastus",
            subscription_id="sub-1",
            subscription_name="Subscription 1",
            status=status,
            duplicate=duplicate,
        )

    def test_non_ci_mode_always_succeeds(self):
        self.assertEqual(_determine_exit_code([self._result("Missing")], ci_mode=False), 0)

    def test_ci_mode_returns_findings_code(self):
        self.assertEqual(_determine_exit_code([self._result("Missing")], ci_mode=True), 1)
        self.assertEqual(_determine_exit_code([self._result("Enabled", duplicate=True)], ci_mode=True), 1)

    def test_ci_mode_prioritizes_errors(self):
        results = [self._result("Missing"), self._result("Error")]
        self.assertEqual(_determine_exit_code(results, ci_mode=True), 2)


class JsonReportingTests(unittest.TestCase):
    def test_generate_json_writes_summary_and_results(self):
        results = [
            DiagnosticResult(
                resource_id="/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Example/type/name",
                resource_name="name",
                resource_type="Microsoft.Example/type",
                resource_group="rg",
                resource_location="eastus",
                subscription_id="sub-1",
                subscription_name="Subscription 1",
                status="Enabled",
                destinations=[
                    {
                        "setting_name": "default",
                        "type": "Log Analytics",
                        "name": "law-1",
                        "id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/law-1",
                        "region": "eastus",
                        "log_categories": ["AuditEvent"],
                        "la_destination_type": "Dedicated",
                    }
                ],
                duplicate=True,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "report.json"
            generate_json(results, str(output))

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["summary"]["total_resources"], 1)
        self.assertEqual(payload["summary"]["duplicate_count"], 1)
        self.assertEqual(payload["results"][0]["resource_name"], "name")
        self.assertEqual(payload["results"][0]["destinations"][0]["name"], "law-1")


if __name__ == "__main__":
    unittest.main()
