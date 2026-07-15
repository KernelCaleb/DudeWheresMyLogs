import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dwml.checks import CHECK_NAMES, get_check, is_healthy
from dwml.cli import _determine_exit_code, build_parser, _parse_checks, filter_resources
from dwml.diagnostics import DiagnosticResult, _extract_destinations, _flag_cross_region
from dwml.reporting import generate_html, generate_json, generate_markdown
from dwml.workspaces import (
    WorkspaceUsage,
    _workspace_audit_enabled,
    flag_silent_resources,
    workspace_status,
)


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


class SilentResourceTests(unittest.TestCase):
    def _la_dest(self, wid=WORKSPACE_A, not_found=False):
        return {"setting_name": "s", "type": "Log Analytics", "name": "law",
                "id": wid, "log_categories": [], "la_destination_type": "",
                "region": "eastus", "not_found": not_found}

    def test_flags_resource_missing_from_workspace_data(self):
        silent = _result(destinations=[self._la_dest()])
        flowing = _result(destinations=[self._la_dest()])
        # The scanned resource ID, lowercased, is present for "flowing" only
        seen_map = {WORKSPACE_A: {flowing.resource_id.lower()}}
        flag_silent_resources([flowing], seen_map)
        flag_silent_resources([silent], {WORKSPACE_A: set()})
        self.assertFalse(flowing.destinations[0].get("silent"))
        self.assertTrue(silent.destinations[0].get("silent"))
        self.assertTrue(get_check("silent-resources").detect(silent))
        self.assertFalse(get_check("silent-resources").detect(flowing))

    def test_unknown_and_dead_workspaces_not_flagged(self):
        unknown = _result(destinations=[self._la_dest()])
        dead = _result(destinations=[self._la_dest(not_found=True)])
        flag_silent_resources([unknown, dead], {})  # workspace not queryable
        flag_silent_resources([dead], {WORKSPACE_A: set()})
        self.assertNotIn("silent", unknown.destinations[0])
        self.assertNotIn("silent", dead.destinations[0])

    def test_matching_is_case_insensitive(self):
        r = _result(destinations=[self._la_dest()])
        # Log Analytics lowercases _ResourceId; ARM IDs are mixed case
        seen_map = {WORKSPACE_A: {r.resource_id.lower()}}
        r.resource_id = r.resource_id.upper()
        seen_map[WORKSPACE_A] = {r.resource_id.lower()}
        flag_silent_resources([r], seen_map)
        self.assertFalse(r.destinations[0]["silent"])


class ActivityLogExportTests(unittest.TestCase):
    def _sub_setting(self, workspace_id=None, categories=("Administrative",), enabled=True):
        return SimpleNamespace(
            workspace_id=workspace_id, storage_account_id=None,
            event_hub_authorization_rule_id=None, event_hub_name=None,
            marketplace_partner_id=None,
            logs=[SimpleNamespace(category=c, enabled=enabled) for c in categories],
        )

    def test_exported_detection(self):
        from dwml.tenant import audit_from_settings
        exported = audit_from_settings("sub-1", "Sub", [
            self._sub_setting(workspace_id=WORKSPACE_A,
                              categories=("Administrative", "Security", "Policy"))])
        self.assertTrue(exported.exported)
        self.assertEqual(exported.missing_core, [])
        self.assertEqual(exported.destinations[0]["type"], "Log Analytics")

        not_exported = audit_from_settings("sub-1", "Sub", [])
        self.assertFalse(not_exported.exported)
        self.assertEqual(not_exported.missing_core,
                         ["Administrative", "Security", "Policy"])

        # A setting with categories but no destination does not count
        no_dest = audit_from_settings("sub-1", "Sub", [self._sub_setting()])
        self.assertFalse(no_dest.exported)

        # Disabled categories do not count
        disabled = audit_from_settings("sub-1", "Sub", [
            self._sub_setting(workspace_id=WORKSPACE_A, enabled=False)])
        self.assertFalse(disabled.exported)

    def test_check_detection_and_exit_code(self):
        from dwml.tenant import SubscriptionAudit
        check = get_check("no-activity-log-export")
        bad = SubscriptionAudit("sub-1", "Sub", exported=False)
        good = SubscriptionAudit("sub-1", "Sub", exported=True)
        unknown = SubscriptionAudit("sub-1", "Sub", exported=None, error="denied")
        self.assertTrue(check.detect(bad))
        self.assertFalse(check.detect(good))
        self.assertFalse(check.detect(unknown))
        # Fails CI by default (it is a missing-logging finding)
        self.assertEqual(_determine_exit_code([], ci_mode=True, sub_audits=[bad]), 1)
        self.assertEqual(_determine_exit_code([], ci_mode=True, sub_audits=[good]), 0)

    def test_reports_render_activity_log_section(self):
        from dwml.tenant import SubscriptionAudit
        audits = [SubscriptionAudit("sub-1", "Sub One", exported=False,
                                    missing_core=["Administrative", "Security", "Policy"])]
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_out = Path(tmp_dir) / "r.html"
            md_out = Path(tmp_dir) / "r.md"
            json_out = Path(tmp_dir) / "r.json"
            generate_html([_result()], str(html_out), sub_audits=audits)
            generate_markdown([_result()], str(md_out), sub_audits=audits)
            generate_json([_result()], str(json_out), sub_audits=audits)
            html_content = html_out.read_text(encoding="utf-8")
            md_content = md_out.read_text(encoding="utf-8")
            payload = json.loads(json_out.read_text(encoding="utf-8"))

        self.assertIn("Activity Log Export", html_content)
        self.assertIn("1 of 1 not exported", html_content)
        self.assertIn("## Activity Log Export", md_content)
        self.assertEqual(payload["summary"]["no_activity_log_export_count"], 1)


