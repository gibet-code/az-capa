from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from functions.web_bp import WEB_DIR


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend model tests")
class ReportModelTests(unittest.TestCase):
    def run_model_scenario(self, scenario: str, scripts: list[str] | None = None) -> dict:
        script_paths = scripts or ["js/shared/report-model.js"]
        script_loaders = "\n".join(
            f'vm.runInThisContext(fs.readFileSync({json.dumps(str(WEB_DIR / path))}, "utf8"));'
            for path in script_paths
        )
        script = f"""
const fs = require("fs");
const vm = require("vm");
{script_loaders}
{scenario}
"""
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def test_business_context_fields_unwrap_generation_metadata(self):
        result = self.run_model_scenario("""
console.log(JSON.stringify(businessContextFields({ businessContext: {
  generation: "context-1",
  publishedAt: "2026-08-28T19:00:12.211Z",
  fields: { environment: "Environment", costCenter: "Cost Center" },
}})));
""", scripts=["js/shared/business-context.js"])

        self.assertEqual(
            [
                {"key": "environment", "name": "Environment"},
                {"key": "costCenter", "name": "Cost Center"},
            ],
            result,
        )

    def test_feature_composition_rejects_duplicate_properties(self):
        result = self.run_model_scenario("""
let message = "";
try {
  applyFeatures({ shared: 1 }, { distinct: 2 }, { shared: 3 });
} catch (error) {
  message = error.message;
}
console.log(JSON.stringify({ message }));
""", scripts=["app.js"])

        self.assertEqual("Duplicate Alpine feature property: shared", result["message"])

    def test_business_context_fields_support_legacy_flat_metadata(self):
        result = self.run_model_scenario("""
console.log(JSON.stringify(businessContextFields({ businessContext: {
  environment: "Environment",
}})));
""", scripts=["js/shared/business-context.js"])

        self.assertEqual([{"key": "environment", "name": "Environment"}], result)

    def test_location_options_use_display_labels_and_canonical_values(self):
        result = self.run_model_scenario("""
console.log(JSON.stringify(locationTableOptions([
  { location: "westus", locationDisplayName: "West US" },
  { location: "northeurope", locationDisplayName: "North Europe" },
])));
""", scripts=["js/shared/resources.js"])

        self.assertEqual(
            [
                {"value": "northeurope", "label": "North Europe"},
                {"value": "westus", "label": "West US"},
            ],
            result,
        )

    def test_location_report_groups_display_reference_name_with_canonical_key(self):
        result = self.run_model_scenario("""
const row = { location: "northeurope", locationDisplayName: "North Europe" };
const coverage = createVmCoverageFeature();
coverage.coverageReportGroupBy = "location";
console.log(JSON.stringify({
  coverage: coverage.coverageReportGroupDescriptor(row),
  usage: odcrUsageChartGroup(row, "location"),
}));
""", scripts=[
            "js/shared/resources.js",
            "js/shared/http.js",
            "js/shared/business-context.js",
            "js/features/vm-coverage.js",
            "js/features/odcr-usage-model.js",
        ])

        self.assertEqual(
            {"key": "location:northeurope", "label": "North Europe", "missing": False},
            result["coverage"],
        )
        self.assertEqual(
            {"name": "North Europe", "filterValue": "northeurope"},
            result["usage"],
        )

    def test_deployment_labels_follow_zone_display_mode(self):
        result = self.run_model_scenario("""
const feature = createVmCoverageFeature();
const zonal = { logicalZone: "1", physicalZone: "3" };
const unresolved = { logicalZone: "2", physicalZone: null };
const regional = { logicalZone: null, physicalZone: null };
const logical = [zonal, unresolved, regional].map((row) => ({
  filter: feature.deploymentFilterValue(row),
  display: feature.deploymentDisplay(row),
}));
feature.zoneDisplayMode = "physical";
const physical = [zonal, unresolved, regional].map((row) => ({
  filter: feature.deploymentFilterValue(row),
  display: feature.deploymentDisplay(row),
}));
console.log(JSON.stringify({ logical, physical }));
""", scripts=[
            "js/shared/resources.js",
            "js/shared/http.js",
            "js/features/vm-coverage.js",
        ])

        self.assertEqual(
            [
                {"filter": "Zone 1", "display": "Log Zone 1"},
                {"filter": "Zone 2", "display": "Log Zone 2"},
                {"filter": "Regional", "display": "Regional"},
            ],
            result["logical"],
        )
        self.assertEqual(
            [
                {"filter": "Zone 3", "display": "Phy Zone 3"},
                {"filter": "Unresolved", "display": "Unresolved"},
                {"filter": "Regional", "display": "Regional"},
            ],
            result["physical"],
        )

    def test_report_pagination_clamps_pages_and_slices_rows(self):
        result = self.run_model_scenario("""
const coverage = createVmCoverageFeature();
coverage.filteredVms = Array.from({ length: 45 }, (_, index) => ({ id: index + 1 }));
coverage.vmPageCount = 3;
coverage.setVmPage(9);
const vmState = {
  page: coverage.vmPage,
  ids: coverage.paginatedVms.map((row) => row.id),
  start: coverage.vmPageStart(),
  end: coverage.vmPageEnd(),
};

const usage = createOdcrUsageFeature();
usage.filteredOdcrUsageRows = Array.from({ length: 45 }, (_, index) => ({ id: index + 1 }));
usage.odcrUsagePageCount = 3;
usage.setOdcrUsagePage(2);
const odcr = {
  page: usage.odcrUsagePage,
  ids: usage.paginatedOdcrUsageRows.map((row) => row.id),
  start: usage.odcrUsagePageStart(),
  end: usage.odcrUsagePageEnd(),
};
console.log(JSON.stringify({ vm: vmState, odcr }));
""", scripts=[
            "js/shared/http.js",
      "js/shared/tables.js",
            "js/features/vm-coverage.js",
            "js/features/odcr-usage.js",
        ])

        self.assertEqual(3, result["vm"]["page"])
        self.assertEqual([41, 42, 43, 44, 45], result["vm"]["ids"])
        self.assertEqual(41, result["vm"]["start"])
        self.assertEqual(45, result["vm"]["end"])
        self.assertEqual(2, result["odcr"]["page"])
        self.assertEqual(list(range(21, 41)), result["odcr"]["ids"])
        self.assertEqual(21, result["odcr"]["start"])
        self.assertEqual(40, result["odcr"]["end"])

    def test_table_filter_and_search_helpers_share_common_semantics(self):
        result = self.run_model_scenario("""
const rows = [
  { id: 1, location: "westus", name: "Alpha" },
  { id: 2, location: "northeurope", name: "Bravo" },
  { id: 3, location: "westus", name: null },
];
const filters = [{
  key: "location",
  options: [{ value: "westus" }, { value: "northeurope" }],
  selected: ["westus"],
}];
const filtered = filterTableRows(rows, filters, (row, filter) =>
  filter.selected.includes(row[filter.key])
);
const searched = searchTableRows(filtered, "ALP", (row) => [row.name, row.location]);
console.log(JSON.stringify({
  filtered: filtered.map((row) => row.id),
  searched: searched.map((row) => row.id),
  emptyQuerySameReference: searchTableRows(rows, "  ", (row) => [row.name]) === rows,
}));
""", scripts=["js/shared/tables.js"])

        self.assertEqual([1, 3], result["filtered"])
        self.assertEqual([1], result["searched"])
        self.assertTrue(result["emptyQuerySameReference"])

    def test_csv_serialization_quotes_values_and_preserves_rows(self):
        result = self.run_model_scenario("""
const csv = serializeCsv([
  ["Name", (row) => row.name],
  ["Note", (row) => row.note],
  ["Empty", (row) => row.empty],
], [
  { name: "Alpha, Inc.", note: 'A "quoted" value', empty: null },
  { name: "Line break", note: "first\\nsecond", empty: undefined },
]);
console.log(JSON.stringify({ csv }));
""", scripts=["js/shared/csv.js"])

        self.assertEqual(
            '"Name","Note","Empty"\r\n'
            '"Alpha, Inc.","A ""quoted"" value",""\r\n'
            '"Line break","first\nsecond",""',
            result["csv"],
        )

    def test_request_json_normalizes_json_and_text_errors(self):
        result = self.run_model_scenario("""
const responses = [
  new Response(JSON.stringify({ message: "Scope unavailable" }), {
    status: 409,
    headers: { "Content-Type": "application/json" },
  }),
  new Response("Gateway unavailable", { status: 503 }),
  new Response(JSON.stringify({ value: [1, 2] }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }),
];
global.fetch = async () => responses.shift();
(async () => {
  const errors = [];
  for (let index = 0; index < 2; index += 1) {
    try { await requestJson("/api/test"); } catch (error) { errors.push(error.message); }
  }
  const data = await requestJson("/api/test");
  console.log(JSON.stringify({ errors, data }));
})();
""", scripts=["js/shared/http.js"])

        self.assertEqual(
            ["Scope unavailable (HTTP 409)", "Gateway unavailable (HTTP 503)"],
            result["errors"],
        )
        self.assertEqual({"value": [1, 2]}, result["data"])

    def test_latest_request_aborts_previous_and_tracks_current(self):
        result = self.run_model_scenario("""
const requests = createLatestRequest();
const first = requests.begin();
const second = requests.begin();
const beforeFinish = {
  firstAborted: first.signal.aborted,
  secondAborted: second.signal.aborted,
  firstCurrent: requests.isCurrent(first.id),
  secondCurrent: requests.isCurrent(second.id),
};
requests.finish(first.id);
const third = requests.begin();
console.log(JSON.stringify({
  beforeFinish,
  secondAbortedAfterThird: second.signal.aborted,
  thirdCurrent: requests.isCurrent(third.id),
}));
""", scripts=["js/shared/http.js"])

        self.assertEqual(
            {
                "firstAborted": True,
                "secondAborted": False,
                "firstCurrent": False,
                "secondCurrent": True,
            },
            result["beforeFinish"],
        )
        self.assertTrue(result["secondAbortedAfterThird"])
        self.assertTrue(result["thirdCurrent"])

    def test_business_context_filter_keeps_identity_when_options_are_reseeded(self):
        result = self.run_model_scenario("""
const existing = {
  key: "businessContext:itOrganization",
  businessContextKey: "itOrganization",
  label: "IT Organization",
  clientOnly: true,
  searchable: true,
  showSelectAll: true,
  options: [
    { value: "IT INFRASTRUCTURE", label: "IT INFRASTRUCTURE" },
    { value: "IT OPERATIONS & FINANCE", label: "IT OPERATIONS & FINANCE" },
  ],
  selected: ["IT INFRASTRUCTURE", "IT OPERATIONS & FINANCE"],
};
const filters = syncBusinessContextFilters(
  [{ key: "subscriptionId", options: [], selected: [] }, existing],
  { businessContext: { fields: { itOrganization: "IT Organization" } } },
);
seedBusinessContextFilterOptions(filters, { businessContext: {
  fields: { itOrganization: "IT Organization" },
}}, [{ businessContext: { itOrganization: "IT INFRASTRUCTURE" } }]);
const current = filters.find((filter) => filter.businessContextKey === "itOrganization");
console.log(JSON.stringify({
  sameObject: current === existing,
  options: current.options.map((option) => option.value),
  selected: current.selected,
}));
""", scripts=["js/shared/tables.js", "js/shared/business-context.js"])

        self.assertEqual(
            {
                "sameObject": True,
                "options": ["IT INFRASTRUCTURE"],
                "selected": ["IT INFRASTRUCTURE"],
            },
            result,
        )

    def test_global_scope_marks_only_corresponding_local_filters(self):
        result = self.run_model_scenario("""
const feature = createScopeFeature();
feature.globalScope = {
  subscriptionIds: null,
  locations: ["westeurope"],
  contextFilters: [{ fieldKey: "itOrganization", values: ["Operations"] }],
};
console.log(JSON.stringify({
  subscription: feature.globalScopeAppliesToFilter({ key: "subscriptionId" }),
  location: feature.globalScopeAppliesToFilter({ key: "location" }),
  itOrganization: feature.globalScopeAppliesToFilter({
    key: "businessContext:itOrganization",
    businessContextKey: "itOrganization",
  }),
  environment: feature.globalScopeAppliesToFilter({
    key: "businessContext:environment",
    businessContextKey: "environment",
  }),
}));
""", scripts=[
      "js/shared/http.js",
      "js/features/scope.js",
    ])

        self.assertEqual(
            {
                "subscription": False,
                "location": True,
                "itOrganization": True,
                "environment": False,
            },
            result,
        )

    def test_frontend_preferences_validate_partition_and_round_trip(self):
        result = self.run_model_scenario("""
const values = new Map();
global.window = { localStorage: {
  getItem: (key) => values.has(key) ? values.get(key) : null,
  setItem: (key, value) => values.set(key, value),
  removeItem: (key) => values.delete(key),
}};

const feature = createPreferenceStoreFeature();
feature.initializePreferences({ local: true });
feature.persistGlobalScope({
  subscriptionIds: ["sub-1"],
  locations: ["westeurope"],
  contextFilters: [{ fieldKey: "environment", values: ["Production", null] }],
});
feature.persistCoverageUnmarkedOwner("not_required");

const restored = createPreferenceStoreFeature();
restored.initializePreferences({ local: true });
const partiallyInvalid = validateFrontendPreferenceRecord({
  schemaVersion: 1,
  globalScope: { subscriptionIds: [], locations: null, contextFilters: [] },
  coverageUnmarkedOwner: "not_required",
});

console.log(JSON.stringify({
  localKey: frontendPreferenceStorageKey({ local: true }),
  productionKey: frontendPreferenceStorageKey({
    local: false, tenantId: "tenant-1", objectId: "object-1",
  }),
  missingIdentityKey: frontendPreferenceStorageKey({ local: false, tenantId: "tenant-1" }),
  restoredScope: restored.globalScope,
  restoredOwner: restored.coverageUnmarkedOwner,
  partiallyInvalid,
  unknownVersion: validateFrontendPreferenceRecord({ schemaVersion: 2 }),
}));
""", scripts=["js/shared/preference-store.js"])

        self.assertEqual("az-capacity.frontend-preferences.v1.local", result["localKey"])
        self.assertEqual(
            "az-capacity.frontend-preferences.v1.tenant-1.object-1",
            result["productionKey"],
        )
        self.assertIsNone(result["missingIdentityKey"])
        self.assertEqual(
            {
                "subscriptionIds": ["sub-1"],
                "locations": ["westeurope"],
                "contextFilters": [
                    {"fieldKey": "environment", "values": ["Production", None]},
                ],
            },
            result["restoredScope"],
        )
        self.assertEqual("not_required", result["restoredOwner"])
        self.assertEqual(
            {"subscriptionIds": None, "locations": None, "contextFilters": []},
            result["partiallyInvalid"]["globalScope"],
        )
        self.assertEqual("not_required", result["partiallyInvalid"]["coverageUnmarkedOwner"])
        self.assertIsNone(result["unknownVersion"])

    def test_frontend_preferences_fail_open_when_storage_throws(self):
        result = self.run_model_scenario("""
global.window = { localStorage: {
  getItem: () => { throw new Error("blocked"); },
  setItem: () => { throw new Error("blocked"); },
  removeItem: () => { throw new Error("blocked"); },
}};
console.warn = () => {};
const feature = createPreferenceStoreFeature();
feature.initializePreferences({ local: true });
feature.persistGlobalScope({ subscriptionIds: ["sub-1"], locations: null, contextFilters: [] });
feature.persistCoverageUnmarkedOwner("not_required");
console.log(JSON.stringify({
  initialized: feature.preferenceStoreInitialized,
  scope: feature.globalScope,
  owner: feature.coverageUnmarkedOwner,
}));
""", scripts=["js/shared/preference-store.js"])

        self.assertEqual(
            {
                "initialized": True,
                "scope": {"subscriptionIds": None, "locations": None, "contextFilters": []},
                "owner": "required",
            },
            result,
        )

    def test_initial_report_waits_for_restored_preferences(self):
        result = self.run_model_scenario("""
const stored = JSON.stringify({
  schemaVersion: 1,
  globalScope: { subscriptionIds: ["sub-1"], locations: null, contextFilters: [] },
  coverageUnmarkedOwner: "not_required",
});
global.window = {
  location: { hash: "#/odcr/usage" },
  addEventListener: () => {},
  localStorage: {
    getItem: (key) => key === "az-capacity.frontend-preferences.v1.local" ? stored : null,
    setItem: () => {},
    removeItem: () => {},
  },
};
global.fetch = async (url) => {
  if (url === "/api/me") return {
    ok: true,
    json: async () => ({
      local: true,
      capabilities: { useOdcr: true, administer: false },
    }),
  };
  throw new Error(`Unexpected fetch: ${url}`);
};
global.setInterval = () => 0;
global.resourceTypeIcon = () => "";
global.useGenericResourceIcon = () => {};
global.createNotificationsFeature = () => ({
  notificationDrawerOpen: false,
  loadNotifications() {},
});
global.createAdministrationFeature = () => ({
  loadSettings() {},
  loadBusinessContextFields() {},
});
global.createDataCollectionFeature = () => ({
  statuses: {},
  loadPipelines() {},
});
global.createVmCoverageFeature = () => ({
  vmsLoaded: false,
  coverageUnmarkedOwner: "required",
  loadVms() { this.reportCalls.push({ kind: "coverage", scope: this.globalScopeRequest() }); },
});
global.createVmCoverageChartsFeature = () => ({ resizeCoverageReportCharts() {} });
global.createCoverageEditorFeature = () => ({});
global.createOdcrUsageFeature = () => ({
  odcrUsageLoaded: false,
  loadOdcrUsage() { this.reportCalls.push({ kind: "usage", scope: this.globalScopeRequest() }); },
  scheduleOdcrUsageChartRender() {},
});

const root = app();
root.reportCalls = [];
root.init();
const callsBeforeInitialization = root.reportCalls.length;
root._preferenceInitialization.then(() => {
  console.log(JSON.stringify({
    callsBeforeInitialization,
    calls: root.reportCalls,
    owner: root.coverageUnmarkedOwner,
  }));
});
""", scripts=[
            "js/shared/preference-store.js",
            "js/shared/http.js",
            "js/features/scope.js",
            "app.js",
        ])

        self.assertEqual(0, result["callsBeforeInitialization"])
        self.assertEqual(
            [{"kind": "usage", "scope": {"subscriptionIds": ["sub-1"]}}],
            result["calls"],
        )
        self.assertEqual("not_required", result["owner"])

    def test_initial_report_reconciles_unavailable_context_fields(self):
        result = self.run_model_scenario("""
const writes = [];
const stored = JSON.stringify({
  schemaVersion: 1,
  globalScope: {
    subscriptionIds: ["sub-1"],
    locations: null,
    contextFilters: [
      { fieldKey: "deleted_field", values: ["Old"] },
      { fieldKey: "environment", values: ["Removed value"] },
    ],
  },
  coverageUnmarkedOwner: "required",
});
global.window = {
  location: { hash: "#/odcr/coverage" },
  addEventListener: () => {},
  localStorage: {
    getItem: (key) => key === "az-capacity.frontend-preferences.v1.local" ? stored : null,
    setItem: (key, value) => writes.push(JSON.parse(value)),
    removeItem: () => {},
  },
};
global.fetch = async (url) => {
  if (url === "/api/me") return {
    ok: true,
    json: async () => ({ local: true, capabilities: { useOdcr: true, administer: false } }),
  };
  if (url === "/api/global-filters/catalogue") return {
    ok: true,
    json: async () => ({
      subscriptions: [], locations: [],
      contextFields: [{ fieldKey: "environment", name: "Environment", values: [] }],
    }),
  };
  throw new Error(`Unexpected fetch: ${url}`);
};
global.setInterval = () => 0;
global.resourceTypeIcon = () => "";
global.useGenericResourceIcon = () => {};
global.createNotificationsFeature = () => ({ notificationDrawerOpen: false, loadNotifications() {} });
global.createAdministrationFeature = () => ({
  loadSettings() {}, loadBusinessContextFields() {},
});
global.createDataCollectionFeature = () => ({ statuses: {}, loadPipelines() {} });
global.createVmCoverageFeature = () => ({
  vmsLoaded: false,
  coverageUnmarkedOwner: "required",
  loadVms() { this.reportCalls.push(this.globalScopeRequest()); },
});
global.createVmCoverageChartsFeature = () => ({ resizeCoverageReportCharts() {} });
global.createCoverageEditorFeature = () => ({});
global.createOdcrUsageFeature = () => ({
  odcrUsageLoaded: false, loadOdcrUsage() {}, scheduleOdcrUsageChartRender() {},
});

const root = app();
root.reportCalls = [];
root.init();
root._preferenceInitialization.then(() => {
  console.log(JSON.stringify({
    reportCalls: root.reportCalls,
    persistedScope: writes[0].globalScope,
    notifications: root.toasts.map((toast) => toast.message),
  }));
});
""", scripts=[
            "js/shared/preference-store.js",
            "js/shared/http.js",
            "js/features/scope.js",
            "app.js",
        ])

        expected_scope = {
            "subscriptionIds": ["sub-1"],
            "locations": None,
            "contextFilters": [
                {"fieldKey": "environment", "values": ["Removed value"]},
            ],
        }
        self.assertEqual([{"subscriptionIds": ["sub-1"], "contextFilters": expected_scope["contextFilters"]}], result["reportCalls"])
        self.assertEqual(expected_scope, result["persistedScope"])
        self.assertEqual(
            ["Removed unavailable global Context filter: deleted_field."],
            result["notifications"],
        )

    def test_catalogue_failure_preserves_restored_context_filters(self):
        result = self.run_model_scenario("""
const stored = JSON.stringify({
  schemaVersion: 1,
  globalScope: {
    subscriptionIds: null, locations: null,
    contextFilters: [{ fieldKey: "deleted_field", values: ["Old"] }],
  },
  coverageUnmarkedOwner: "required",
});
global.window = {
  location: { hash: "#/odcr/coverage" },
  addEventListener: () => {},
  localStorage: {
    getItem: (key) => key === "az-capacity.frontend-preferences.v1.local" ? stored : null,
    setItem: () => { throw new Error("Preference must not be rewritten"); },
    removeItem: () => {},
  },
};
global.fetch = async (url) => {
  if (url === "/api/me") return {
    ok: true,
    json: async () => ({ local: true, capabilities: { useOdcr: true, administer: false } }),
  };
  if (url === "/api/global-filters/catalogue") throw new Error("Catalogue unavailable");
  throw new Error(`Unexpected fetch: ${url}`);
};
global.setInterval = () => 0;
global.resourceTypeIcon = () => "";
global.useGenericResourceIcon = () => {};
global.createNotificationsFeature = () => ({ notificationDrawerOpen: false, loadNotifications() {} });
global.createAdministrationFeature = () => ({
  loadSettings() {}, loadBusinessContextFields() {},
});
global.createDataCollectionFeature = () => ({ statuses: {}, loadPipelines() {} });
global.createVmCoverageFeature = () => ({
  vmsLoaded: false,
  coverageUnmarkedOwner: "required",
  loadVms() { this.reportCalls.push(this.globalScopeRequest()); },
});
global.createVmCoverageChartsFeature = () => ({ resizeCoverageReportCharts() {} });
global.createCoverageEditorFeature = () => ({});
global.createOdcrUsageFeature = () => ({
  odcrUsageLoaded: false, loadOdcrUsage() {}, scheduleOdcrUsageChartRender() {},
});

const root = app();
root.reportCalls = [];
root.init();
root._preferenceInitialization.then(() => console.log(JSON.stringify({
  reportCalls: root.reportCalls,
  scopeInventoryError: root.scopeInventoryError,
})));
""", scripts=[
            "js/shared/preference-store.js",
            "js/shared/http.js",
            "js/features/scope.js",
            "app.js",
        ])

        self.assertEqual(
            [{"contextFilters": [{"fieldKey": "deleted_field", "values": ["Old"]}]}],
            result["reportCalls"],
        )
        self.assertEqual("Catalogue unavailable", result["scopeInventoryError"])

    def test_remove_all_global_filters_persists_and_reloads_active_report(self):
        result = self.run_model_scenario("""
const feature = createScopeFeature();
feature.globalScope = {
  subscriptionIds: ["sub-1"], locations: ["westeurope"],
  contextFilters: [{ fieldKey: "environment", values: ["Production"] }],
};
feature.globalScopeDraft = JSON.parse(JSON.stringify(feature.globalScope));
feature.scopeMenuOpen = true;
feature.odcrTab = "usage";
feature.vmsLoaded = true;
feature.persisted = [];
feature.persistGlobalScope = (scope) => feature.persisted.push(JSON.parse(JSON.stringify(scope)));
feature.loadOdcrUsage = async (force) => { feature.reloadForce = force; };
feature.removeAllGlobalScope().then(() => console.log(JSON.stringify({
  scope: feature.globalScope,
  draft: feature.globalScopeDraft,
  persisted: feature.persisted,
  scopeMenuOpen: feature.scopeMenuOpen,
  vmsLoaded: feature.vmsLoaded,
  reloadForce: feature.reloadForce,
})));
""", scripts=["js/shared/http.js", "js/features/scope.js"])

        unrestricted = {"subscriptionIds": None, "locations": None, "contextFilters": []}
        self.assertEqual(unrestricted, result["scope"])
        self.assertEqual(unrestricted, result["draft"])
        self.assertEqual([unrestricted], result["persisted"])
        self.assertFalse(result["scopeMenuOpen"])
        self.assertFalse(result["vmsLoaded"])
        self.assertTrue(result["reloadForce"])

    def test_facets_or_within_and_across(self):
        result = self.run_model_scenario("""
const model = createReportModel({ facetMatchers: {
  subscription: (row, value) => row.subscription === value,
  status: (row, value) => row.status === value,
}});
model.setSourceRecords([
  { id: 1, subscription: "A", status: "Supported" },
  { id: 2, subscription: "B", status: "Supported" },
  { id: 3, subscription: "B", status: "Unknown" },
  { id: 4, subscription: "C", status: "Supported" },
]);
model.applySelection({ facet: "subscription", operation: "replace", values: ["A", "B"] });
const snapshot = model.applySelection({ facet: "status", operation: "replace", value: "Supported" });
console.log(JSON.stringify({
  reference: snapshot.referenceRecords.map((row) => row.id),
  focus: snapshot.focusRecords.map((row) => row.id),
  selections: snapshot.selections,
}));
""")

        self.assertEqual([1, 2, 3, 4], result["reference"])
        self.assertEqual([1, 2], result["focus"])
        self.assertEqual(
            {"subscription": ["A", "B"], "status": ["Supported"]},
            result["selections"],
        )

    def test_compound_selection_publishes_one_revision(self):
        result = self.run_model_scenario("""
const model = createReportModel({ facetMatchers: {
  dimension: (row, value) => row.dimension === value,
  category: (row, value) => row.category === value,
}});
model.setSourceRecords([
  { id: 1, dimension: "A", category: "High" },
  { id: 2, dimension: "A", category: "Low" },
  { id: 3, dimension: "B", category: "High" },
]);
const revisions = [];
model.subscribe((snapshot) => revisions.push(snapshot.revision));
const snapshot = model.applySelection([
  { facet: "dimension", operation: "replace", value: "A" },
  { facet: "category", operation: "replace", value: "High" },
]);
console.log(JSON.stringify({
  revisions,
  revision: snapshot.revision,
  focus: snapshot.focusRecords.map((row) => row.id),
}));
""")

        self.assertEqual([2], result["revisions"])
        self.assertEqual(2, result["revision"])
        self.assertEqual([1], result["focus"])

    def test_population_can_exclude_visual_owned_facets(self):
        result = self.run_model_scenario("""
const model = createReportModel({ facetMatchers: {
  subscription: (row, value) => row.subscription === value,
  status: (row, value) => row.status === value,
}});
model.setSourceRecords([
  { id: 1, subscription: "A", status: "Supported" },
  { id: 2, subscription: "A", status: "Unknown" },
  { id: 3, subscription: "B", status: "Supported" },
]);
model.applySelection([
  { facet: "subscription", operation: "replace", value: "A" },
  { facet: "status", operation: "replace", value: "Supported" },
]);
console.log(JSON.stringify({
  all: model.getSnapshot().focusRecords.map((row) => row.id),
  withoutStatus: model.getPopulationExcluding("status").map((row) => row.id),
  withoutSubscription: model.getPopulationExcluding(["subscription"]).map((row) => row.id),
}));
""")

        self.assertEqual([1], result["all"])
        self.assertEqual([1, 2], result["withoutStatus"])
        self.assertEqual([1, 3], result["withoutSubscription"])

    def test_reference_population_is_stable_across_selection_changes(self):
        result = self.run_model_scenario("""
const records = [
  { id: 1, inPageScope: true, status: "Supported" },
  { id: 2, inPageScope: true, status: "Unknown" },
  { id: 3, inPageScope: false, status: "Supported" },
];
const model = createReportModel({ facetMatchers: {
  status: (row, value) => row.status === value,
}});
model.setSourceRecords(records, (row) => row.inPageScope);
const selected = model.applySelection({ facet: "status", operation: "replace", value: "Supported" });
const cleared = model.clearSelections();
console.log(JSON.stringify({
  selectedReference: selected.referenceRecords.map((row) => row.id),
  selectedFocus: selected.focusRecords.map((row) => row.id),
  clearedReference: cleared.referenceRecords.map((row) => row.id),
  clearedFocus: cleared.focusRecords.map((row) => row.id),
  sourceUnchanged: records.map((row) => row.id),
}));
""")

        self.assertEqual([1, 2], result["selectedReference"])
        self.assertEqual([1], result["selectedFocus"])
        self.assertEqual([1, 2], result["clearedReference"])
        self.assertEqual([1, 2], result["clearedFocus"])
        self.assertEqual([1, 2, 3], result["sourceUnchanged"])

    def test_odcr_association_and_sparse_prerequisite_aggregation(self):
        result = self.run_model_scenario("""
const metadata = { odcrRequirements: { defaultStatus: "Passed", definitions: [
  { key: "sku", label: "SKU support" },
  { key: "host", label: "No host" },
  { key: "unused", label: "Always passed" },
] } };
const rows = [
  { id: 1, subscriptionId: "A", odcrCoverage: { decision: "required", priority: "high" },
    reservationStatus: "Allocated, within capacity", odcrSupportabilityStatus: "Supported", odcrRequirements: [] },
  { id: 2, subscriptionId: "A", odcrCoverage: { decision: "not_required" },
    reservationStatus: "Not associated", odcrSupportabilityStatus: "Unsupported",
    odcrRequirements: [{ key: "sku", status: "Failed" }, { key: "host", status: "Unknown" }] },
  { id: 3, subscriptionId: "B", odcrCoverage: null,
    reservationStatus: "Not associated", odcrSupportabilityStatus: "Unknown",
    odcrRequirements: [{ key: "sku", status: "Unknown" }] },
  { id: 4, subscriptionId: "B", odcrCoverage: { decision: "required", priority: "low" },
    reservationStatus: "Allocated, within capacity", odcrSupportabilityStatus: "Unsupported",
    odcrRequirements: [{ key: "sku", status: "Failed" }] },
];
const view = buildCoverageReportViewModel({
  referenceRows: rows,
  focusRows: [rows[1]],
  metadata,
  groupBy: "subscription",
  groupDescriptor: (row) => ({ key: row.subscriptionId, label: row.subscriptionId, missing: false }),
});
console.log(JSON.stringify({
  associationTotal: view.coverage.total,
  associationRows: view.coverage.rows,
  unassociationTotal: view.unassociation.total,
  prerequisiteTotal: view.prerequisites.total,
  prerequisiteRows: view.prerequisites.rows,
  issues: view.prerequisiteIssues.rows,
}));
""", scripts=["js/features/coverage-report-model.js"])

        self.assertEqual(4, result["associationTotal"]["referenceCount"])
        self.assertEqual(2, result["associationTotal"]["assignedReferenceCount"])
        self.assertEqual(50, result["associationTotal"]["assignedPercent"])
        self.assertIsNone(result["associationTotal"]["reviewReferenceCount"])
        self.assertEqual(2, result["unassociationTotal"]["unassociatedReferenceCount"])
        self.assertEqual(50, result["unassociationTotal"]["unassociatedPercent"])
        self.assertEqual(
          [("A", 1, 2), ("B", 1, 2)],
          sorted(
            (row["key"], row["assignedReferenceCount"], row["referenceCount"])
            for row in result["associationRows"]
          ),
        )
        self.assertEqual(2, result["prerequisiteTotal"]["statuses"]["Unsupported"]["referenceCount"])
        self.assertEqual(1, result["prerequisiteTotal"]["statuses"]["Unknown"]["referenceCount"])
        self.assertEqual(
            [1, 2],
            sorted(row["unsupportedOrUnknownCount"] for row in result["prerequisiteRows"]),
        )
        self.assertEqual(
          [("sku", 3), ("host", 1)],
            [
            (row["key"], row["referenceCount"])
                for row in result["issues"]
            ],
        )
        self.assertNotIn("unused", [row["key"] for row in result["issues"]])

    def test_coverage_tiles_use_independent_cross_filtered_populations(self):
        result = self.run_model_scenario("""
const rows = [
  { id: 1, odcrCoverage: { decision: "required", priority: "high" },
    reservationStatus: "Allocated, within capacity", odcrSupportabilityStatus: "Supported", odcrRequirements: [] },
  { id: 2, odcrCoverage: { decision: "required", priority: "medium" },
    reservationStatus: "Not associated", odcrSupportabilityStatus: "Unsupported", odcrRequirements: [] },
  { id: 3, odcrCoverage: null,
    reservationStatus: "Not associated", odcrSupportabilityStatus: "Unknown", odcrRequirements: [] },
];
const view = buildCoverageReportViewModel({
  referenceRows: rows,
  focusRows: [rows[0]],
  visualPopulations: {
    marking: rows.slice(0, 2),
    coverage: rows,
    coverageDetails: [rows[0]],
    prerequisites: rows.slice(0, 2),
    prerequisiteIssues: [rows[1]],
  },
  groupBy: "none",
});
console.log(JSON.stringify({
  focusCount: view.focusedCount,
  markingCount: view.marking.populationCount,
  coverageCount: view.coverage.populationCount,
  detailsCount: view.coverageDetails.populationCount,
  prerequisiteCount: view.prerequisites.populationCount,
  issueCount: view.prerequisiteIssues.populationCount,
  markingTotal: view.marking.total,
  associationTotal: view.coverage.total,
}));
""", scripts=["js/features/coverage-report-model.js"])

        self.assertEqual(1, result["focusCount"])
        self.assertEqual(2, result["markingCount"])
        self.assertEqual(3, result["coverageCount"])
        self.assertEqual(1, result["detailsCount"])
        self.assertEqual(2, result["prerequisiteCount"])
        self.assertEqual(1, result["issueCount"])
        self.assertEqual(2, result["markingTotal"]["markedReferenceCount"])
        self.assertEqual(2, result["markingTotal"]["referenceCount"])
        self.assertEqual(3, result["associationTotal"]["referenceCount"])
        self.assertEqual(1, result["associationTotal"]["assignedReferenceCount"])

    def test_odcr_association_rows_sort_by_percentage(self):
        result = self.run_model_scenario("""
const feature = createVmCoverageFeature();
feature.coverageReportView = { coverage: {
  rows: [
    { key: "a", label: "A", referenceCount: 10, assignedReferenceCount: 8, assignedPercent: 80, reviewReferenceCount: null },
    { key: "b", label: "B", referenceCount: 5, assignedReferenceCount: 4, assignedPercent: 80, reviewReferenceCount: null },
    { key: "c", label: "C", referenceCount: 8, assignedReferenceCount: 2, assignedPercent: 25, reviewReferenceCount: null },
  ],
}, unassociation: {
  rows: [
    { key: "a", label: "A", referenceCount: 10, unassociatedReferenceCount: 2, unassociatedPercent: 20 },
    { key: "b", label: "B", referenceCount: 5, unassociatedReferenceCount: 1, unassociatedPercent: 20 },
    { key: "c", label: "C", referenceCount: 8, unassociatedReferenceCount: 6, unassociatedPercent: 75 },
  ],
} };
const rows = () => feature.coverageSortedRows("coverage").map((row) => row.label);
const percent = rows();
feature.cycleCoverageReportSort("coverage", "review");
const review = rows();
feature.cycleCoverageReportSort("coverage", "label");
const label = rows();
const unassociationPercent = feature.coverageSortedRows("unassociation").map((row) => row.label);
console.log(JSON.stringify({ percent, review, label, unassociationPercent }));
""", scripts=[
      "js/shared/http.js",
      "js/features/vm-coverage.js",
    ])

        self.assertEqual(["A", "B", "C"], result["percent"])
        self.assertEqual(["A", "B", "C"], result["review"])
        self.assertEqual(["C", "B", "A"], result["label"])
        self.assertEqual(["C", "A", "B"], result["unassociationPercent"])

    def test_coverage_progressive_filtering_tracks_latest_visual_context(self):
        result = self.run_model_scenario("""
const feature = createVmCoverageFeature();
feature.coverageReportGroupBy = "location";
feature.vmReportBaseRows = [
  { id: 1, location: "A", odcrCoverage: null, reservationStatus: "Allocated, within capacity", odcrSupportabilityStatus: "Supported", odcrRequirements: [] },
  { id: 2, location: "A", odcrCoverage: null, reservationStatus: "Not associated", odcrSupportabilityStatus: "Unsupported", odcrRequirements: [] },
  { id: 3, location: "B", odcrCoverage: null, reservationStatus: "Not associated", odcrSupportabilityStatus: "Unsupported", odcrRequirements: [] },
];
feature.rebuildVmView = function() { this.syncCoverageReportModel(); };
feature.syncCoverageReportModel();
const labels = (rows) => rows.map((row) => row.label);

feature.setCoverageFacet("coverage", "dimension", "location:a");
const afterLocation = {
  coverage: labels(feature.coverageReportView.coverage.rows),
  marking: labels(feature.coverageReportView.marking.rows),
  prerequisites: labels(feature.coverageReportView.prerequisites.rows),
};

feature.setCoverageFacet("prerequisites", "prerequisiteStatus", "Unsupported");
const afterPrerequisite = {
  coverage: labels(feature.coverageReportView.coverage.rows),
  marking: labels(feature.coverageReportView.marking.rows),
  prerequisites: labels(feature.coverageReportView.prerequisites.rows),
};

feature.setCoverageFacet("coverage", "dimension", "location:a", { ctrlKey: true });
const afterControlRemove = {
  selections: feature.coverageFacetSelections,
  context: feature.coverageReportEditingContext,
};

feature.setCoverageFacet("coverage", "dimension", "location:b");
feature.setCoverageFacet("prerequisites", "prerequisiteStatus", "Unsupported");
feature.setCoverageFacet("coverage", "dimension", "location:b");
const afterRestart = {
  selections: feature.coverageFacetSelections,
  context: feature.coverageReportEditingContext,
};

console.log(JSON.stringify({ afterLocation, afterPrerequisite, afterControlRemove, afterRestart }));
""", scripts=[
            "js/shared/http.js",
            "js/shared/resources.js",
            "js/shared/report-model.js",
            "js/features/coverage-report-model.js",
            "js/features/vm-coverage.js",
        ])

        self.assertEqual(["A", "B"], result["afterLocation"]["coverage"])
        self.assertEqual(["A"], result["afterLocation"]["marking"])
        self.assertEqual(["A"], result["afterLocation"]["prerequisites"])
        self.assertEqual(["A"], result["afterPrerequisite"]["coverage"])
        self.assertEqual(["A"], result["afterPrerequisite"]["marking"])
        self.assertEqual(["A"], result["afterPrerequisite"]["prerequisites"])
        self.assertEqual([], result["afterControlRemove"]["selections"]["dimension"])
        self.assertEqual(
            ["Unsupported"],
            result["afterControlRemove"]["selections"]["prerequisiteStatus"],
        )
        self.assertEqual(
            {"visual": "prerequisites", "facets": ["prerequisiteStatus"]},
            result["afterControlRemove"]["context"],
        )
        self.assertEqual({"dimension": ["location:b"]}, result["afterRestart"]["selections"])
        self.assertEqual(
            {"visual": "coverage", "facets": ["dimension"]},
            result["afterRestart"]["context"],
        )

    def test_coverage_category_filters_globally_and_prefilters_block_populations(self):
        result = self.run_model_scenario("""
const feature = createVmCoverageFeature();
feature.coverageReportGroupBy = "none";
feature.vmReportBaseRows = [
  { id: 1, odcrCoverage: { decision: "required", priority: "high" }, reservationStatus: "Allocated, within capacity", odcrSupportabilityStatus: "Supported", odcrRequirements: [] },
  { id: 2, odcrCoverage: { decision: "required", priority: "low" }, reservationStatus: "Not associated", odcrSupportabilityStatus: "Unsupported", odcrRequirements: [] },
  { id: 3, odcrCoverage: null, reservationStatus: "Not associated", odcrSupportabilityStatus: "Unknown", odcrRequirements: [] },
  { id: 4, odcrCoverage: { decision: "not_required" }, reservationStatus: "Allocated, within capacity", odcrSupportabilityStatus: "Supported", odcrRequirements: [] },
  { id: 5, odcrCoverage: { decision: "required", priority: "unexpected" }, reservationStatus: "Not associated", odcrSupportabilityStatus: "Unknown", odcrRequirements: [] },
];
feature.rebuildVmView = function() { this.syncCoverageReportModel(); };
feature.syncCoverageReportModel();
const initial = {
  focus: feature.coverageReportFocusRows.map((row) => row.id),
  required: feature.coverageReportView.coverage.populationCount,
  notRequired: feature.coverageReportView.unassociation.populationCount,
  categories: feature.coverageCategoryCounts,
};
feature.setCoverageFacet("categories", "coverageCategory", "required:high");
const selected = {
  focus: feature.coverageReportFocusRows.map((row) => row.id),
  required: feature.coverageReportView.coverage.populationCount,
  notRequired: feature.coverageReportView.unassociation.populationCount,
  categories: feature.coverageCategoryCounts,
};
console.log(JSON.stringify({ initial, selected }));
""", scripts=[
            "js/shared/http.js",
            "js/shared/report-model.js",
            "js/features/coverage-report-model.js",
            "js/features/vm-coverage.js",
        ])

        self.assertEqual([1, 2, 3, 4, 5], result["initial"]["focus"])
        self.assertEqual(4, result["initial"]["required"])
        self.assertEqual(1, result["initial"]["notRequired"])
        self.assertEqual({"required:high": 1, "required:medium": 0, "required:low": 1, "required:undefined": 1, "unmarked": 1, "not_required": 1}, result["initial"]["categories"])
        self.assertEqual([1], result["selected"]["focus"])
        self.assertEqual(1, result["selected"]["required"])
        self.assertEqual(0, result["selected"]["notRequired"])
        self.assertEqual(result["initial"]["categories"], result["selected"]["categories"])

    def test_coverage_category_interactions_and_transient_unmarked_ownership(self):
        result = self.run_model_scenario("""
const feature = createVmCoverageFeature();
feature.coverageReportGroupBy = "none";
feature.vmReportBaseRows = [
  { id: 1, odcrCoverage: { decision: "required", priority: "high" }, reservationStatus: "Allocated, within capacity", odcrSupportabilityStatus: "Supported", odcrRequirements: [] },
  { id: 2, odcrCoverage: { decision: "required", priority: "low" }, reservationStatus: "Not associated", odcrSupportabilityStatus: "Unsupported", odcrRequirements: [] },
  { id: 3, odcrCoverage: null, reservationStatus: "Not associated", odcrSupportabilityStatus: "Unknown", odcrRequirements: [] },
  { id: 4, odcrCoverage: { decision: "not_required" }, reservationStatus: "Allocated, within capacity", odcrSupportabilityStatus: "Supported", odcrRequirements: [] },
];
feature.rebuildVmView = function() { this.syncCoverageReportModel(); };
feature.syncCoverageReportModel();

feature.setCoverageFacet("categories", "coverageCategory", "required:high");
feature.setCoverageFacet("categories", "coverageCategory", "not_required", { ctrlKey: true });
const additive = feature.coverageReportFocusRows.map((row) => row.id);
feature.setCoverageFacet("categories", "coverageCategory", "required:low");
const replaced = feature.coverageReportFocusRows.map((row) => row.id);
feature.setCoverageFacet("categories", "coverageCategory", "required:low");
const cleared = feature.coverageReportFocusRows.map((row) => row.id);

feature.setCoverageFacet("categories", "coverageCategory", "unmarked");
feature.moveCoverageUnmarked("not_required");
const moved = {
  owner: feature.coverageUnmarkedOwner,
  selection: feature.coverageFacetSelections.coverageCategory,
  required: feature.coverageReportView.coverage.populationCount,
  notRequired: feature.coverageReportView.unassociation.populationCount,
};
feature.clearCoverageReportSelections();
const ownerAfterReportReset = feature.coverageUnmarkedOwner;
feature.vmFilters = [];
feature.resetLocalVmFilters();
const ownerAfterLocalReset = feature.coverageUnmarkedOwner;
const freshOwner = createVmCoverageFeature().coverageUnmarkedOwner;

console.log(JSON.stringify({
  additive, replaced, cleared, moved, ownerAfterReportReset, ownerAfterLocalReset, freshOwner,
}));
""", scripts=[
            "js/shared/http.js",
            "js/shared/report-model.js",
            "js/features/coverage-report-model.js",
            "js/features/vm-coverage.js",
        ])

        self.assertEqual([1, 4], result["additive"])
        self.assertEqual([2], result["replaced"])
        self.assertEqual([1, 2, 3, 4], result["cleared"])
        self.assertEqual(
            {
                "owner": "not_required",
                "selection": ["unmarked"],
                "required": 0,
                "notRequired": 1,
            },
            result["moved"],
        )
        self.assertEqual("not_required", result["ownerAfterReportReset"])
        self.assertEqual("not_required", result["ownerAfterLocalReset"])
        self.assertEqual("required", result["freshOwner"])


if __name__ == "__main__":
    unittest.main()