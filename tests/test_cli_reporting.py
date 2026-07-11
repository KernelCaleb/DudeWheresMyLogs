import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dwml.checks import CHECK_NAMES, get_check, is_healthy
from dwml.cli import _determine_exit_code, build_parser, _parse_checks, filter_resources
from dwml.diagnostics import DiagnosticResult, _extract_destinations, _flag_cross_region
from dwml.reporting import generate_html, generate_json, generate_markdown
from dwml.workspaces import WorkspaceUsage, _workspace_audit_enabled, workspace_status


def _setting(name="setting", workspace_id=None, storage_account_id=None,
             event_hub_rule=None, marketplace_partner_id=None):
    """Build a minimal stand-in for an Azure DiagnosticSettingsResource."""
    return SimpleNamespace(
        name=name,
        logs=[],
        workspace_id=workspace_id,
        storage_account_id=storage_account_id,
        event_hub_authorization_rule_id=event_hub_rule,
        event_hub_name=None,
        marketplace_partner_id=marketplace_partner_id,
        log_analytics_destination_type=None,
    )


def _result(status="Enabled", duplicate=False, destinations=None, location="eastus"):
    return DiagnosticResult(
        resource_id="/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Example/type/name",
        resource_name="name",
        resource_type="Microsoft.Example/type",
        resource_group="rg",
        resource_location=location,
        subscription_id="sub-1",
        subscription_name="Subscription 1",
        status=status,
        destinations=destinations or [],
        duplicate=duplicate,
    )


WORKSPACE_A = "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/law-a"
WORKSPACE_B = "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/law-b"
STORAGE_A = "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/stga"


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


class DuplicateDetectionTests(unittest.TestCase):
    def test_same_type_different_ids_is_duplicate(self):
        settings = [
            _setting("a", workspace_id=WORKSPACE_A),
            _setting("b", workspace_id=WORKSPACE_B),
        ]
        _, duplicate = _extract_destinations(settings)
        self.assertTrue(duplicate)

    def test_same_type_same_id_is_not_duplicate(self):
        # Two settings pointing at the same workspace (e.g. split categories)
        # are deduplicated by Azure and are not a cost issue.
        settings = [
            _setting("a", workspace_id=WORKSPACE_A),
            _setting("b", workspace_id=WORKSPACE_A),
        ]
        _, duplicate = _extract_destinations(settings)
        self.assertFalse(duplicate)

    def test_different_types_are_not_duplicate(self):
        settings = [
            _setting("a", workspace_id=WORKSPACE_A, storage_account_id=STORAGE_A),
        ]
        _, duplicate = _extract_destinations(settings)
        self.assertFalse(duplicate)


class ProviderTypeParsingTests(unittest.TestCase):
    def test_parses_simple_and_nested_types(self):
        from dwml.diagnostics import _provider_type_from_id
        self.assertEqual(
            _provider_type_from_id(WORKSPACE_A),
            ("Microsoft.OperationalInsights", "workspaces"))
        self.assertEqual(
            _provider_type_from_id(
                "/subscriptions/s/resourceGroups/rg/providers/Microsoft.EventHub"
                "/namespaces/ns1/authorizationRules/rule1"),
            ("Microsoft.EventHub", "namespaces/authorizationRules"))
        self.assertEqual(_provider_type_from_id("not-a-resource-id"), ("", ""))


class CrossRegionTests(unittest.TestCase):
    def _dest(self, region):
        return {
            "setting_name": "s", "type": "Log Analytics", "name": "law",
            "id": WORKSPACE_A, "log_categories": [], "la_destination_type": "",
            "region": region, "not_found": False,
        }

    def test_flags_destination_in_other_region(self):
        result = _result(destinations=[self._dest("westus")], location="eastus")
        _flag_cross_region([result])
        self.assertTrue(result.destinations[0]["cross_region"])

    def test_same_region_and_global_are_not_flagged(self):
        same = _result(destinations=[self._dest("East US")], location="eastus")
        global_res = _result(destinations=[self._dest("westus")], location="global")
        unresolved = _result(destinations=[self._dest("")], location="eastus")
        _flag_cross_region([same, global_res, unresolved])
        self.assertFalse(same.destinations[0]["cross_region"])
        self.assertFalse(global_res.destinations[0]["cross_region"])
        self.assertFalse(unresolved.destinations[0]["cross_region"])