class CostEstimateTests(unittest.TestCase):
    def setUp(self):
        from dwml.costs import load_prices
        self.prices = load_prices()

    def test_workspace_ingest_and_retention_math(self):
        from dwml.costs import estimate_costs
        ws = _workspace(audit=True, queries=1)
        ws.ingest_gb = 30.0  # 30 GB over 30 days -> 30 GB/month
        ws.ingest_gb_by_plan = {"analytics": 30.0, "basic": 0.0, "auxiliary": 0.0}
        ws.sentinel_enabled = False
        ws.retention_days = 121  # 90 days beyond the 31 free
        estimate_costs([], [ws], {}, self.prices)
        self.assertAlmostEqual(ws.est_monthly_ingest, 30.0 * 2.30, places=2)
        # 1 GB/day * 90 extra days * $0.10/GB-month
        self.assertAlmostEqual(ws.est_monthly_retention, 1.0 * 90 * 0.10, places=2)

    def test_sentinel_rate_applies(self):
        from dwml.costs import estimate_costs
        ws = _workspace()
        ws.ingest_gb = 30.0
        ws.ingest_gb_by_plan = {"analytics": 30.0, "basic": 0.0, "auxiliary": 0.0}
        ws.sentinel_enabled = True
        ws.retention_days = 90  # within Sentinel free window
        estimate_costs([], [ws], {}, self.prices)
        self.assertAlmostEqual(ws.est_monthly_ingest, 30.0 * 4.30, places=2)
        self.assertEqual(ws.est_monthly_retention, 0.0)

    def test_duplicate_waste_keeps_largest_flow(self):
        from dwml.costs import estimate_costs
        ws_a = _workspace(name="law-a")
        ws_b = _workspace(name="law-b")
        for ws in (ws_a, ws_b):
            ws.sentinel_enabled = False
        r = _result(duplicate=True, destinations=[
            {"type": "Log Analytics", "id": ws_a.workspace_id, "name": "law-a",
             "region": "eastus", "not_found": False, "setting_name": "s",
             "log_categories": [], "la_destination_type": ""},
            {"type": "Log Analytics", "id": ws_b.workspace_id, "name": "law-b",
             "region": "eastus", "not_found": False, "setting_name": "s",
             "log_categories": [], "la_destination_type": ""},
        ])
        rid = r.resource_id.lower()
        seen = {ws_a.workspace_id: {rid: 10.0}, ws_b.workspace_id: {rid: 3.0}}
        estimate_costs([r], [ws_a, ws_b], seen, self.prices)
        # Larger flow (10 GB) kept; 3 GB/30d redundant -> 3 GB/mo * $2.30
        self.assertAlmostEqual(r.est_monthly_impact, 3.0 * 2.30, places=2)

    def test_cross_region_bandwidth(self):
        from dwml.costs import bandwidth_rate
        self.assertEqual(bandwidth_rate("eastus", "westus2", self.prices), 0.02)
        self.assertEqual(bandwidth_rate("eastus", "westeurope", self.prices), 0.05)
        self.assertEqual(bandwidth_rate("japaneast", "eastus", self.prices), 0.08)
        self.assertEqual(bandwidth_rate("brazilsouth", "eastus", self.prices), 0.16)

    def test_no_data_no_estimate(self):
        from dwml.costs import estimate_costs
        ws = _workspace()
        ws.ingest_gb = None
        r = _result(duplicate=True, destinations=[
            {"type": "Log Analytics", "id": ws.workspace_id, "name": "law",
             "region": "eastus", "not_found": False, "setting_name": "s",
             "log_categories": [], "la_destination_type": ""}])
        estimate_costs([r], [ws], {}, self.prices)
        self.assertIsNone(ws.est_monthly_total)
        self.assertIsNone(r.est_monthly_impact)

    def test_export_fee_destinations_counted(self):
        from dwml.costs import export_fee_destinations
        r = _result(destinations=[
            {"type": "Storage Account", "id": STORAGE_A, "name": "stga"},
            {"type": "Log Analytics", "id": WORKSPACE_A, "name": "law"}])
        self.assertEqual(len(export_fee_destinations([r])), 1)


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


