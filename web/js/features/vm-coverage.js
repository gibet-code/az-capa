function createVmCoverageFeature() {
  return {
    vms: [],
    vmMetadata: null,
    vmsLoading: false,
    vmsLoaded: false,
    vmLoadDurationMs: null,
    vmLoadedAt: null,
    vmFilter: "",
    vmSort: { key: null, direction: null },
    zoneDisplayMode: "logical",
    selectedVmIds: {},
    odcrDetailsVm: null,
    odcrGroupOpen: { passed: false, failed: true, unknown: false },
    coverageReportGroupBy: "none",
    coverageFacetSelections: {},
    coverageUnmarkedOwner: "required",
    coverageReportEditingContext: null,
    coverageReportSorts: {
      details: { key: "count", direction: "desc" },
      coverage: { key: "percent", direction: "desc" },
      unassociation: { key: "percent", direction: "desc" },
      marking: { key: "marked", direction: "desc" },
      prerequisites: { key: "count", direction: "desc" },
      issues: { key: "count", direction: "desc" },
    },
    vmReportBaseRows: [],
    filteredVms: [],
    paginatedVms: [],
    coverageReportView: null,
    coverageCategoryCounts: {},
    coverageReportFocusRows: [],
    coverageDecisionTooltip: null,
    vmPage: 1,
    vmPageSize: 20,
    vmPageCount: 1,
    vmFilters: [
      { key: "subscriptionId", label: "Subscription", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "resourceGroup", label: "Resource group", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "location", label: "Location", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "deploymentType", label: "Deployment", clientOnly: true, searchable: false, showSelectAll: true, options: [], selected: [] },
      { key: "vmSize", label: "VM size", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "vmssFlexible", label: "VMSS Flexible", searchable: false, showSelectAll: true, options: [], selected: [] },
      { key: "reservationStatus", label: "Reservation status", searchable: false, showSelectAll: true, options: [], selected: [] },
      { key: "odcrCoverageDecision", label: "ODCR coverage", clientOnly: true, searchable: false, showSelectAll: true,
        options: [{ value: "unmarked", label: "Unmarked" }, { value: "required", label: "Required" }, { value: "not_required", label: "Not required" }],
        selected: ["unmarked", "required", "not_required"] },
      { key: "odcrCoveragePriority", label: "ODCR priority", clientOnly: true, searchable: false, showSelectAll: true,
        options: [{ value: "low", label: "Low" }, { value: "medium", label: "Medium" }, { value: "high", label: "High" }],
        selected: ["low", "medium", "high"] },
    ],
    _vmRequests: createLatestRequest(),
    _coverageReportModel: null,

    async loadVms(resetLocalSubscription = false) {
      const request = this._vmRequests.begin();
      const startedAt = performance.now();
      this.vmsLoading = true;
      this.vmLoadDurationMs = null;
      this.selectedVmIds = {};
      this.vms = [];
      try {
        const data = await requestJson("/api/odcr/coverage", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.globalScopeRequest()),
          signal: request.signal,
        });
        if (!this._vmRequests.isCurrent(request.id)) return;
        this.vmMetadata = data.metadata || null;
        this.vms = data.value || [];
        this._coverageReportModel = null;
        this.syncVmBusinessContextFilters();
        if (!this.coverageReportGroupOptions.some((option) => option.key === this.coverageReportGroupBy)) {
          this.coverageReportGroupBy = "subscription";
        }
        this.seedVmFilterOptions(this.vms, resetLocalSubscription);
        this.rebuildVmView();
        this.vmsLoaded = true;
        this.vmLoadDurationMs = performance.now() - startedAt;
        this.vmLoadedAt = Date.now();
      } catch (err) {
        if (err && err.name === "AbortError") return;
        this.notify(`Failed to load VMs: ${err.message || err}`);
      } finally {
        if (this._vmRequests.isCurrent(request.id)) this.vmsLoading = false;
        this._vmRequests.finish(request.id);
      }
    },

    seedVmFilterOptions(rows, resetLocalSubscription = false) {
      seedResourceTableFilterOptions(this.vmFilters, this.vmMetadata, rows, resetLocalSubscription);
      this.seedDeploymentFilterOptions(rows);
      setTableFilterOptions(this.vmFilters, "vmSize", distinctTableOptions(rows, "vmSize"));
      setTableFilterOptions(this.vmFilters, "vmssFlexible", [...new Set(rows.map((row) => row.isVmssFlexible))]
        .filter((value) => typeof value === "boolean")
        .map((value) => ({ value, label: value ? "Yes" : "No" }))
        .sort((left, right) => Number(left.value) - Number(right.value)));
      const reservationStatusOrder = [
        "Allocated, within capacity",
        "Associated, capacity available",
        "Allocated, overallocated",
        "Associated, capacity fully used",
        "Associated, no capacity",
        "Configuration issue",
        "Not associated",
      ];
      setTableFilterOptions(this.vmFilters, "reservationStatus", distinctTableOptions(rows, "reservationStatus").sort(
        (left, right) => reservationStatusOrder.indexOf(left.value) - reservationStatusOrder.indexOf(right.value)
      ));
    },

    get vmBusinessContextFields() {
      return businessContextFields(this.vmMetadata);
    },

    syncVmBusinessContextFilters() {
      this.vmFilters = syncBusinessContextFilters(this.vmFilters, this.vmMetadata);
    },

    vmBusinessContextFilterKey(fieldKey) {
      return businessContextFilterKey(fieldKey);
    },

    vmBusinessContextValue(vm, fieldKey) {
      return businessContextValue(vm, fieldKey);
    },

    vmBusinessContextDisplay(vm, fieldKey) {
      return businessContextDisplay(vm, fieldKey);
    },

    seedDeploymentFilterOptions(rows) {
      const filter = this.vmFilters.find((item) => item.key === "deploymentType");
      filter.options = [...new Set(rows.map((vm) => this.deploymentFilterValue(vm)).filter(Boolean))]
        .map((value) => ({
          value,
          label: value === "Regional" ? value : `${this.zoneDisplayPrefix} ${value}`,
        })).sort((left, right) => {
        if (left.value === "Regional") return -1;
        if (right.value === "Regional") return 1;
        return Number(left.value.replace("Zone ", "")) - Number(right.value.replace("Zone ", ""));
      });
      filter.selected = filter.options.map((option) => option.value);
    },

    get zoneDisplayPrefix() {
      return this.zoneDisplayMode === "physical" ? "Phy" : "Log";
    },

    deploymentFilterValue(vm) {
      return resourceDeploymentFilterValue(vm, this.zoneDisplayMode);
    },

    deploymentDisplay(vm) {
      return resourceDeploymentDisplay(vm, this.zoneDisplayMode);
    },

    toggleZoneDisplay() {
      for (const filters of [this.vmFilters, this.odcrUsageFilters]) {
        const filter = filters.find((item) => item.key === "deploymentType");
        filter.selected = filter.options.map((option) => option.value);
      }
      this.zoneDisplayMode = this.zoneDisplayMode === "logical" ? "physical" : "logical";
      this.selectedVmIds = {};
      this.seedDeploymentFilterOptions(this.vms);
      this.rebuildVmView();
      this.seedOdcrUsageDeploymentFilterOptions(this.odcrUsageRows);
      this.rebuildOdcrUsageView();
    },

    get hasActiveVmFilters() {
      return this.vmFilters.some((filter) =>
        filter.options.length > 0 && filter.selected.length !== filter.options.length
      );
    },

    get hasActiveLocalVmFilters() {
      return this.hasActiveVmFilters
        || this.vmFilter.trim().length > 0;
    },

    rebuildVmView(resetPage = true) {
      const filterFields = {
        subscriptionId: "subscriptionId",
        resourceGroup: "resourceGroup",
        location: "location",
        deploymentType: "deploymentType",
        vmSize: "vmSize",
        vmssFlexible: "isVmssFlexible",
        reservationStatus: "reservationStatus",
      };
      const baseRows = filterTableRows(this.vms, this.vmFilters, (vm, filter) => {
        if (filter.key === "odcrCoverageDecision") {
          return filter.selected.includes((vm.odcrCoverage && vm.odcrCoverage.decision) || "unmarked");
        }
        if (filter.key === "odcrCoveragePriority") {
          return filter.selected.includes(vm.odcrCoverage && vm.odcrCoverage.priority);
        }
        if (filter.businessContextKey) {
          return matchesBusinessContextFilter(filter, vm);
        }
        const value = filter.key === "deploymentType"
          ? this.deploymentFilterValue(vm)
          : vm[filterFields[filter.key]];
        return filter.selected.includes(value);
      });
      this.vmReportBaseRows = searchTableRows(baseRows, this.vmFilter, (vm) => [
          vm.name, vm.subscriptionName, vm.subscriptionId,
          ...this.vmBusinessContextFields.map((field) => this.vmBusinessContextValue(vm, field.key)),
          vm.resourceGroup, vm.location, vm.locationDisplayName,
          this.deploymentDisplay(vm), vm.vmSize, vm.capacityReservationGroupName,
          vm.matchingCapacityReservationName, vm.reservationStatus, vm.powerState,
          this.odcrCoverageText(vm), vm.odcrCoverage && vm.odcrCoverage.note,
        ]);

      this.syncCoverageReportModel();

      const tableRows = [...this.coverageReportFocusRows];

      sortTableRows(tableRows, this.vmSort, (row, key) =>
        key === "odcrCoverage" ? this.odcrCoverageSortValue(row)
          : key === "deploymentType" ? this.deploymentFilterValue(row)
            : key.startsWith("businessContext:")
              ? this.vmBusinessContextValue(row, key.slice("businessContext:".length))
            : row[key]
      );
      this.filteredVms = tableRows;
      this.vmPageCount = Math.max(1, Math.ceil(tableRows.length / this.vmPageSize));
      this.vmPage = resetPage ? 1 : Math.min(this.vmPage, this.vmPageCount);
      this.updateVmPage();
      this.scheduleCoverageReportCharts();
    },

    syncCoverageReportModel() {
      if (!this._coverageReportModel) {
        this._coverageReportModel = createReportModel({
          grouping: this.coverageReportGroupBy,
          facetMatchers: createCoverageReportFacetMatchers({
            groupDescriptor: (vm) => this.coverageReportGroupDescriptor(vm),
            metadata: this.vmMetadata,
          }),
        });
      }
      this._coverageReportModel.setGrouping(this.coverageReportGroupBy);
      this._coverageReportModel.setReferencePopulation(this.vmReportBaseRows);
      this._coverageReportModel.clearSelections();
      const actions = [];
      for (const [facet, values] of Object.entries(this.coverageFacetSelections)) {
        if (!values.length) continue;
        actions.push({
          facet,
          operation: "replace",
          values,
        });
      }
      if (actions.length > 0) this._coverageReportModel.applySelection(actions);
      const snapshot = this._coverageReportModel.getSnapshot();
      this.coverageReportFocusRows = [...snapshot.focusRecords];
      const populationFor = (visual) => this._coverageReportModel.getPopulationExcluding(
        this.coverageReportEditingContext?.visual === visual
          ? this.coverageReportEditingContext.facets
          : []
      );
      const requiredPopulationFor = (visual) => coverageReportBlockPopulation(
        populationFor(visual), "required", this.coverageUnmarkedOwner
      );
      const notRequiredPopulationFor = (visual) => coverageReportBlockPopulation(
        populationFor(visual), "not_required", this.coverageUnmarkedOwner
      );
      const visualPopulations = {
        coverage: requiredPopulationFor("coverage"),
        unassociation: notRequiredPopulationFor("unassociation"),
        marking: populationFor("marking"),
        coverageDetails: requiredPopulationFor("details"),
        prerequisites: requiredPopulationFor("prerequisites"),
        prerequisiteIssues: requiredPopulationFor("issues"),
      };
      this.coverageReportView = buildCoverageReportViewModel({
        referenceRows: snapshot.referenceRecords,
        focusRows: snapshot.focusRecords,
        visualPopulations,
        metadata: this.vmMetadata,
        groupBy: this.coverageReportGroupBy,
        groupDescriptor: (vm) => this.coverageReportGroupDescriptor(vm),
      });
      const categoryPopulation = this._coverageReportModel.getPopulationExcluding("coverageCategory");
      this.coverageCategoryCounts = Object.fromEntries(
        this.coverageCategoryValues.map((value) => [
          value,
          categoryPopulation.filter((row) => coverageReportCategoryKey(row) === value).length,
        ])
      );
    },

    get coverageRequiredCategoryValues() {
      return coverageReportCategoryValues(this.coverageUnmarkedOwner).required;
    },

    get coverageNotRequiredCategoryValues() {
      return coverageReportCategoryValues(this.coverageUnmarkedOwner).notRequired;
    },

    get coverageCategoryValues() {
      return [...this.coverageRequiredCategoryValues, ...this.coverageNotRequiredCategoryValues];
    },

    coverageCategoryCount(value) {
      return this.coverageCategoryCounts[value] || 0;
    },

    moveCoverageUnmarked(owner) {
      if (!["required", "not_required"].includes(owner) || owner === this.coverageUnmarkedOwner) return;
      this.coverageUnmarkedOwner = owner;
      if (typeof this.persistCoverageUnmarkedOwner === "function") this.persistCoverageUnmarkedOwner(owner);
      this.selectedVmIds = {};
      this.rebuildVmView();
    },

    get hasCoverageReportSelections() {
      return Object.values(this.coverageFacetSelections).some((values) => values.length > 0);
    },

    get coverageActiveFiltersText() {
      const facetLabels = {
        coverageCategory: "Coverage Category",
        marking: "Coverage Decision Status",
        association: "ODCR Association",
        reservationStatus: "Reservation Status",
        prerequisiteStatus: "Prerequisite Status",
        prerequisiteIssue: "Prerequisite Issue",
      };
      const labels = Object.entries(this.coverageFacetSelections)
        .filter(([, values]) => values.length > 0)
        .map(([facet]) => facet === "dimension" ? this.coverageReportGroupLabel : facetLabels[facet])
        .filter(Boolean);
      if (labels.length === 0) return "";
      if (labels.length === 1) return `Active filters: ${labels[0]}`;
      return `Active filters: ${labels.slice(0, -1).join(", ")} and ${labels.at(-1)}`;
    },

    coverageFacetSelected(facet, value) {
      return (this.coverageFacetSelections[facet] || []).includes(value);
    },

    setCoverageFacet(visual, facet, value, event = null) {
      const current = this.coverageFacetSelections[facet] || [];
      const additive = Boolean(event && (event.ctrlKey || event.metaKey));
      const selected = current.includes(value);
      if (facet === "dimension" && selected && !additive) {
        this.coverageFacetSelections = { dimension: [value] };
        this.coverageReportEditingContext = { visual, facets: [facet] };
        this.selectedVmIds = {};
        this.rebuildVmView();
        return;
      }
      let next;
      if (additive) {
        next = selected
          ? current.filter((item) => item !== value)
          : [...current, value];
      } else {
        next = selected ? [] : [value];
      }
      this.coverageFacetSelections = { ...this.coverageFacetSelections, [facet]: next };
      if (!(facet === "dimension" && additive && selected)) {
        this.coverageReportEditingContext = { visual, facets: [facet] };
      }
      this.selectedVmIds = {};
      this.rebuildVmView();
    },

    setCoverageFacetValues(visual, facet, values) {
      const current = this.coverageFacetSelections[facet] || [];
      const sameValues = current.length === values.length
        && values.every((value) => current.includes(value));
      this.coverageFacetSelections = {
        ...this.coverageFacetSelections,
        [facet]: sameValues ? [] : [...values],
      };
      this.coverageReportEditingContext = { visual, facets: [facet] };
      this.selectedVmIds = {};
      this.rebuildVmView();
    },

    clearCoverageReportSelections() {
      if (!this.hasCoverageReportSelections) return;
      this.coverageFacetSelections = {};
      this.coverageReportEditingContext = null;
      this.selectedVmIds = {};
      this.rebuildVmView();
    },

    visibleCoveragePrerequisiteRows() {
      return this.coverageSortedRows("prerequisites");
    },

    cycleCoverageReportSort(table, key) {
      const current = this.coverageReportSorts[table];
      const direction = current.key === key && current.direction === "desc" ? "asc" : "desc";
      this.coverageReportSorts = { ...this.coverageReportSorts, [table]: { key, direction } };
    },

    coverageReportSortIndicator(table, key) {
      const sort = this.coverageReportSorts[table];
      if (!sort || sort.key !== key) return "";
      return sort.direction === "asc" ? "↑" : "↓";
    },

    coverageSortedRows(table, sourceRows = null) {
      const rows = [...(sourceRows || {
        coverage: this.coverageReportView?.coverage?.rows,
        unassociation: this.coverageReportView?.unassociation?.rows,
        marking: this.coverageReportView?.marking?.rows,
        details: this.coverageReportView?.coverageDetails?.rows,
        prerequisites: this.coverageReportView?.prerequisites?.rows,
        issues: this.coverageReportView?.prerequisiteIssues?.rows,
      }[table] || [])];
      const sort = this.coverageReportSorts[table];
      const value = (row) => {
        if (sort.key === "label") return row.label;
        if (table === "coverage" && sort.key === "review") return row.reviewReferenceCount ?? -1;
        if (table === "coverage") return row.assignedPercent ?? -1;
        if (table === "unassociation") return row.unassociatedPercent ?? -1;
        if (table === "marking") return row.markedPercent ?? -1;
        if (table === "prerequisites") return row.unsupportedOrUnknownCount;
        return row.referenceCount;
      };
      const direction = sort.direction === "asc" ? 1 : -1;
      return rows.sort((left, right) => {
        const leftValue = value(left);
        const rightValue = value(right);
        const compared = typeof leftValue === "string"
          ? leftValue.localeCompare(rightValue, undefined, { sensitivity: "base", numeric: true })
          : leftValue - rightValue;
        const coverageCountDifference = table === "coverage" && sort.key === "percent"
          ? left.assignedReferenceCount - right.assignedReferenceCount
          : table === "unassociation" && sort.key === "percent"
            ? left.unassociatedReferenceCount - right.unassociatedReferenceCount
          : 0;
        return compared * direction
          || coverageCountDifference * direction
          || left.label.localeCompare(right.label);
      });
    },

    updateVmPage() {
      this.paginatedVms = paginateTableRows(this.filteredVms, this.vmPage, this.vmPageSize);
    },

    setVmPage(page) {
      this.vmPage = Math.max(1, Math.min(page, this.vmPageCount));
      this.updateVmPage();
    },

    setVmPageSize(value) {
      const pageSize = Number(value);
      if (![20, 50, 100, 200].includes(pageSize)) return;
      this.vmPageSize = pageSize;
      this.vmPageCount = Math.max(1, Math.ceil(this.filteredVms.length / pageSize));
      this.vmPage = 1;
      this.updateVmPage();
    },

    vmPageStart() {
      return tablePageStart(this.filteredVms.length, this.vmPage, this.vmPageSize);
    },

    vmPageEnd() {
      return tablePageEnd(this.filteredVms.length, this.vmPage, this.vmPageSize);
    },

    vmLoadedSummaryText() {
      if (this.vmLoadDurationMs === null || this.vmLoadedAt === null) return "";
      return `${this.vms.length} VMs ${this.vmLoadDurationText().toLowerCase()}, ${this.vmLoadedAgoText()}`;
    },

    exportVmsCsv() {
      const columns = [
        ["Name", (vm) => vm.name],
        ["Subscription", (vm) => vm.subscriptionName || vm.subscriptionId],
        ...this.vmBusinessContextFields.map((field) => [
          field.name,
          (vm) => this.vmBusinessContextValue(vm, field.key),
        ]),
        ["Subscription ID", (vm) => vm.subscriptionId],
        ["Resource group", (vm) => vm.resourceGroup],
        ["Region", (vm) => locationDisplay(vm)],
        ["Deployment", (vm) => this.deploymentDisplay(vm)],
        ["VM size", (vm) => vm.vmSize],
        ["ODCR coverage", (vm) => this.odcrCoverageText(vm)],
        ["Reservation status", (vm) => vm.reservationStatus],
        ["Prerequisite status", (vm) => vm.odcrSupportabilityStatus],
        ["Capacity reservation group", (vm) => vm.capacityReservationGroupName],
        ["Capacity reservation", (vm) => vm.matchingCapacityReservationName],
        ["Allocation", (vm) => vm.reservationAllocatedCount],
        ["Associated VMs", (vm) => vm.reservationAssociatedCount],
        ["VMSS Flexible", (vm) => vm.isVmssFlexible === true ? "Yes" : vm.isVmssFlexible === false ? "No" : ""],
        ["Power state", (vm) => vm.powerState],
      ];
      downloadCsv(
        `azure-capacity-vms-${new Date().toISOString().slice(0, 10)}.csv`,
        columns,
        this.filteredVms,
      );
    },

    onVmSearchInput(value) {
      this.vmFilter = value;
      this.selectedVmIds = {};
      this.rebuildVmView();
    },

    get coverageReportGroupOptions() {
      return [
        { key: "none", label: "None" },
        { key: "subscription", label: "Subscription" },
        { key: "location", label: "Location" },
        ...this.vmBusinessContextFields.map((field) => ({
          key: this.vmBusinessContextFilterKey(field.key),
          label: field.name,
        })),
      ];
    },

    get coverageReportGroupLabel() {
      return this.coverageReportGroupOptions.find((option) => option.key === this.coverageReportGroupBy)?.label
        || "Dimension";
    },

    coverageReportTitle(baseTitle) {
      return this.coverageReportGroupBy === "none"
        ? baseTitle
        : `${baseTitle} by ${this.coverageReportGroupLabel}`;
    },

    setCoverageReportGroupBy(groupBy) {
      if (!this.coverageReportGroupOptions.some((option) => option.key === groupBy)) return;
      this.coverageReportGroupBy = groupBy;
      this.coverageFacetSelections = {};
      this.coverageReportEditingContext = null;
      this.selectedVmIds = {};
      this.rebuildVmView();
    },

    coverageReportGroupDescriptor(vm) {
      if (this.coverageReportGroupBy === "subscription") {
        const value = String(vm.subscriptionId || "").trim();
        return {
          key: value ? `subscription:${value.toLowerCase()}` : BUSINESS_CONTEXT_NOT_MAPPED,
          label: vm.subscriptionName || value || "Not mapped",
          missing: !value,
        };
      }
      if (this.coverageReportGroupBy === "location") {
        const value = String(vm.location || "").trim();
        return {
          key: value ? `location:${value.toLocaleLowerCase()}` : BUSINESS_CONTEXT_NOT_MAPPED,
          label: locationDisplay(vm) || "Not mapped",
          missing: !value,
        };
      }
      const fieldKey = this.coverageReportGroupBy.slice("businessContext:".length);
      const value = this.vmBusinessContextValue(vm, fieldKey);
      return {
        key: value === null ? BUSINESS_CONTEXT_NOT_MAPPED : `context:${value.toLocaleLowerCase()}`,
        label: value ?? "Not mapped",
        missing: value === null,
      };
    },

    onVmFilterChange() {
      this.selectedVmIds = {};
      this.rebuildVmView();
    },

    resetLocalVmFilters() {
      this.vmFilter = "";
      for (const filter of this.vmFilters) {
        filter.selected = filter.options.map((option) => option.value);
      }
      this.selectedVmIds = {};
      this.rebuildVmView();
    },

    vmSelectionKey(vm) {
      return String(vm && vm.id || "").toLowerCase();
    },

    isVmSelected(vm) {
      return this.selectedVmIds[this.vmSelectionKey(vm)] === true;
    },

    setVmSelected(vm, selected) {
      const key = this.vmSelectionKey(vm);
      if (!key) return;
      const next = { ...this.selectedVmIds };
      if (selected) next[key] = true;
      else delete next[key];
      this.selectedVmIds = next;
    },

    toggleVmSelection(vm) {
      this.setVmSelected(vm, !this.isVmSelected(vm));
    },

    get selectedVmCount() {
      return Object.keys(this.selectedVmIds).length;
    },

    get allVisibleVmsSelected() {
      return this.paginatedVms.length > 0 && this.paginatedVms.every((vm) => this.isVmSelected(vm));
    },

    get someVisibleVmsSelected() {
      return !this.allVisibleVmsSelected && this.paginatedVms.some((vm) => this.isVmSelected(vm));
    },

    setAllVisibleVmsSelected(selected) {
      const next = { ...this.selectedVmIds };
      for (const vm of this.paginatedVms) {
        const key = this.vmSelectionKey(vm);
        if (!key) continue;
        if (selected) next[key] = true;
        else delete next[key];
      }
      this.selectedVmIds = next;
    },

    cycleVmSort(key) {
      this.vmSort = nextTableSort(this.vmSort, key);
      this.rebuildVmView();
    },

    vmSortIndicator(key) {
      if (this.vmSort.key !== key) return "";
      return this.vmSort.direction === "asc" ? "↑" : "↓";
    },

    vmPowerClass(power) {
      const p = (power || "").toLowerCase();
      if (p.includes("running")) return "ok";
      if (p.includes("dealloc") || p.includes("stopp")) return "muted";
      return "warn";
    },

    reservationStatusClass(status) {
      if (status === "Allocated, within capacity") return "ok";
      if (["Associated, capacity available", "Allocated, overallocated"].includes(status)) return "warn";
      return "err";
    },

    prerequisiteStatusClass(status) {
      if (status === "Supported") return "ok";
      if (status === "Unsupported") return "err";
      if (status === "Unknown") return "warn";
      return "muted";
    },

    coverageDecisionAuditName(vm) {
      const updatedBy = vm && vm.odcrCoverage && vm.odcrCoverage.updatedBy;
      return updatedBy && (updatedBy.displayName || updatedBy.objectId) || "Unknown editor";
    },

    coverageDecisionHasAudit(vm) {
      const coverage = vm && vm.odcrCoverage;
      return Boolean(coverage && coverage.updatedAt);
    },

    showCoverageDecisionTooltip(vm, element) {
      const rect = element.getBoundingClientRect();
      const width = Math.min(390, window.innerWidth - 24);
      const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
      const showAbove = rect.bottom + 150 > window.innerHeight;
      this.coverageDecisionTooltip = {
        vm,
        left,
        top: showAbove ? rect.top - 8 : rect.bottom + 8,
        showAbove,
      };
    },

    hideCoverageDecisionTooltip() {
      this.coverageDecisionTooltip = null;
    },

    hasOdcrDetails(vm) {
      return this.odcrRequirementCodes(vm, "Failed").length > 0
        || this.odcrRequirementCodes(vm, "Unknown").length > 0;
    },

    openOdcrDetails(vm) {
      if (this.hasOdcrDetails(vm)) {
        const results = new Map((vm.odcrRequirements || []).map((result) => [result.key, result]));
        const requirementMetadata = this.vmMetadata && this.vmMetadata.odcrRequirements;
        const defaultStatus = (requirementMetadata && requirementMetadata.defaultStatus) || "Passed";
        const definitions = (requirementMetadata && requirementMetadata.definitions) || [];
        this.odcrDetailsVm = {
          ...vm,
          odcrRequirements: definitions.map((definition) => {
            const result = results.get(definition.key) || { key: definition.key, status: defaultStatus };
            return {
              ...definition,
              ...result,
              reason: result.reason || (result.status === "Failed" ? definition.failureReason : null),
            };
          }),
        };
        const hasFailures = this.odcrRequirementCodes(vm, "Failed").length > 0;
        this.odcrGroupOpen = { passed: false, failed: hasFailures, unknown: !hasFailures };
      }
    },

    odcrRequirementCodes(vm, status) {
      return (vm && vm.odcrRequirements || [])
        .filter((result) => result.status === status)
        .map((result) => result.key);
    },

    odcrRequirementDefinition(code) {
      const metadata = this.vmMetadata && this.vmMetadata.odcrRequirements;
      return ((metadata && metadata.definitions) || []).find((item) => item.key === code) || {};
    },

    closeOdcrDetails() {
      this.odcrDetailsVm = null;
    },

    odcrCoverageText(vm) {
      const coverage = vm && vm.odcrCoverage;
      if (!coverage || !coverage.decision) return "Unmarked";
      if (coverage.decision === "not_required") return "Not required";
      const priority = coverage.priority
        ? coverage.priority.charAt(0).toUpperCase() + coverage.priority.slice(1)
        : "";
      return `Required${priority ? ` · ${priority}` : ""}`;
    },

    odcrCoverageClass(vm) {
      const coverage = vm && vm.odcrCoverage;
      if (!coverage || !coverage.decision) return "muted";
      if (coverage.decision === "not_required") return "ok";
      return coverage.priority === "high" ? "err" : coverage.priority === "medium" ? "warn" : "running";
    },

    odcrCoverageSortValue(vm) {
      const coverage = vm && vm.odcrCoverage;
      if (!coverage || !coverage.decision) return "0-unmarked";
      if (coverage.decision === "not_required") return "1-not-required";
      const rank = { low: "1", medium: "2", high: "3" }[coverage.priority] || "0";
      return `2-required-${rank}`;
    },

    odcrRequirementClass(status) {
      switch (status) {
        case "Passed": case "Not applicable": case "To be implemented": return "ok";
        case "Failed": return "err";
        case "Unknown": return "warn";
        default: return "muted";
      }
    },

    odcrRequirementIcon(status) {
      if (status === "Failed") return "failed";
      if (status === "Unknown") return "unknown";
      return "passed";
    },

    showOdcrRequirementReason(requirement) {
      return requirement.status !== "Passed" && requirement.status !== "Not applicable";
    },

    odcrRequirementGroups() {
      if (!this.odcrDetailsVm) return [];
      const requirements = this.odcrDetailsVm.odcrRequirements || [];
      const passedStatuses = new Set(["Passed", "Not applicable", "To be implemented"]);
      const passed = requirements.filter((item) => passedStatuses.has(item.status));
      const failed = requirements.filter((item) => item.status === "Failed");
      const unknown = requirements.filter((item) => item.status === "Unknown");
      return [
        { key: "passed", label: `Passed (${passed.length}/${requirements.length})`, items: passed },
        { key: "failed", label: `Failed (${failed.length}/${requirements.length})`, items: failed },
        { key: "unknown", label: `Unknown (${unknown.length}/${requirements.length})`, items: unknown },
      ];
    },

    toggleOdcrGroup(key) {
      this.odcrGroupOpen[key] = !this.odcrGroupOpen[key];
    },

    reservationAllocationText(vm) {
      if (!vm.matchingCapacityReservationName) return "—";
      return `${vm.reservationAllocatedCount ?? 0} / ${vm.reservationCapacity ?? 0}`;
    },

    vmLoadDurationText() {
      if (this.vmLoadDurationMs === null) return "";
      const seconds = this.vmLoadDurationMs / 1000;
      if (seconds < 1) return "Loaded in less than a second";
      if (seconds < 10) return `Loaded in ${seconds.toFixed(1)} seconds`;
      if (seconds < 60) return `Loaded in ${Math.round(seconds)} seconds`;
      const minutes = Math.floor(seconds / 60);
      const remainingSeconds = Math.round(seconds % 60);
      return `Loaded in ${minutes} minute${minutes === 1 ? "" : "s"}${remainingSeconds ? ` ${remainingSeconds} seconds` : ""}`;
    },

    vmLoadedAgoText() {
      if (this.vmLoadedAt === null) return "";
      return this.relativeTimeText(this.vmLoadedAt);
    },
  };
}
