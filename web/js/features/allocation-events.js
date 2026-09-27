function createAllocationEventsFeature() {
  return {
    allocationVms: [],
    allocationVmMetadata: null,
    allocationVmsLoading: false,
    allocationVmsLoaded: false,
    allocationVmLoadDurationMs: null,
    allocationVmLoadedAt: null,
    allocationVmSearch: "",
    allocationVmSort: { key: "name", direction: "asc" },
    allocationVmRows: [],
    paginatedAllocationVms: [],
    allocationVmPage: 1,
    allocationVmPageSize: 20,
    allocationVmPageCount: 1,
    allocationVmPeriod: "30d",
    allocationVmConfirmedFailuresOnly: true,
    allocationVmFailuresOnly: false,
    allocationTimelineVm: null,
    allocationTimelineData: null,
    allocationTimelineLoading: false,
    allocationTimelineError: null,
    allocationVmFilters: [
      { key: "subscriptionId", label: "Subscription", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "resourceGroup", label: "Resource group", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "location", label: "Location", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "deploymentType", label: "Deployment", clientOnly: true, searchable: false, showSelectAll: true, options: [], selected: [] },
      { key: "vmSize", label: "VM size", clientOnly: true, searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "powerState", label: "Power state", clientOnly: true, searchable: false, showSelectAll: true, options: [], selected: [] },
    ],
    _allocationVmRequests: createLatestRequest(),
    _allocationTimelineRequests: createLatestRequest(),
    _allocationTimelineReturnFocus: null,

    async loadAllocationEventVms(resetLocalSubscription = false) {
      const request = this._allocationVmRequests.begin();
      const startedAt = performance.now();
      this.allocationVmsLoading = true;
      this.allocationVmsLoaded = false;
      this.allocationVmLoadDurationMs = null;
      this.allocationVms = [];
      try {
        const data = await requestJson("/api/allocation-events/vms", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.globalScopeRequest()),
          signal: request.signal,
        });
        if (!this._allocationVmRequests.isCurrent(request.id)) return;
        this.allocationVmMetadata = data.metadata || null;
        this.allocationVms = data.value || [];
        this.syncAllocationVmBusinessContextFilters();
        this.seedAllocationVmFilterOptions(resetLocalSubscription);
        this.rebuildAllocationVmView();
        this.allocationVmsLoaded = true;
        this.allocationVmLoadDurationMs = performance.now() - startedAt;
        this.allocationVmLoadedAt = Date.now();
      } catch (error) {
        if (error && error.name === "AbortError") return;
        this.notify(`Failed to load Allocation Events VMs: ${error.message || error}`);
      } finally {
        if (this._allocationVmRequests.isCurrent(request.id)) this.allocationVmsLoading = false;
        this._allocationVmRequests.finish(request.id);
      }
    },

    get allocationVmBusinessContextFields() {
      return businessContextFields(this.allocationVmMetadata);
    },

    syncAllocationVmBusinessContextFilters() {
      this.allocationVmFilters = syncBusinessContextFilters(
        this.allocationVmFilters,
        this.allocationVmMetadata,
      );
    },

    allocationVmBusinessContextFilterKey(fieldKey) {
      return businessContextFilterKey(fieldKey);
    },

    allocationVmBusinessContextValue(vm, fieldKey) {
      return businessContextValue(vm, fieldKey);
    },

    allocationVmBusinessContextDisplay(vm, fieldKey) {
      return businessContextDisplay(vm, fieldKey);
    },

    allocationVmDeployment(vm) {
      return resourceDeploymentFilterValue(vm, this.zoneDisplayMode);
    },

    allocationVmDeploymentDisplay(vm) {
      return resourceDeploymentDisplay(vm, this.zoneDisplayMode);
    },

    async openAllocationTimeline(vm, trigger = null) {
      if (trigger) this._allocationTimelineReturnFocus = trigger;
      this.allocationTimelineVm = vm;
      this.allocationTimelineData = null;
      this.allocationTimelineError = null;
      this.allocationTimelineLoading = true;
      if (typeof this.$nextTick === "function") {
        this.$nextTick(() => this.$refs?.allocationTimelineClose?.focus());
      }
      const request = this._allocationTimelineRequests.begin();
      try {
        const data = await requestJson("/api/allocation-events/timeline", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ resourceId: vm.id }),
          signal: request.signal,
        });
        if (!this._allocationTimelineRequests.isCurrent(request.id) || !this.allocationTimelineVm) return;
        this.allocationTimelineData = data;
      } catch (error) {
        if (error && error.name === "AbortError") return;
        if (this._allocationTimelineRequests.isCurrent(request.id)) {
          this.allocationTimelineError = error.message || String(error);
        }
      } finally {
        if (this._allocationTimelineRequests.isCurrent(request.id)) {
          this.allocationTimelineLoading = false;
        }
        this._allocationTimelineRequests.finish(request.id);
      }
    },

    retryAllocationTimeline() {
      if (this.allocationTimelineVm) this.openAllocationTimeline(this.allocationTimelineVm);
    },

    closeAllocationTimeline() {
      const returnFocus = this._allocationTimelineReturnFocus;
      this._allocationTimelineRequests.cancel();
      this.allocationTimelineVm = null;
      this.allocationTimelineData = null;
      this.allocationTimelineError = null;
      this.allocationTimelineLoading = false;
      this._allocationTimelineReturnFocus = null;
      if (returnFocus && typeof returnFocus.focus === "function") {
        setTimeout(() => returnFocus.focus(), 0);
      }
    },

    get allocationTimelineResource() {
      return {
        ...(this.allocationTimelineVm || {}),
        ...(this.allocationTimelineData?.vm || {}),
      };
    },

    allocationTimelineCoverageText() {
      const coverage = this.allocationTimelineData?.coverage;
      if (!coverage?.start || !coverage?.end) return "Coverage is unavailable.";
      return `${this.formatDateTimeWithTimeZone(coverage.start)} to ${this.formatDateTimeWithTimeZone(coverage.end)}`;
    },

    allocationTimelineCoverageState() {
      const coverage = this.allocationTimelineData?.coverage;
      if (!coverage) return "unavailable";
      if (coverage.stale) return "stale";
      return coverage.complete ? "complete" : "incomplete";
    },

    allocationTimelineDateKey(timestamp) {
      const value = new Date(timestamp);
      if (Number.isNaN(value.getTime())) return "unknown";
      return [
        value.getFullYear(),
        String(value.getMonth() + 1).padStart(2, "0"),
        String(value.getDate()).padStart(2, "0"),
      ].join("-");
    },

    allocationTimelineDateLabel(timestamp) {
      const value = new Date(timestamp);
      if (Number.isNaN(value.getTime())) return "Unknown date";
      const day = new Date(value.getFullYear(), value.getMonth(), value.getDate());
      const now = new Date();
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const difference = Math.round((today.getTime() - day.getTime()) / 86400000);
      const date = value.toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
      if (difference === 0) return `Today \u00b7 ${date}`;
      if (difference === 1) return `Yesterday \u00b7 ${date}`;
      return date;
    },

    get allocationTimelineGroups() {
      const events = this.allocationTimelineData?.events || [];
      const decorated = events.map((event, index) => {
        const next = events[index + 1];
        const currentTime = new Date(event.timestamp).getTime();
        const nextTime = next ? new Date(next.timestamp).getTime() : Number.NaN;
        const gapSeconds = Number.isNaN(currentTime) || Number.isNaN(nextTime)
          ? null
          : Math.max(0, Math.floor((currentTime - nextTime) / 1000));
        return {
          ...event,
          gapText: gapSeconds === null ? "" : `${this.humanDuration(gapSeconds)} between events`,
          isLast: index === events.length - 1,
        };
      });
      const groups = [];
      for (const event of decorated) {
        const key = this.allocationTimelineDateKey(event.timestamp);
        let group = groups[groups.length - 1];
        if (!group || group.key !== key) {
          group = { key, label: this.allocationTimelineDateLabel(event.timestamp), events: [] };
          groups.push(group);
        }
        group.events.push(event);
      }
      return groups;
    },

    allocationTimelineEventTitle(event) {
      const action = String(event.action || "").toLocaleLowerCase();
      if (event.recipe === "current-state") return "Current configuration";
      if (event.recipe === "vm-configuration") {
        const provisioningState = String(event.details?.provisioningState || "").toLocaleLowerCase();
        if (action === "delete") return "VM deleted";
        if (this.allocationTimelineIsCreateEvent(event)) return "VM created";
        if (provisioningState === "updating") return "VM configuration updated";
        return "VM configuration write";
      }
      if (!action) return "VM lifecycle operation";
      return action.charAt(0).toLocaleUpperCase() + action.slice(1);
    },

    allocationTimelineOutcomeLabel(outcome) {
      const value = String(outcome || "unknown").toLocaleLowerCase();
      return value.charAt(0).toLocaleUpperCase() + value.slice(1);
    },

    allocationTimelineOutcomeClass(outcome) {
      const value = String(outcome || "").toLocaleLowerCase();
      if (value === "succeeded") return "ok";
      if (value === "failed") return "err";
      if (value === "pending") return "running";
      if (value === "cancelled" || value === "canceled") return "warn";
      return "muted";
    },

    allocationTimelineErrorDetailTitle(detail, index) {
      return detail?.code || `Detail ${index + 1}`;
    },

    allocationTimelineErrorJson(event) {
      return JSON.stringify(event.error?.details || [], null, 2);
    },

    allocationTimelineIsCreateEvent(event) {
      return event?.recipe === "vm-configuration"
        && String(event.details?.provisioningState || "").toLocaleLowerCase() === "creating";
    },

    allocationTimelineDetailEntries(event) {
      const labels = {
        vmSize: "VM size",
        location: "Location",
        zones: "Zones",
        provisioningState: "Provisioning state",
        capacityReservationGroupId: "Capacity reservation group",
        powerState: "Power state",
        errorCode: "Error code",
        errorMessage: "Error message",
      };
      return Object.entries(event.details || {})
        .filter(([key, value]) => ![
          "errorCode", "errorMessage", "zones", "physicalZones", "locationDisplayName",
        ].includes(key)
          && (key !== "location" || event.recipe === "current-state" || this.allocationTimelineIsCreateEvent(event))
          && value !== null && value !== undefined && value !== "")
        .map(([key, value]) => ({
          key,
          label: labels[key] || key,
          value: key === "location"
            ? String(event.details.locationDisplayName || value)
            : Array.isArray(value) ? value.join(", ") : String(value),
        }));
    },

    allocationTimelineZoneDisplay(event) {
      const details = event.details || {};
      if (!Object.prototype.hasOwnProperty.call(details, "zones")) {
        return this.allocationTimelineIsCreateEvent(event) ? "Regional" : "Not provided";
      }
      const logicalZones = Array.isArray(details.zones) ? details.zones : [];
      const physicalZones = Array.isArray(details.physicalZones) ? details.physicalZones : [];
      if (logicalZones.length === 0) return "Regional";
      return logicalZones.map((logicalZone, index) => resourceDeploymentDisplay({
        logicalZone,
        physicalZone: physicalZones[index] || null,
      }, this.zoneDisplayMode)).join(", ");
    },

    setAllocationVmPeriod(period) {
      if (!["1d", "7d", "30d"].includes(period) || this.allocationVmPeriod === period) return;
      this.allocationVmPeriod = period;
      this.rebuildAllocationVmView();
    },

    toggleAllocationVmConfirmedFailuresOnly() {
      this.allocationVmConfirmedFailuresOnly = !this.allocationVmConfirmedFailuresOnly;
      this.rebuildAllocationVmView();
    },

    toggleAllocationVmFailuresOnly() {
      this.allocationVmFailuresOnly = !this.allocationVmFailuresOnly;
      this.rebuildAllocationVmView();
    },

    allocationVmPeriodData(vm) {
      return vm.allocation?.periods?.[this.allocationVmPeriod] || null;
    },

    allocationVmSuccessMetric(vm) {
      const period = this.allocationVmPeriodData(vm);
      if (!period || period.complete !== true) {
        return { state: "unavailable", succeeded: 0, failed: 0, attempts: 0, rate: null };
      }
      const succeeded = Number(period.succeeded || 0);
      const allFailures = Number(period.failed || 0);
      const confirmedFailures = Number(period.confirmedAllocationFailures || 0);
      const failed = this.allocationVmConfirmedFailuresOnly ? confirmedFailures : allFailures;
      const attempts = succeeded + failed;
      if (attempts === 0) {
        return {
          state: this.allocationVmConfirmedFailuresOnly && allFailures > 0 ? "noMatching" : "empty",
          succeeded,
          failed,
          attempts,
          rate: null,
        };
      }
      return {
        state: "measured",
        succeeded,
        failed,
        attempts,
        rate: (succeeded / attempts) * 100,
      };
    },

    allocationVmSuccessCounts(vm) {
      const metric = this.allocationVmSuccessMetric(vm);
      return metric.state === "measured" ? `${metric.succeeded} ok \u00b7 ${metric.failed} failed \u00b7` : "";
    },

    allocationVmSuccessRate(vm) {
      const metric = this.allocationVmSuccessMetric(vm);
      if (metric.rate === null) return "";
      const decimals = Number.isInteger(metric.rate) ? 0 : 1;
      return `${metric.rate.toFixed(decimals)}%`;
    },

    allocationVmSuccessEmptyText(vm) {
      const state = this.allocationVmSuccessMetric(vm).state;
      if (state === "unavailable") return "Data unavailable";
      if (state === "noMatching") return "No matching events";
      if (state === "empty") return "No allocation events";
      return "";
    },

    allocationVmSuccessClass(vm) {
      const metric = this.allocationVmSuccessMetric(vm);
      if (metric.state !== "measured") return "muted";
      if (metric.rate >= 95) return "ok";
      if (metric.rate >= 80) return "warn";
      return "err";
    },

    allocationVmPeriodTooltip() {
      const activity = this.allocationVmMetadata?.activityLog;
      if (!activity?.coverageEnd) return "Activity Log coverage is not available yet.";
      const days = Number.parseInt(this.allocationVmPeriod, 10);
      const end = new Date(activity.coverageEnd);
      const start = new Date(end.getTime() - (days * 24 * 60 * 60 * 1000));
      const lines = [
        `Coverage period: ${this.formatDateTimeWithTimeZone(start)} to ${this.formatDateTimeWithTimeZone(end)}.`,
      ];
      lines.push("Coverage ends at the latest complete UTC day.");
      lines.push("Only completed Start, Restart, Reapply, Redeploy, and VM create operations are included.");
      return lines.join(" ");
    },

    allocationVmSuccessTooltip(vm) {
      const period = this.allocationVmPeriodData(vm);
      const metric = this.allocationVmSuccessMetric(vm);
      if (!period || metric.state === "unavailable") {
        return "The selected period is not completely covered for this subscription.";
      }
      const scope = this.allocationVmConfirmedFailuresOnly
        ? "Only AllocationFailed and ZonalAllocationFailed errors count as failures."
        : "Every failed operation counts as a failure.";
      const coverage = period.start && period.end
        ? `Coverage period: ${this.formatDateTimeWithTimeZone(period.start)} to ${this.formatDateTimeWithTimeZone(period.end)}. `
        : "";
      if (metric.state === "noMatching") {
        return `${coverage}No successful or confirmed allocation-failure events. ${scope}`;
      }
      if (metric.state === "empty") return `${coverage}No allocation events. ${scope}`;
      const sourceBreakdown = [
        `${period.succeededLifecycle || 0} lifecycle successes`,
        `${period.succeededCreates || 0} successful creates`,
        `${period.failedLifecycle || 0} lifecycle failures`,
        `${period.failedCreates || 0} failed creates`,
      ].join(", ");
      return `${coverage}${metric.succeeded} succeeded, ${metric.failed} failed, ${this.allocationVmSuccessRate(vm)} success. ${sourceBreakdown}. ${scope}`;
    },

    allocationVmLastAllocationText(vm) {
      const event = vm.allocation?.lastAllocation;
      if (!event?.timestamp) return "—";
      const outcome = String(event.outcome || "unknown");
      const label = outcome.charAt(0).toLocaleUpperCase() + outcome.slice(1).toLocaleLowerCase();
      return `${label} \u00b7 ${this.relativeTimeText(event.timestamp, { short: true })}`;
    },

    allocationVmLastAllocationTitle(vm) {
      const event = vm.allocation?.lastAllocation;
      if (!event?.timestamp) return "";
      return `${event.action || "Allocation action"} · ${this.formatDateTimeWithTimeZone(event.timestamp)}`;
    },

    allocationVmLastFailureText(vm) {
      const event = vm.allocation?.lastFailure;
      if (!event?.timestamp) return "—";
      return `${event.errorCode || "Failed"} \u00b7 ${this.relativeTimeText(event.timestamp, { short: true })}`;
    },

    allocationVmLastFailureTitle(vm) {
      const event = vm.allocation?.lastFailure;
      if (!event?.timestamp) return "";
      return [
        event.action || "Allocation action",
        this.formatDateTimeWithTimeZone(event.timestamp),
        event.errorCode,
        event.errorMessage,
      ].filter(Boolean).join(" \u00b7 ");
    },

    sortAllocationVmsBySuccess(rows) {
      const direction = this.allocationVmSort.direction === "asc" ? 1 : -1;
      const rank = { measured: 0, noMatching: 1, empty: 2, unavailable: 3 };
      return rows.sort((left, right) => {
        const leftMetric = this.allocationVmSuccessMetric(left);
        const rightMetric = this.allocationVmSuccessMetric(right);
        const stateDifference = rank[leftMetric.state] - rank[rightMetric.state];
        if (stateDifference !== 0) return stateDifference;
        if (leftMetric.state === "measured" && leftMetric.rate !== rightMetric.rate) {
          return (leftMetric.rate - rightMetric.rate) * direction;
        }
        if (leftMetric.attempts !== rightMetric.attempts) {
          return rightMetric.attempts - leftMetric.attempts;
        }
        return String(left.name || "").localeCompare(String(right.name || ""), undefined, {
          numeric: true,
          sensitivity: "base",
        });
      });
    },

    seedAllocationVmFilterOptions(resetLocalSubscription = false) {
      seedResourceTableFilterOptions(
        this.allocationVmFilters,
        this.allocationVmMetadata,
        this.allocationVms,
        resetLocalSubscription,
      );
      this.seedAllocationVmDeploymentFilterOptions();
      setTableFilterOptions(
        this.allocationVmFilters,
        "vmSize",
        distinctTableOptions(this.allocationVms, "vmSize"),
      );
      setTableFilterOptions(
        this.allocationVmFilters,
        "powerState",
        distinctTableOptions(this.allocationVms, "powerState"),
      );
    },

    seedAllocationVmDeploymentFilterOptions() {
      const filter = this.allocationVmFilters.find((item) => item.key === "deploymentType");
      filter.options = [...new Set(
        this.allocationVms.map((vm) => this.allocationVmDeployment(vm)).filter(Boolean)
      )].map((value) => ({
        value,
        label: value === "Regional" ? value : `${this.zoneDisplayPrefix} ${value}`,
      })).sort((left, right) => {
        if (left.value === "Regional") return -1;
        if (right.value === "Regional") return 1;
        return Number(left.value.replace("Zone ", "")) - Number(right.value.replace("Zone ", ""));
      });
      filter.selected = filter.options.map((option) => option.value);
    },

    get hasActiveLocalAllocationVmFilters() {
      return this.allocationVmSearch.trim().length > 0
        || this.allocationVmFilters.some((filter) =>
          filter.options.length > 0 && filter.selected.length !== filter.options.length
        );
    },

    rebuildAllocationVmView(resetPage = true) {
      const filterFields = {
        subscriptionId: "subscriptionId",
        resourceGroup: "resourceGroup",
        location: "location",
        vmSize: "vmSize",
        powerState: "powerState",
      };
      let rows = filterTableRows(this.allocationVms, this.allocationVmFilters, (vm, filter) => {
        if (filter.businessContextKey) return matchesBusinessContextFilter(filter, vm);
        const value = filter.key === "deploymentType"
          ? this.allocationVmDeployment(vm)
          : vm[filterFields[filter.key]];
        return filter.selected.includes(value);
      });
      rows = searchTableRows(rows, this.allocationVmSearch, (vm) => [
        vm.name,
        vm.subscriptionName,
        vm.subscriptionId,
        ...this.allocationVmBusinessContextFields.map((field) =>
          this.allocationVmBusinessContextValue(vm, field.key)
        ),
        vm.resourceGroup,
        vm.location,
        vm.locationDisplayName,
        this.allocationVmDeploymentDisplay(vm),
        vm.vmSize,
        vm.powerState,
        vm.allocation?.lastAllocation?.action,
        vm.allocation?.lastAllocation?.outcome,
        vm.allocation?.lastFailure?.errorCode,
        vm.allocation?.lastFailure?.errorMessage,
      ]);
      if (this.allocationVmFailuresOnly) {
        rows = rows.filter((vm) => this.allocationVmSuccessMetric(vm).failed > 0);
      }
      rows = this.allocationVmSort.key === "allocationSuccess"
        ? this.sortAllocationVmsBySuccess([...rows])
        : sortTableRows([...rows], this.allocationVmSort, (vm, key) => {
          if (key === "deploymentType") return this.allocationVmDeployment(vm);
          if (key.startsWith("businessContext:")) {
            return this.allocationVmBusinessContextValue(
              vm,
              key.slice("businessContext:".length),
            );
          }
          return vm[key];
        });
      this.allocationVmRows = rows;
      this.allocationVmPageCount = Math.max(1, Math.ceil(rows.length / this.allocationVmPageSize));
      this.allocationVmPage = resetPage
        ? 1
        : Math.min(this.allocationVmPage, this.allocationVmPageCount);
      this.updateAllocationVmPage();
    },

    updateAllocationVmPage() {
      this.paginatedAllocationVms = paginateTableRows(
        this.allocationVmRows,
        this.allocationVmPage,
        this.allocationVmPageSize,
      );
    },

    onAllocationVmSearchInput(value) {
      this.allocationVmSearch = value;
      this.rebuildAllocationVmView();
    },

    onAllocationVmFilterChange() {
      this.rebuildAllocationVmView();
    },

    resetLocalAllocationVmFilters() {
      this.allocationVmSearch = "";
      for (const filter of this.allocationVmFilters) {
        filter.selected = filter.options.map((option) => option.value);
      }
      this.rebuildAllocationVmView();
    },

    cycleAllocationVmSort(key) {
      this.allocationVmSort = nextTableSort(this.allocationVmSort, key);
      this.rebuildAllocationVmView();
    },

    allocationVmSortIndicator(key) {
      if (this.allocationVmSort.key !== key) return "";
      return this.allocationVmSort.direction === "asc" ? "↑" : "↓";
    },

    allocationVmPowerClass(power) {
      const value = String(power || "").toLocaleLowerCase();
      if (value.includes("running")) return "ok";
      if (value.includes("dealloc") || value.includes("stopp")) return "muted";
      return "warn";
    },

    setAllocationVmPage(page) {
      this.allocationVmPage = Math.max(1, Math.min(page, this.allocationVmPageCount));
      this.updateAllocationVmPage();
    },

    setAllocationVmPageSize(value) {
      const pageSize = Number(value);
      if (![20, 50, 100, 200].includes(pageSize)) return;
      this.allocationVmPageSize = pageSize;
      this.allocationVmPageCount = Math.max(1, Math.ceil(this.allocationVmRows.length / pageSize));
      this.allocationVmPage = 1;
      this.updateAllocationVmPage();
    },

    allocationVmPageStart() {
      return tablePageStart(
        this.allocationVmRows.length,
        this.allocationVmPage,
        this.allocationVmPageSize,
      );
    },

    allocationVmPageEnd() {
      return tablePageEnd(
        this.allocationVmRows.length,
        this.allocationVmPage,
        this.allocationVmPageSize,
      );
    },

    allocationVmLoadedSummaryText() {
      if (this.allocationVmLoadDurationMs === null || this.allocationVmLoadedAt === null) return "";
      const seconds = this.allocationVmLoadDurationMs / 1000;
      const duration = seconds < 1
        ? "loaded in less than a second"
        : `loaded in ${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} seconds`;
      return `${this.allocationVms.length} VMs ${duration}, ${this.relativeTimeText(this.allocationVmLoadedAt)}`;
    },

    allocationVmCsvMetricValue(vm, key) {
      const metric = this.allocationVmSuccessMetric(vm);
      return metric.state === "unavailable" ? "" : metric[key];
    },

    allocationVmCsvPeriodValue(vm, key) {
      const period = this.allocationVmPeriodData(vm);
      return period?.complete === true ? period[key] : "";
    },

    exportAllocationVmsCsv() {
      const columns = [
        ["Name", (vm) => vm.name],
        ["Subscription", (vm) => vm.subscriptionName || vm.subscriptionId],
        ...this.allocationVmBusinessContextFields.map((field) => [
          field.name,
          (vm) => this.allocationVmBusinessContextValue(vm, field.key),
        ]),
        ["Subscription ID", (vm) => vm.subscriptionId],
        ["Resource group", (vm) => vm.resourceGroup],
        ["Region", (vm) => locationDisplay(vm)],
        ["Deployment", (vm) => this.allocationVmDeploymentDisplay(vm)],
        ["VM size", (vm) => vm.vmSize],
        ["Power state", (vm) => vm.powerState],
        ["Allocation period", () => this.allocationVmPeriod],
        ["Failure scope", () => this.allocationVmConfirmedFailuresOnly ? "Confirmed allocation failures" : "All failures"],
        ["Allocation successes", (vm) => this.allocationVmCsvMetricValue(vm, "succeeded")],
        ["Allocation failures", (vm) => this.allocationVmCsvMetricValue(vm, "failed")],
        ["Allocation success rate", (vm) => {
          const rate = this.allocationVmSuccessMetric(vm).rate;
          return rate === null ? "" : rate;
        }],
        ["All failures", (vm) => this.allocationVmCsvPeriodValue(vm, "failed")],
        ["Confirmed allocation failures", (vm) => this.allocationVmCsvPeriodValue(vm, "confirmedAllocationFailures")],
        ["Last allocation action", (vm) => vm.allocation?.lastAllocation?.action],
        ["Last allocation outcome", (vm) => vm.allocation?.lastAllocation?.outcome],
        ["Last allocation time", (vm) => vm.allocation?.lastAllocation?.timestamp],
        ["Last failure action", (vm) => vm.allocation?.lastFailure?.action],
        ["Last failure code", (vm) => vm.allocation?.lastFailure?.errorCode],
        ["Last failure message", (vm) => vm.allocation?.lastFailure?.errorMessage],
        ["Last failure time", (vm) => vm.allocation?.lastFailure?.timestamp],
      ];
      downloadCsv(
        `azure-capacity-allocation-events-vms-${new Date().toISOString().slice(0, 10)}.csv`,
        columns,
        this.allocationVmRows,
      );
    },
  };
}