class TermTests(unittest.TestCase):
    def test_paint_disabled_is_passthrough(self):
        from dwml.term import paint
        self.assertEqual(paint("hello", "bold", "red", enabled=False), "hello")
        self.assertEqual(paint("hello", enabled=True), "hello")

    def test_paint_enabled_wraps_in_ansi(self):
        from dwml.term import paint
        self.assertEqual(paint("x", "bold", enabled=True), "\x1b[1mx\x1b[0m")
        self.assertEqual(paint("x", "bold", "red", enabled=True), "\x1b[1;31mx\x1b[0m")

    def test_fmt_elapsed(self):
        from dwml.term import fmt_elapsed
        self.assertEqual(fmt_elapsed(42), "42s")
        self.assertEqual(fmt_elapsed(187), "3m 07s")
        self.assertEqual(fmt_elapsed(3725), "1h 02m")

    def test_supports_color_respects_no_color_and_tty(self):
        import os
        from unittest import mock
        from dwml.term import supports_color
        tty = SimpleNamespace(isatty=lambda: True)
        pipe = SimpleNamespace(isatty=lambda: False)
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("NO_COLOR", "FORCE_COLOR", "TERM")}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            self.assertTrue(supports_color(tty))
            self.assertFalse(supports_color(pipe))
        with mock.patch.dict(os.environ, {**clean_env, "NO_COLOR": ""}, clear=True):
            self.assertFalse(supports_color(tty))
        with mock.patch.dict(os.environ, {**clean_env, "FORCE_COLOR": "1"}, clear=True):
            self.assertTrue(supports_color(pipe))