class ExitCodeTests(unittest.TestCase):
    def _dead_result(self):
        return _result(destinations=[{
            "setting_name": "s", "type": "Log Analytics", "name": "law",
            "id": WORKSPACE_A, "log_categories": [], "la_destination_type": "",
            "region": "", "not_found": True,
        }])

    def _cross_result(self):
        return _result(destinations=[{
            "setting_name": "s", "type": "Log Analytics", "name": "law",
            "id": WORKSPACE_A, "log_categories": [], "la_destination_type": "",
            "region": "westus", "not_found": False, "cross_region": True,
        }])

    def test_non_ci_mode_always_succeeds(self):
        self.assertEqual(_determine_exit_code([_result("Missing")], ci_mode=False), 0)

    def test_ci_mode_returns_findings_code(self):
        self.assertEqual(_determine_exit_code([_result("Missing")], ci_mode=True), 1)
        self.assertEqual(_determine_exit_code([_result(duplicate=True)], ci_mode=True), 1)
        self.assertEqual(_determine_exit_code([self._dead_result()], ci_mode=True), 1)

    def test_ci_mode_prioritizes_errors(self):
        results = [_result("Missing"), _result("Error")]
        self.assertEqual(_determine_exit_code(results, ci_mode=True), 2)

    def test_fail_on_limits_findings(self):
        dup = _result(duplicate=True)
        self.assertEqual(
            _determine_exit_code([dup], ci_mode=True, fail_on=("missing",)), 0)
        self.assertEqual(
            _determine_exit_code([dup], ci_mode=True, fail_on=("duplicates",)), 1)

    def test_cross_region_not_a_finding_by_default(self):
        cross = self._cross_result()
        self.assertEqual(_determine_exit_code([cross], ci_mode=True), 0)
        self.assertEqual(
            _determine_exit_code([cross], ci_mode=True, fail_on=("cross-region",)), 1)


class ChecksRegistryTests(unittest.TestCase):
    def test_parse_checks_defaults_to_all(self):
        parser = build_parser()
        self.assertEqual(_parse_checks(None, parser), CHECK_NAMES)
        self.assertEqual(_parse_checks(["missing,duplicates"], parser),
                         ("missing", "duplicates"))

    def test_parse_checks_rejects_unknown(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            _parse_checks(["bogus"], parser)

    def test_healthy_respects_active_checks(self):
        dup = _result(duplicate=True)
        self.assertFalse(is_healthy(dup))
        # With only the missing check active, a duplicate result is healthy
        self.assertTrue(is_healthy(dup, checks=("missing",)))

    def test_html_omits_disabled_check_sections(self):
        results = [_result("Missing"), _result(duplicate=True)]
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "report.html"
            generate_html(results, str(output), checks=("missing",))
            content = output.read_text(encoding="utf-8")

        self.assertIn("Missing Diagnostics", content)
        self.assertNotIn("Duplicate Shipping", content)


def _workspace(name="law-1", audit=True, queries=0, error=""):
    return WorkspaceUsage(
        workspace_id=f"/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/{name}",
        name=name, subscription_id="sub-1", resource_group="rg",
        region="eastus", retention_days=30, sku="PerGB2018",
        shipping_resources=3, audit_enabled=audit, query_count=queries,
        ingest_gb=0.5, access_error=error,
    )


class WorkspaceUsageTests(unittest.TestCase):
    def _log(self, category=None, group=None, enabled=True):
        return SimpleNamespace(category=category, category_group=group, enabled=enabled)

    def test_audit_enabled_detection(self):
        audit_on = SimpleNamespace(logs=[self._log(category="Audit")])
        audit_group = SimpleNamespace(logs=[self._log(group="allLogs")])
        audit_off = SimpleNamespace(logs=[self._log(category="Audit", enabled=False)])
        other = SimpleNamespace(logs=[self._log(category="SomethingElse")])
        self.assertTrue(_workspace_audit_enabled([audit_on]))
        self.assertTrue(_workspace_audit_enabled([audit_group]))
        self.assertFalse(_workspace_audit_enabled([audit_off, other]))
        self.assertFalse(_workspace_audit_enabled([]))

    def test_unqueried_detection(self):
        check = get_check("unqueried-workspaces")
        self.assertTrue(check.detect(_workspace(audit=True, queries=0)))
        self.assertFalse(check.detect(_workspace(audit=True, queries=5)))
        # No auditing or no access: cannot claim unqueried
        self.assertFalse(check.detect(_workspace(audit=False, queries=0)))
        self.assertFalse(check.detect(_workspace(audit=True, queries=None)))

    def test_no_query_auditing_detection(self):
        check = get_check("no-query-auditing")
        self.assertTrue(check.detect(_workspace(audit=False)))
        self.assertFalse(check.detect(_workspace(audit=True)))
        self.assertFalse(check.detect(_workspace(audit=None)))

    def test_workspace_status_strings(self):
        self.assertIn("Unqueried", workspace_status(_workspace(audit=True, queries=0)))
        self.assertIn("Active", workspace_status(_workspace(audit=True, queries=7)))
        self.assertIn("not enabled", workspace_status(_workspace(audit=False)))
        self.assertIn("no data-plane access",
                      workspace_status(_workspace(error="no data-plane access")))

    def test_exit_code_uses_workspace_findings(self):
        clean = [_result()]
        ws = [_workspace(audit=True, queries=0)]
        self.assertEqual(_determine_exit_code(
            clean, ci_mode=True, fail_on=("unqueried-workspaces",), ws_results=ws), 1)
        self.assertEqual(_determine_exit_code(
            clean, ci_mode=True, fail_on=("unqueried-workspaces",), ws_results=[]), 0)
        # Workspace findings never fail CI by default
        self.assertEqual(_determine_exit_code(clean, ci_mode=True, ws_results=ws), 0)

    def test_json_payload_includes_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "report.json"
            generate_json([_result()], str(output),
                          ws_results=[_workspace(audit=True, queries=0),
                                      _workspace(name="law-2", audit=False)])
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["summary"]["unqueried_workspaces_count"], 1)
        self.assertEqual(payload["summary"]["no_query_auditing_count"], 1)
        self.assertEqual(len(payload["workspaces"]), 2)

    def test_reports_render_workspace_section(self):
        ws = [_workspace(audit=True, queries=0)]
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_out = Path(tmp_dir) / "report.html"
            md_out = Path(tmp_dir) / "report.md"
            generate_html([_result()], str(html_out), ws_results=ws)
            generate_markdown([_result()], str(md_out), ws_results=ws)
            html_content = html_out.read_text(encoding="utf-8")
            md_content = md_out.read_text(encoding="utf-8")

        self.assertIn("Workspace Usage", html_content)
        self.assertIn("Unqueried (30d)", html_content)
        self.assertIn("## Workspace Usage (1)", md_content)
        self.assertIn("Unqueried (30d)", md_content)


