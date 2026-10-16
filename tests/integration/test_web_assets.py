from __future__ import annotations

import re
import unittest

import azure.functions as func

from functions.web_bp import WEB_DIR, zzz_spa_fallback


class WebAssetTests(unittest.TestCase):
    def test_alpine_root_uses_automatic_init_only(self):
        index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        root_tag = re.search(r'<div class="layout"[^>]*>', index)

        self.assertIsNotNone(root_tag)
        self.assertIn('x-data="app()"', root_tag.group(0))
        self.assertNotIn("x-init", root_tag.group(0))

    def test_app_scope_tab_is_wired(self):
        index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        administration = (WEB_DIR / "js" / "features" / "administration.js").read_text(encoding="utf-8")
        app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn("selectTab('app-scope')", index)
        self.assertIn("tab === 'app-scope'", index)
        self.assertIn("appScopeHealth", index)
        self.assertIn("appScope.modeLabel", index)
        self.assertIn("Administrator guidance", index)
        self.assertIn("get appScopeHealth()", administration)
        self.assertIn("get appScopeResolvedSummary()", administration)
        self.assertIn("async loadAppScope()", administration)
        self.assertIn('requestJson("/api/app_scope")', administration)
        self.assertIn('if (tab === "app-scope") this.loadAppScope();', app_js)

    def test_app_scope_tab_has_loading_and_refresh(self):
        index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        administration = (WEB_DIR / "js" / "features" / "administration.js").read_text(encoding="utf-8")

        self.assertIn("appScopeLoading: false", administration)
        self.assertIn('@click="loadAppScope()"', index)
        self.assertIn('spinning: appScopeLoading', index)
        self.assertIn('x-show="appScopeLoading"', index)

    def test_role_aware_startup_uses_only_normalized_identity_api(self):
        index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        notifications = (WEB_DIR / "js" / "features" / "notifications.js").read_text(encoding="utf-8")

        self.assertIn('await this._preferenceInitialization;', app_js)
        self.assertIn('requestJson("/api/me"', app_js)
        self.assertNotIn('/.auth/me', app_js)
        self.assertIn('if (this.user.capabilities.administer)', app_js)
        self.assertIn('if (!this.user.capabilities.administer) return;', notifications)
        self.assertIn('x-show="user.capabilities.administer"', index)
        self.assertIn("switchLocalRole(role)", index)
        self.assertIn("['Admin', 'User', 'None']", index)

    def test_feature_activation_is_loaded_once_and_gates_navigation(self):
        index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        feature = (
            WEB_DIR / "js" / "features" / "feature-activation.js"
        ).read_text(encoding="utf-8")
        web_bp = (WEB_DIR.parent / "functions" / "web_bp.py").read_text(encoding="utf-8")

        self.assertIn('src="js/features/feature-activation.js"', index)
        self.assertIn("selectTab('features')", index)
        self.assertIn("At least one feature must remain enabled.", index)
        self.assertIn("Allocation Events", index)
        self.assertIn("featureEnabled('odcr')", index)
        self.assertIn("featureEnabled('allocation_events')", index)
        self.assertIn("await this.loadFeatureSnapshot()", app_js)
        self.assertIn('requestJson("/api/features"', feature)
        self.assertNotIn("setInterval", feature)
        self.assertIn("this.adminFeatures = this.adminFeatures.map", feature)
        self.assertNotIn("this.featureSnapshot =", feature.split("async setFeatureEnabled", 1)[1])
        self.assertIn('"js/features/feature-activation.js"', web_bp)

    def test_allocation_events_vm_inventory_is_wired_to_shared_table_architecture(self):
        index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        scope_js = (WEB_DIR / "js" / "features" / "scope.js").read_text(encoding="utf-8")
        feature = (WEB_DIR / "js" / "features" / "allocation-events.js").read_text(encoding="utf-8")
        web_bp = (WEB_DIR.parent / "functions" / "web_bp.py").read_text(encoding="utf-8")

        self.assertIn('go(\'allocation-events\')', index)
        self.assertIn('page === \'allocation-events\'', index)
        self.assertIn('src="js/features/allocation-events.js"', index)
        self.assertIn('hash === "allocation-events"', app_js)
        self.assertIn("createAllocationEventsFeature()", app_js)
        self.assertIn('this.page === "allocation-events"', scope_js)
        self.assertIn("loadAllocationEventVms(true)", scope_js)
        self.assertIn('requestJson("/api/allocation-events/vms"', feature)
        self.assertIn("seedResourceTableFilterOptions(", feature)
        self.assertIn("filterTableRows(", feature)
        self.assertIn("searchTableRows(", feature)
        self.assertIn("sortTableRows(", feature)
        self.assertIn("paginateTableRows(", feature)
        self.assertIn('"js/features/allocation-events.js"', web_bp)
        self.assertIn(
            '<th class="tooltip-header" :aria-sort="allocationVmSort.key === \'deploymentType\'',
            index,
        )
        self.assertEqual(index.count('class="vm-table allocation-events-table'), 2)
        self.assertIn('class="odcr-usage-controls allocation-events-controls"', index)
        self.assertIn('aria-label="Allocation success period"', index)
        self.assertIn("<span>Allocation failures only</span>", index)
        self.assertIn("<span>VMs with failures only</span>", index)
        self.assertIn("cycleAllocationVmSort('allocationSuccess')", index)
        self.assertIn('class="allocation-success-cell"', index)
        self.assertIn("allocationVmLastAllocationText(vm)", index)
        self.assertIn("allocationVmLastFailureText(vm)", index)
        self.assertIn("openAllocationTimeline(vm, $event.currentTarget)", index)
        self.assertIn('id="allocation-timeline-title"', index)
        self.assertIn("allocationTimelineGroups", index)
        self.assertIn("allocationTimelineDetailEntries(event)", index)
        self.assertIn("allocationTimelineZoneDisplay(event)", index)
        self.assertIn("allocationTimelineErrorJson(event)", index)
        self.assertIn("event.error.hasAllocationFailure", index)
        self.assertIn("View details JSON", index)
        self.assertIn("Deployment zones", index)
        self.assertIn("event.recipe === 'current-state'", index)
        self.assertIn('requestJson("/api/allocation-events/timeline"', feature)
        self.assertIn("_allocationTimelineRequests.cancel()", feature)
        self.assertIn('allocationVmPeriod: "30d"', feature)
        self.assertIn("allocationVmConfirmedFailuresOnly: true", feature)
        self.assertIn("allocationVmFailuresOnly: false", feature)
        self.assertIn("toggleAllocationVmFailuresOnly()", feature)
        self.assertIn("confirmedAllocationFailures", feature)
        self.assertIn("formatDateTimeWithTimeZone", feature)
        self.assertIn("Intl.DateTimeFormat().resolvedOptions().timeZone", app_js)

        vm_styles = (WEB_DIR / "styles" / "vm-coverage.css").read_text(encoding="utf-8")
        self.assertRegex(
            vm_styles,
            r"\.allocation-events-table th:first-child,\s*"
            r"\.allocation-events-table td:first-child \{[^}]*"
            r"width: 230px;[^}]*min-width: 230px;[^}]*max-width: 230px;",
        )
        self.assertRegex(
            vm_styles,
            r"\.allocation-events-table th:nth-child\(2\),\s*"
            r"\.allocation-events-table td:nth-child\(2\) \{[^}]*left: 230px;",
        )
        self.assertIn(".allocation-period-switch", vm_styles)
        self.assertIn(".allocation-success-counts", vm_styles)
        self.assertIn(".allocation-success-rate", vm_styles)

        drawer_styles = (WEB_DIR / "styles" / "drawers.css").read_text(encoding="utf-8")
        self.assertIn(".allocation-timeline-drawer", drawer_styles)
        self.assertIn(".allocation-timeline-rail::after", drawer_styles)
        self.assertIn(".allocation-timeline-gap", drawer_styles)

    def test_global_filter_scroll_ownership_is_bounded(self):
        base_styles = (WEB_DIR / "styles" / "base.css").read_text(encoding="utf-8")

        self.assertRegex(base_styles, r"\.scope-menu \{[^}]*overflow: clip;")
        self.assertRegex(
            base_styles,
            r"\.scope-options \{[^}]*max-height: 240px;[^}]*overflow-y: auto;",
        )

    def test_coverage_compact_reports_keep_semantic_tables_and_responsive_charts(self):
        index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        coverage_styles = (WEB_DIR / "styles" / "vm-coverage.css").read_text(encoding="utf-8")
        responsive_styles = (WEB_DIR / "styles" / "responsive.css").read_text(encoding="utf-8")

        self.assertIn("coverage-legend-table", index)
        self.assertIn('class="coverage-legend-bullet"', index)
        self.assertIn('x-ref="coverageMarkingGroupedChart"', index)
        self.assertIn('x-ref="coveragePrerequisitesGroupedChart"', index)
        self.assertIn('x-ref="coverageAssociationGroupedChart"', index)
        self.assertIn('x-ref="coverageAssociationChart"', index)
        self.assertIn('x-ref="coverageUnassociationGroupedChart"', index)
        self.assertIn('x-ref="coverageUnassociationChart"', index)
        self.assertIn('class="coverage-report-block coverage-required-block"', index)
        self.assertIn('class="coverage-report-block coverage-not-required-block"', index)
        self.assertIn("ODCR Required", index)
        self.assertIn("ODCR not Required", index)
        self.assertIn("coverage-category-facets", index)
        self.assertIn('role="group" aria-label="ODCR Required coverage categories"', index)
        self.assertIn("coverageCategoryCount('required:undefined') > 0", index)
        self.assertIn("Required - Undefined", index)
        self.assertIn("setCoverageFacet('categories', 'coverageCategory', 'required:high', $event)", index)
        self.assertIn("setCoverageFacet('categories', 'coverageCategory', 'not_required', $event)", index)
        self.assertIn("moveCoverageUnmarked('required')", index)
        self.assertIn("moveCoverageUnmarked('not_required')", index)
        self.assertIn('aria-label="Move Unmarked to ODCR Required"', index)
        self.assertIn('aria-label="Move Unmarked to ODCR not Required"', index)
        self.assertNotIn("localStorage", (WEB_DIR / "js" / "features" / "vm-coverage.js").read_text(encoding="utf-8"))
        self.assertNotIn('type="checkbox" class="coverage-category', index)
        self.assertIn('class="coverage-compact-legend"', index)
        self.assertIn('class="coverage-none-visual"', index)
        self.assertIn('class="report-table coverage-association-table"', index)
        self.assertIn("coverageReportTitle('ODCR Association')", index)
        self.assertIn('class="coverage-review-indicator"', index)
        self.assertIn('class="report-table coverage-unassociation-table"', index)
        self.assertNotIn("ODCR Coverage Decision Alignment", index)
        self.assertIn("setCoverageFacet('coverage', 'dimension', row.key, $event)", index)
        self.assertIn("setCoverageFacet('marking', 'dimension', row.key, $event)", index)
        self.assertIn("setCoverageFacet('prerequisites', 'dimension', row.key, $event)", index)
        controls = re.search(r'<div class="coverage-report-controls">([\s\S]*?)</div>', index)
        self.assertIsNotNone(controls)
        self.assertLess(controls.group(1).find("coverage-clear"), controls.group(1).find("coverage-matrix-group-by"))
        self.assertIn('x-show="hasCoverageReportSelections"', controls.group(1))
        self.assertIn("Clear report filters.", controls.group(1))
        self.assertIn("coverageActiveFiltersText", controls.group(1))
        self.assertIn("&#8630;", controls.group(1))
        association_header = re.search(
            r'<table class="report-table coverage-association-table"[\s\S]*?<thead><tr>([\s\S]*?)</tr></thead>',
            index,
        )
        self.assertIsNotNone(association_header)
        self.assertNotIn(">VMs<", association_header.group(1))
        self.assertLess(association_header.group(1).find("coverageReportGroupLabel"), association_header.group(1).find(">Review<"))
        self.assertLess(association_header.group(1).find(">Review<"), association_header.group(1).find(">Assigned<"))
        unassociation_header = re.search(
            r'<table class="report-table coverage-unassociation-table"[\s\S]*?<thead><tr>([\s\S]*?)</tr></thead>',
            index,
        )
        self.assertIsNotNone(unassociation_header)
        self.assertNotIn(">VMs<", unassociation_header.group(1))
        self.assertNotIn(">Review<", unassociation_header.group(1))
        self.assertIn(">Unassociated<", unassociation_header.group(1))
        self.assertIn("row.assignedReferenceCount + ' / ' + row.referenceCount", index)
        self.assertIn("row.unassociatedReferenceCount + ' / ' + row.referenceCount", index)
        self.assertNotIn("coverage-mini-pie-label", index)
        self.assertIn("Reservation status", index)
        self.assertIn("report-table-two-column", index)
        self.assertIn('class="report-sort-label"', index)
        self.assertIn('title="Unsupported or unknown VMs"', index)
        self.assertIn("Unsup. or unkn. VMs", index)
        self.assertNotIn("Hide fully supported rows", index)
        self.assertIn("Coverage decision status", index)
        self.assertIn("Decision recorded", index)
        self.assertIn(".coverage-compact-report", coverage_styles)
        self.assertIn("grid-template-columns: 108px minmax(0, 1fr)", coverage_styles)
        self.assertIn(".coverage-none-visual", coverage_styles)
        self.assertIn(".coverage-association-table", coverage_styles)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", coverage_styles)
        self.assertIn('"required required not-required"', coverage_styles)
        self.assertIn('"required required marking"', coverage_styles)
        self.assertIn(".coverage-marking-tile { grid-area: marking; background: var(--surface); }", coverage_styles)
        self.assertIn("grid-template-rows: calc((100% + 34px) / 2) calc((100% - 34px) / 2)", coverage_styles)
        self.assertIn(".coverage-marking-tile .report-table thead th { background: var(--surface); }", coverage_styles)
        self.assertNotIn(".coverage-marking-grouped .report-table { font-size: 10px; }", coverage_styles)
        self.assertRegex(
            coverage_styles,
            r"\.coverage-none-visual \{[^}]*align-items: center;",
        )
        self.assertIn(".coverage-report-tile > .report-table-scroll { max-height: calc(100% - 31px); }", coverage_styles)
        self.assertIn('formatter: "{d}%"', (WEB_DIR / "js" / "features" / "vm-coverage-charts.js").read_text(encoding="utf-8"))
        self.assertIn(".report-table-two-column thead th:last-child .report-sort-header", coverage_styles)
        self.assertRegex(
            responsive_styles,
            r"@media \(max-width: 640px\)[\s\S]*?\.coverage-clear \{ flex: 1 0 100%; \}",
        )
        self.assertRegex(
            responsive_styles,
            r"@media \(max-width: 1180px\)[\s\S]*?\.coverage-compact-chart, \.coverage-compact-visual \{ display: none; \}",
        )

    def test_all_local_stylesheets_are_served_as_css(self):
        index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        stylesheets = re.findall(
            r'<link\b[^>]*\brel="stylesheet"[^>]*\bhref="([^"]+)"',
            index,
        )

        self.assertEqual(
            [
                "styles/base.css",
                "styles/controls.css",
                "styles/vm-coverage.css",
                "styles/drawers.css",
                "styles/responsive.css",
            ],
            stylesheets,
        )

        for stylesheet in stylesheets:
            with self.subTest(stylesheet=stylesheet):
                request = func.HttpRequest(
                    method="GET",
                    url=f"http://localhost/{stylesheet}",
                    headers={},
                    params={},
                    route_params={"path": stylesheet},
                    body=b"",
                )
                response = zzz_spa_fallback(request)

                self.assertEqual(200, response.status_code)
                self.assertEqual("text/css", response.mimetype)
                self.assertNotIn(b"<!DOCTYPE html>", response.get_body())

    def test_all_local_scripts_are_served_as_javascript(self):
        index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        scripts = re.findall(r'<script(?:\s+defer)?\s+src="([^"]+)"', index)

        self.assertIn("app.js", scripts)
        self.assertIn("js/features/administration.js", scripts)
        self.assertIn("js/features/data-collection.js", scripts)
        self.assertIn("js/features/coverage-editor.js", scripts)
        self.assertIn("js/features/coverage-report-model.js", scripts)
        self.assertIn("js/features/notifications.js", scripts)
        self.assertIn("js/features/odcr-usage-model.js", scripts)
        self.assertIn("js/features/odcr-usage.js", scripts)
        self.assertIn("js/features/scope.js", scripts)
        self.assertIn("js/features/vm-coverage.js", scripts)
        self.assertIn("js/features/vm-coverage-charts.js", scripts)
        self.assertIn("js/shared/resources.js", scripts)
        self.assertIn("js/shared/http.js", scripts)
        self.assertIn("js/shared/echarts-adapter.js", scripts)
        self.assertIn("js/shared/csv.js", scripts)
        self.assertIn("js/shared/preference-store.js", scripts)
        self.assertIn("js/shared/report-model.js", scripts)
        self.assertIn("js/shared/tables.js", scripts)

        for script in scripts:
            with self.subTest(script=script):
                request = func.HttpRequest(
                    method="GET",
                    url=f"http://localhost/{script}",
                    headers={},
                    params={},
                    route_params={"path": script},
                    body=b"",
                )
                response = zzz_spa_fallback(request)

                self.assertEqual(200, response.status_code)
                self.assertEqual("application/javascript", response.mimetype)
                self.assertNotIn(b"<!DOCTYPE html>", response.get_body())

    def test_unknown_frontend_path_falls_back_to_index(self):
        request = func.HttpRequest(
            method="GET",
            url="http://localhost/odcr/usage",
            headers={},
            params={},
            route_params={"path": "odcr/usage"},
            body=b"",
        )

        response = zzz_spa_fallback(request)

        self.assertEqual(200, response.status_code)
        self.assertEqual("text/html", response.mimetype)
        self.assertIn(b'<div class="layout" x-data="app()"', response.get_body())


if __name__ == "__main__":
    unittest.main()