class DiffTests(unittest.TestCase):
    def _payload(self, results, ws_results=None, sub_audits=None):
        from dwml.reporting import build_payload
        return build_payload(results, ws_results=ws_results, sub_audits=sub_audits)

    def _missing(self, name):
        r = _result("Missing")
        r.resource_id = f"/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Example/type/{name}"
        r.resource_name = name
        return r

    def test_load_payload_from_json_and_html(self):
        from dwml.diffing import load_payload
        results = [self._missing("kv-1")]
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_out = Path(tmp_dir) / "r.json"
            html_out = Path(tmp_dir) / "r.html"
            generate_json(results, str(json_out))
            generate_html(results, str(html_out))
            from_json = load_payload(str(json_out))
            from_html = load_payload(str(html_out))

        self.assertEqual(from_json["summary"]["total_resources"], 1)
        self.assertEqual(from_html["summary"]["total_resources"], 1)
        self.assertEqual(from_json["results"][0]["resource_name"], "kv-1")

    def test_load_payload_rejects_foreign_files(self):
        from dwml.diffing import load_payload
        with tempfile.TemporaryDirectory() as tmp_dir:
            other = Path(tmp_dir) / "other.html"
            other.write_text("<html><body>hello</body></html>", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_payload(str(other))

    def test_compute_diff_added_and_resolved(self):
        from dwml.diffing import compute_diff, diff_has_changes
        old = self._payload([self._missing("kv-old"), self._missing("kv-both")])
        new = self._payload([self._missing("kv-both"), self._missing("kv-new"),
                             _result(duplicate=True)])
        diff = compute_diff(old, new)

        missing = diff["checks"]["missing"]
        self.assertEqual(missing["old_count"], 2)
        self.assertEqual(missing["new_count"], 2)
        self.assertEqual([i["name"] for i in missing["added"]], ["kv-new"])
        self.assertEqual([i["name"] for i in missing["resolved"]], ["kv-old"])
        self.assertEqual(len(diff["checks"]["duplicates"]["added"]), 1)
        self.assertTrue(diff_has_changes(diff))
        # Workspace analysis missing from both payloads: not comparable
        self.assertIn("unqueried-workspaces", diff["skipped"])

    def test_compute_diff_no_changes(self):
        from dwml.diffing import compute_diff, diff_has_changes
        payload = self._payload([self._missing("kv-1")])
        diff = compute_diff(payload, payload)
        self.assertFalse(diff_has_changes(diff))

    def test_compute_diff_workspace_and_cost_delta(self):
        from dwml.diffing import compute_diff
        ws_old = _workspace(audit=True, queries=5)
        ws_new = _workspace(audit=True, queries=0)
        ws_old.est_monthly_total = 10.0
        ws_new.est_monthly_total = 14.5
        old = self._payload([_result()], ws_results=[ws_old])
        new = self._payload([_result()], ws_results=[ws_new])
        diff = compute_diff(old, new)

        unqueried = diff["checks"]["unqueried-workspaces"]
        self.assertEqual([i["name"] for i in unqueried["added"]], ["law-1"])
        self.assertNotIn("unqueried-workspaces", diff["skipped"])
        self.assertEqual(diff["costs"]["est_monthly_spend_usd"]["old"], 10.0)
        self.assertEqual(diff["costs"]["est_monthly_spend_usd"]["new"], 14.5)

    def test_renderers_and_exit_codes(self):
        from dwml.diffing import run_diff
        old_results = [self._missing("kv-old")]
        new_results = [self._missing("kv-old"), self._missing("kv-new")]
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_path = Path(tmp_dir) / "old.json"
            new_path = Path(tmp_dir) / "new.json"
            generate_json(old_results, str(old_path))
            generate_json(new_results, str(new_path))

            md_out = Path(tmp_dir) / "diff.md"
            self.assertEqual(run_diff([str(old_path), str(new_path),
                                       "-f", "md", "-o", str(md_out)]), 0)
            md_content = md_out.read_text(encoding="utf-8")

            json_out = Path(tmp_dir) / "diff.json"
            self.assertEqual(run_diff([str(old_path), str(new_path), "--ci",
                                       "-f", "json", "-o", str(json_out)]), 1)
            diff_payload = json.loads(json_out.read_text(encoding="utf-8"))

            # New finding outside --fail-on categories does not fail CI
            self.assertEqual(run_diff([str(old_path), str(new_path), "--ci",
                                       "--fail-on", "duplicates",
                                       "-f", "json", "-o", str(json_out)]), 0)
            # Identical reports: clean
            self.assertEqual(run_diff([str(new_path), str(new_path), "--ci",
                                       "-f", "json", "-o", str(json_out)]), 0)
            # Unreadable input: operational error
            self.assertEqual(run_diff([str(old_path),
                                       str(Path(tmp_dir) / "nope.json"),
                                       "-f", "json", "-o", str(json_out)]), 2)

        self.assertIn("# Log Health Report Diff", md_content)
        self.assertIn("**New:** kv-new", md_content)
        self.assertEqual(
            [i["name"] for i in diff_payload["checks"]["missing"]["added"]],
            ["kv-new"])

    def test_render_text_marks_changes(self):
        from dwml.diffing import compute_diff, render_text
        old = self._payload([self._missing("kv-old")])
        new = self._payload([self._missing("kv-new")])
        text = render_text(compute_diff(old, new), color=False)
        self.assertIn("+ kv-new", text)
        self.assertIn("- kv-old", text)
        self.assertIn("1 new finding(s), 1 resolved.", text)
        clean = render_text(compute_diff(old, old), color=False)
        self.assertIn("No finding changes.", clean)


_POLICY_YAML = """\
version: 1
rules:
  - name: kv-audit-to-la
    title: Key Vaults must ship AuditEvent to Log Analytics
    severity: fail
    match: { type: "Microsoft.KeyVault/*" }
    require:
      categories: ["AuditEvent"]
      destination_type: "Log Analytics"
  - name: no-legacy-tables
    severity: warn
    match: { type: "*" }
    forbid: { la_destination_type: "AzureDiagnostics" }
  - name: ws-retention-90
    severity: warn
    scope: workspace
    require: { retention_days_at_least: 90 }
"""


def _kv_result(name="kv-1", status="Enabled", destinations=None, duplicate=False):
    r = _result(status=status, destinations=destinations or [], duplicate=duplicate)
    r.resource_type = "Microsoft.KeyVault/vaults"
    r.resource_id = (
        f"/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/{name}")
    r.resource_name = name
    return r


def _la_dest(categories=(), la_type="", region="eastus", silent=None, wid=WORKSPACE_A):
    d = {"setting_name": "s", "type": "Log Analytics", "name": "law",
         "id": wid, "log_categories": list(categories),
         "la_destination_type": la_type, "region": region, "not_found": False}
    if silent is not None:
        d["silent"] = silent
    return d


class PolicyLoadTests(unittest.TestCase):
    def _write(self, content, suffix=".yaml"):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False, encoding="utf-8")
        f.write(content)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name

    def test_loads_yaml_and_json(self):
        from dwml.policy import load_policy
        rules = load_policy(self._write(_POLICY_YAML))
        self.assertEqual([r.name for r in rules],
                         ["kv-audit-to-la", "no-legacy-tables", "ws-retention-90"])
        self.assertEqual(rules[0].severity, "fail")
        self.assertEqual(rules[2].scope, "workspace")

        json_policy = json.dumps({"rules": [
            {"name": "j-rule", "require": {"diagnostics": True}}]})
        rules = load_policy(self._write(json_policy, suffix=".json"))
        self.assertEqual(rules[0].severity, "fail")  # default

    def test_validation_errors(self):
        from dwml.policy import PolicyError, load_policy
        cases = [
            "rules:\n  - severity: fail\n    require: {diagnostics: true}",  # no name
            "rules:\n  - name: x\n    severity: bogus\n    require: {diagnostics: true}",
            "rules:\n  - name: x\n    require: {nonsense: true}",
            "rules:\n  - name: x\n    match: {type: '*'}",  # no require/forbid
            "rules:\n  - name: x\n    require: {diagnostics: true}\n"
            "  - name: x\n    require: {diagnostics: true}",  # duplicate
            "rules:\n  - name: x\n    scope: workspace\n    forbid: {silent: true}",
        ]
        for content in cases:
            with self.assertRaises(PolicyError, msg=content):
                load_policy(self._write(content))

    def test_baseline_policy_ships_valid(self):
        from dwml.policy import load_policy
        baseline = Path(__file__).parent.parent / "policies" / "baseline.yaml"
        rules = load_policy(str(baseline))
        self.assertGreaterEqual(len(rules), 5)