class ReportingTests(unittest.TestCase):
    def _results(self):
        return [
            _result(
                destinations=[{
                    "setting_name": "default",
                    "type": "Log Analytics",
                    "name": "law-1",
                    "id": WORKSPACE_A,
                    "region": "westus",
                    "log_categories": ["AuditEvent"],
                    "la_destination_type": "Dedicated",
                    "not_found": False,
                    "cross_region": True,
                }],
                duplicate=True,
            ),
            _result("Missing"),
        ]

    def test_generate_json_writes_summary_and_results(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "report.json"
            generate_json(self._results(), str(output))
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["summary"]["total_resources"], 2)
        self.assertEqual(payload["summary"]["duplicate_count"], 1)
        self.assertEqual(payload["summary"]["dead_destination_count"], 0)
        self.assertEqual(payload["summary"]["cross_region_count"], 1)
        self.assertEqual(payload["results"][0]["destinations"][0]["name"], "law-1")

    def test_generate_markdown_lists_findings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "report.md"
            generate_markdown(self._results(), str(output))
            content = output.read_text(encoding="utf-8")

        self.assertIn("## Missing Diagnostics (1)", content)
        self.assertIn("## Duplicate Shipping (1)", content)
        self.assertIn("## Cross-Region Shipping (1)", content)
        self.assertIn("| Missing Diagnostics | 1 |", content)

    def test_generate_html_embeds_machine_readable_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "report.html"
            generate_html(self._results(), str(output))
            content = output.read_text(encoding="utf-8")

        start = content.index('<script type="application/json" id="dwml-data">')
        blob = content[start:]
        blob = blob[blob.index(">") + 1:blob.index("</script>")]
        payload = json.loads(blob.replace("<\\/", "</"))
        self.assertEqual(payload["summary"]["total_resources"], 2)

    def test_summary_only_omits_healthy_detail(self):
        results = self._results() + [_result()]
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "report.html"
            generate_html(results, str(output), summary_only=True)
            content = output.read_text(encoding="utf-8")

        self.assertIn("details omitted (summary-only)", content)


if __name__ == "__main__":
    unittest.main()