class PolicyEvaluationTests(unittest.TestCase):
    def _evaluate(self, yaml_rules, results, ws_results=()):
        import yaml as yaml_mod
        from dwml.policy import _validate_rule, evaluate_policy
        raw = yaml_mod.safe_load(yaml_rules)["rules"]
        rules = []
        seen = set()
        for i, r in enumerate(raw):
            rule = _validate_rule(r, i, seen)
            seen.add(rule.name)
            rules.append(rule)
        evaluate_policy(rules, list(results), list(ws_results))
        return rules

    def test_require_categories_and_destination(self):
        good = _kv_result("kv-good", destinations=[_la_dest(["AuditEvent"])])
        alllogs = _kv_result("kv-alllogs", destinations=[_la_dest(["allLogs"])])
        wrong_cat = _kv_result("kv-nocat", destinations=[_la_dest(["Other"])])
        storage_only = _kv_result("kv-stg", destinations=[{
            "setting_name": "s", "type": "Storage Account", "name": "stg",
            "id": STORAGE_A, "log_categories": ["AuditEvent"],
            "la_destination_type": "", "region": "eastus", "not_found": False}])
        missing = _kv_result("kv-missing", status="Missing")
        unmatched = _result()  # not a Key Vault

        self._evaluate(_POLICY_YAML, [good, alllogs, wrong_cat, storage_only,
                                      missing, unmatched])
        self.assertEqual(good.policy_violations, [])
        self.assertEqual(alllogs.policy_violations, [])
        self.assertEqual(wrong_cat.policy_violations, ["kv-audit-to-la"])
        self.assertEqual(storage_only.policy_violations, ["kv-audit-to-la"])
        self.assertEqual(missing.policy_violations, ["kv-audit-to-la"])
        self.assertEqual(unmatched.policy_violations, [])

    def test_forbid_la_destination_type(self):
        legacy = _result(destinations=[_la_dest(["X"], la_type="AzureDiagnostics")])
        dedicated = _result(destinations=[_la_dest(["X"], la_type="Dedicated")])
        unknown_mode = _result(destinations=[_la_dest(["X"], la_type="")])
        self._evaluate(_POLICY_YAML, [legacy, dedicated, unknown_mode])
        self.assertIn("no-legacy-tables", legacy.policy_violations)
        self.assertEqual(dedicated.policy_violations, [])
        self.assertEqual(unknown_mode.policy_violations, [])

    def test_require_flowing_unknown_never_violates(self):
        rules = """
rules:
  - name: must-flow
    require: { flowing: true }
"""
        silent = _result(destinations=[_la_dest(["X"], silent=True)])
        flowing = _result(destinations=[_la_dest(["X"], silent=False)])
        unknown = _result(destinations=[_la_dest(["X"])])  # liveness not assessed
        no_la = _result(status="Missing")
        self._evaluate(rules, [silent, flowing, unknown, no_la])
        self.assertEqual(silent.policy_violations, ["must-flow"])
        self.assertEqual(flowing.policy_violations, [])
        self.assertEqual(unknown.policy_violations, [])
        self.assertEqual(no_la.policy_violations, ["must-flow"])

    def test_destination_region_same_and_unknown(self):
        rules = """
rules:
  - name: same-region
    require: { destination_region: same }
"""
        cross = _result(destinations=[_la_dest(["X"], region="westus")],
                        location="eastus")
        local = _result(destinations=[_la_dest(["X"], region="East US")],
                        location="eastus")
        unresolved = _result(destinations=[_la_dest(["X"], region="")],
                             location="eastus")
        self._evaluate(rules, [cross, local, unresolved])
        self.assertEqual(cross.policy_violations, ["same-region"])
        self.assertEqual(local.policy_violations, [])
        self.assertEqual(unresolved.policy_violations, [])
        # The evaluation-only helper key never leaks into report data
        self.assertNotIn("_src_region", cross.destinations[0])

    def test_workspace_rules(self):
        rules = """
rules:
  - name: retention-90
    scope: workspace
    require: { retention_days_at_least: 90 }
  - name: must-be-read
    scope: workspace
    require: { queried: true }
  - name: cost-cap
    scope: workspace
    require: { max_monthly_cost: 100 }
"""
        short = _workspace(name="law-short", audit=True, queries=5)
        short.retention_days = 30
        unread = _workspace(name="law-unread", audit=True, queries=0)
        unread.retention_days = 180
        unknown = _workspace(name="law-unknown", audit=None, queries=None)
        unknown.retention_days = 0  # config unknown
        pricey = _workspace(name="law-pricey", audit=True, queries=5)
        pricey.retention_days = 180
        pricey.est_monthly_total = 250.0
        self._evaluate(rules, [], [short, unread, unknown, pricey])
        self.assertEqual(short.policy_violations, ["retention-90"])
        self.assertEqual(unread.policy_violations, ["must-be-read"])
        self.assertEqual(unknown.policy_violations, [])
        self.assertEqual(pricey.policy_violations, ["cost-cap"])


class PolicyIntegrationTests(unittest.TestCase):
    def setUp(self):
        from dwml.checks import reset_extra_checks
        self.addCleanup(reset_extra_checks)

    def _register(self, yaml_rules=_POLICY_YAML):
        import yaml as yaml_mod
        from dwml.checks import register_checks
        from dwml.policy import _validate_rule, make_checks
        raw = yaml_mod.safe_load(yaml_rules)["rules"]
        rules = []
        seen = set()
        for i, r in enumerate(raw):
            rule = _validate_rule(r, i, seen)
            seen.add(rule.name)
            rules.append(rule)
        checks = make_checks(rules)
        register_checks(checks)
        return rules, checks

    def test_registration_and_exit_codes(self):
        self._register()
        from dwml import checks as registry
        self.assertIn("kv-audit-to-la", registry.CHECK_NAMES)
        # severity fail joins the CI default; warn does not
        self.assertIn("kv-audit-to-la", registry.DEFAULT_FAIL_ON)
        self.assertNotIn("no-legacy-tables", registry.DEFAULT_FAIL_ON)

        violator = _kv_result("kv-bad")
        violator.policy_violations = ["kv-audit-to-la"]
        self.assertEqual(_determine_exit_code([violator], ci_mode=True), 1)
        self.assertEqual(_determine_exit_code(
            [violator], ci_mode=True, fail_on=("missing",)), 0)
        self.assertFalse(is_healthy(violator))

    def test_duplicate_name_rejected(self):
        from dwml.checks import register_checks
        from dwml.policy import PolicyRule, make_checks
        clash = PolicyRule(name="missing", title="x", description="x",
                           severity="fail", scope="resource",
                           require={"diagnostics": True})
        with self.assertRaises(ValueError):
            register_checks(make_checks([clash]))

    def test_reports_render_policy_sections_and_payload(self):
        self._register()
        violator = _kv_result("kv-bad")
        violator.policy_violations = ["kv-audit-to-la"]
        clean = _kv_result("kv-good", destinations=[_la_dest(["AuditEvent"])])
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_out = Path(tmp_dir) / "r.html"
            json_out = Path(tmp_dir) / "r.json"
            md_out = Path(tmp_dir) / "r.md"
            generate_html([violator, clean], str(html_out))
            generate_json([violator, clean], str(json_out))
            generate_markdown([violator, clean], str(md_out))
            html_content = html_out.read_text(encoding="utf-8")
            md_content = md_out.read_text(encoding="utf-8")
            payload = json.loads(json_out.read_text(encoding="utf-8"))

        self.assertIn("Key Vaults must ship AuditEvent to Log Analytics",
                      html_content)
        self.assertIn("Policy violations: kv-audit-to-la", html_content)
        self.assertIn("| Key Vaults must ship AuditEvent to Log Analytics | 1 |",
                      md_content)
        self.assertEqual(
            payload["summary"]["policy_violation_counts"]["kv-audit-to-la"], 1)
        self.assertEqual(payload["policy"][0]["severity"], "fail")

    def test_diff_compares_policy_findings(self):
        from dwml.diffing import compute_diff
        from dwml.reporting import build_payload
        self._register()
        clean = _kv_result("kv-1", destinations=[_la_dest(["AuditEvent"])])
        old_payload = build_payload([clean])

        violator = _kv_result("kv-1", destinations=[_la_dest(["Other"])])
        violator.policy_violations = ["kv-audit-to-la"]
        new_payload = build_payload([violator])

        diff = compute_diff(old_payload, new_payload)
        added = diff["checks"]["kv-audit-to-la"]["added"]
        self.assertEqual([i["name"] for i in added], ["kv-1"])

        # A rule recorded in only one report is skipped, not guessed
        no_policy = build_payload([clean])
        no_policy.pop("policy")
        diff = compute_diff(no_policy, new_payload)
        self.assertIn("kv-audit-to-la", diff["skipped"])
        self.assertNotIn("kv-audit-to-la", diff["checks"])


if __name__ == "__main__":
    unittest.main()
