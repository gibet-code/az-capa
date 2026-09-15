function createOdcrUsageFeature() {
  return {
    odcrUsageRows: [],
    odcrUsageReservations: [],
    odcrUsageGroups: [],
    odcrUsageGroupRows: [],
    odcrUsageView: "reservation",
    expandedOdcrUsageGroupId: null,
    odcrUsageMetadata: null,
    odcrUsageLoading: false,
    odcrUsageLoaded: false,
    odcrUsageLoadDurationMs: null,
    odcrUsageLoadedAt: null,
    odcrUsageFilter: "",
    odcrUsageSort: { key: null, direction: null },
    odcrUsageMetric: "hours",
    odcrUsageIncludeDeleted: false,
    odcrUsageChartReady: false,
    odcrUsageChartGroupBy: "capacityReservationGroup",
    filteredOdcrUsageRows: [],
    paginatedOdcrUsageRows: [],
    odcrUsagePage: 1,
    odcrUsagePageSize: 20,
    odcrUsagePageCount: 1,
    odcrUsageFilters: [
      { key: "subscriptionId", label: "Subscription", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "resourceGroup", label: "Resource group", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "location", label: "Location", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "capacityReservationGroupName", label: "Capacity Reservation Group", searchable: true, showSelectAll: true, options: [], selected: [] },
      { key: "deploymentType", label: "Deployment", searchable: false, showSelectAll: true, options: [], selected: [] },
    ],
    _odcrUsageRequests: createLatestRequest(),
    _odcrUsageChartAdapter: null,
    _odcrUsageGroupColors: {},
    _odcrUsageChartGroups: [],

    // ── ODCR Usage (user-scoped) ─────────────────────────────
    async loadOdcrUsage(resetLocalSubscription = false) {
      const request = this._odcrUsageRequests.begin();
      const startedAt = performance.now();
      this.odcrUsageLoading = true;
      this.odcrUsageLoadDurationMs = null;
      this.odcrUsageRows = [];
      this.odcrUsageReservations = [];
      this.odcrUsageGroups = [];
      this.odcrUsageGroupRows = [];
      try {
        const data = await requestJson("/api/odcr_usage/list", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.globalScopeRequest()),
          signal: request.signal,
        });
        if (!this._odcrUsageRequests.isCurrent(request.id)) return;
        this.odcrUsageReservations = data.capacityReservations || data.value || [];
        this.odcrUsageGroups = data.capacityReservationGroups || [];
        this.odcrUsageMetadata = data.metadata || null;
        if (
          this.odcrUsageChartGroupBy.startsWith("businessContext:")
          && !this.odcrUsageBusinessContextFields.some(
            (field) => this.odcrUsageChartGroupBy === businessContextFilterKey(field.key)
          )
        ) {
          this.odcrUsageChartGroupBy = "capacityReservationGroup";
        }
        this.syncOdcrUsageBusinessContextFilters();
        const models = buildOdcrUsageModels(
          this.odcrUsageGroups,
          this.odcrUsageReservations,
          this.odcrUsageMetadata,
        );
        this.odcrUsageReservations = models.reservationRows;
        this.odcrUsageGroupRows = models.groupRows;
        this.odcrUsageRows = this.odcrUsageView === "group" ? this.odcrUsageGroupRows : this.odcrUsageReservations;
        this.seedOdcrUsageFilterOptions(this.odcrUsageRows, resetLocalSubscription);
        this.rebuildOdcrUsageView();
        this.odcrUsageLoaded = true;
        this.odcrUsageLoadDurationMs = performance.now() - startedAt;
        this.odcrUsageLoadedAt = Date.now();
      } catch (err) {
        if (err && err.name === "AbortError") return;
        this.notify(`Failed to load ODCR usage: ${err.message || err}`);
      } finally {
        if (this._odcrUsageRequests.isCurrent(request.id)) {
          this.odcrUsageLoading = false;
          this.scheduleOdcrUsageChartRender();
        }
        this._odcrUsageRequests.finish(request.id);
      }
    },

    seedOdcrUsageFilterOptions(rows, resetLocalSubscription = false) {
      seedResourceTableFilterOptions(
        this.odcrUsageFilters,
        this.odcrUsageMetadata,
        rows,
        resetLocalSubscription,
      );
      setTableFilterOptions(
        this.odcrUsageFilters,
        "capacityReservationGroupName",
        distinctTableOptions(rows, "capacityReservationGroupName"),
      );
      this.seedOdcrUsageDeploymentFilterOptions(rows);
    },

    get odcrUsageBusinessContextFields() {
      return businessContextFields(this.odcrUsageMetadata);
    },

    syncOdcrUsageBusinessContextFilters() {
      this.odcrUsageFilters = syncBusinessContextFilters(this.odcrUsageFilters, this.odcrUsageMetadata);
    },

    odcrUsageBusinessContextFilterKey(fieldKey) {
      return businessContextFilterKey(fieldKey);
    },

    odcrUsageBusinessContextValue(row, fieldKey) {
      return businessContextValue(row, fieldKey);
    },

    odcrUsageBusinessContextDisplay(row, fieldKey) {
      return businessContextDisplay(row, fieldKey);
    },

    seedOdcrUsageDeploymentFilterOptions(rows) {
      const options = [...new Set(rows.map((row) => this.odcrUsageDeploymentFilterValue(row)).filter(Boolean))]
        .map((value) => ({
          value,
          label: this.odcrUsageView === "group" || value === "Regional" || value === "Unresolved"
            ? value
            : `${this.zoneDisplayPrefix} ${value}`,
        }))
        .sort((left, right) => {
          if (left.value === "Regional") return -1;
          if (right.value === "Regional") return 1;
          if (left.value === "Unresolved") return 1;
          if (right.value === "Unresolved") return -1;
          return Number(left.value.replace("Zone ", "")) - Number(right.value.replace("Zone ", ""));
        });
      setTableFilterOptions(this.odcrUsageFilters, "deploymentType", options, true);
    },

    odcrUsageDeploymentFilterValue(row) {
      return this.odcrUsageView === "group"
        ? row.deploymentType || "Unknown"
        : resourceDeploymentFilterValue(row, this.zoneDisplayMode);
    },

    odcrUsageDeploymentDisplay(row) {
      return this.odcrUsageView === "group"
        ? row.deploymentType || "—"
        : row.resourceExists === false ? "—" : resourceDeploymentDisplay(row, this.zoneDisplayMode);
    },

    setOdcrUsageView(view) {
      if (!["reservation", "group"].includes(view) || view === this.odcrUsageView) return;
      this.odcrUsageView = view;
      this.expandedOdcrUsageGroupId = null;
      this.odcrUsageRows = view === "group" ? this.odcrUsageGroupRows : this.odcrUsageReservations;
      this.odcrUsageFilter = "";
      this.odcrUsageSort = { key: null, direction: null };
      this.odcrUsagePage = 1;
      if (view === "group" && this.odcrUsageChartGroupBy === "capacityReservation") {
        this.odcrUsageChartGroupBy = "capacityReservationGroup";
      }
      this.seedOdcrUsageFilterOptions(this.odcrUsageRows);
      this.rebuildOdcrUsageView();
    },

    toggleOdcrUsageGroup(group) {
      if (this.odcrUsageView !== "group" || !group?.id) return;
      this.expandedOdcrUsageGroupId = this.expandedOdcrUsageGroupId === group.id ? null : group.id;
    },

    odcrUsageGroupChildren(group) {
      const groupId = String(group?.id || "").toLowerCase();
      return this.odcrUsageReservations
        .filter((reservation) => String(reservation.capacityReservationGroupId || "").toLowerCase() === groupId)
        .sort((left, right) => String(left.name || "").localeCompare(String(right.name || ""), undefined, {
          numeric: true,
          sensitivity: "base",
        }));
    },

    odcrUsageRenderedRows() {
      const rows = [];
      for (const row of this.paginatedOdcrUsageRows) {
        rows.push({ kind: "parent", id: `parent:${row.id}`, row });
        if (this.odcrUsageView === "group" && this.expandedOdcrUsageGroupId === row.id) {
          const children = this.odcrUsageGroupChildren(row);
          for (const child of children) {
            rows.push({ kind: "child", id: `child:${child.id}`, row: child });
          }
          if (!children.length) rows.push({ kind: "empty", id: `empty:${row.id}`, row: {} });
        }
      }
      return rows;
    },

    rebuildOdcrUsageView(resetPage = true) {
      const visibleRows = this.odcrUsageRows.filter(
        (row) => this.odcrUsageIncludeDeleted || row.resourceExists !== false
      );
      const filteredRows = filterTableRows(visibleRows, this.odcrUsageFilters, (row, filter) => {
        if (filter.businessContextKey) {
          return matchesBusinessContextFilter(filter, row);
        }
        const value = filter.key === "deploymentType" ? this.odcrUsageDeploymentFilterValue(row) : row[filter.key];
        return filter.selected.includes(value);
      });
      const rows = searchTableRows(filteredRows, this.odcrUsageFilter, (row) => [
        row.name,
        row.subscriptionName,
        row.subscriptionId,
        ...this.odcrUsageBusinessContextFields.map((field) => this.odcrUsageBusinessContextValue(row, field.key)),
        row.resourceGroup,
        row.location,
        row.locationDisplayName,
        this.odcrUsageDeploymentDisplay(row),
        this.odcrUsageView === "reservation" ? row.vmSize : null,
        row.capacityReservationGroupName,
        this.odcrUsageValue(row, "lastDay"),
        this.odcrUsageValue(row, "last7Days"),
        this.odcrUsageValue(row, "last30Days"),
      ]);
      sortTableRows(rows, this.odcrUsageSort, (row, key) =>
        key === "deploymentType" ? this.odcrUsageDeploymentFilterValue(row)
          : key === "reservationDisplayName" ? this.odcrUsageReservationDisplayName(row)
          : key === "groupName" ? row.name
          : key === "subscriptionName" ? row.subscriptionName || row.subscriptionId
            : key.startsWith("businessContext:")
              ? this.odcrUsageBusinessContextValue(row, key.slice("businessContext:".length))
            : ["lastDay", "last7Days", "last30Days"].includes(key) ? this.odcrUsageValue(row, key)
            : row[key]
      );
      this.filteredOdcrUsageRows = rows;
      this.odcrUsagePageCount = Math.max(1, Math.ceil(rows.length / this.odcrUsagePageSize));
      this.odcrUsagePage = resetPage ? 1 : Math.min(this.odcrUsagePage, this.odcrUsagePageCount);
      this.updateOdcrUsagePage();
      this.scheduleOdcrUsageChartRender();
    },

    scheduleOdcrUsageChartRender() {
      if (typeof echarts === "undefined") return;
      this.$nextTick(() => this.renderOdcrUsageChart());
    },

    renderOdcrUsageChart() {
      const container = this.$refs?.odcrUsageChart;
      if (!container || this.odcrTab !== "usage") return;
      const usage = this.odcrUsageMetadata?.usage;
      const dates = usage?.dates || [];
      const groups = buildOdcrUsageChartData(
        this.filteredOdcrUsageRows,
        dates,
        this.odcrUsageMetric,
        this.odcrUsageChartGroupBy,
      );
      this._odcrUsageChartGroups = groups;
      const hasValues = groups.some((group) => group.values.some((value) => value !== null));
      this.odcrUsageChartReady = Boolean(usage?.isAvailable && dates.length && groups.length && hasValues);
      if (!this.odcrUsageChartReady) {
        if (this._odcrUsageChartAdapter) this._odcrUsageChartAdapter.clear();
        return;
      }

      if (!this._odcrUsageChartAdapter) {
        this._odcrUsageChartAdapter = createEchartsAdapter({
          onClick: (event) => {
          if (event.componentType === "series") this.applyOdcrUsageChartGroupFilter(event.seriesName);
          },
        });
      }
      for (const [index, { name }] of groups.entries()) {
        this._odcrUsageGroupColors[name] = ODCR_CHART_PALETTE[index % ODCR_CHART_PALETTE.length];
      }
      const formatValue = (value) => this.formatOdcrUsageChartValue(value);
      const compactChart = container.clientWidth < 700;
      const dayTotals = dates.map((_, dateIndex) => groups.reduce((total, group) => {
        const value = group.values[dateIndex];
        return value === null ? total : total + Number(value);
      }, 0));
      const option = {
        animationDuration: 350,
        animationDurationUpdate: 220,
        backgroundColor: "transparent",
        textStyle: { color: "#e6edf3", fontFamily: "Segoe UI, sans-serif" },
        legend: {
          type: "scroll",
          show: this.odcrUsageChartGroupBy !== "none",
          orient: "vertical",
          top: 8,
          right: 4,
          bottom: 24,
          width: compactChart ? 92 : 190,
          itemWidth: 12,
          itemHeight: 8,
          tooltip: { show: true },
          textStyle: {
            color: "#c4ced8",
            fontSize: 11,
            width: compactChart ? 68 : 164,
            overflow: "truncate",
          },
          pageTextStyle: { color: "#9aa7b4" },
          pageIconColor: "#4c9aff",
          pageIconInactiveColor: "#526170",
        },
        grid: {
          top: 30,
          right: this.odcrUsageChartGroupBy === "none" ? 18 : compactChart ? 110 : 220,
          bottom: 42,
          left: compactChart ? 48 : 70,
        },
        tooltip: {
          trigger: "item",
          backgroundColor: "#222c38",
          borderColor: "#526170",
          textStyle: { color: "#e6edf3", fontSize: 12 },
          confine: true,
          formatter: (item) => {
            const lines = [`<strong>${escapeHtml(item.name || "")}</strong>`];
            if (this.odcrUsageChartGroupBy !== "none") {
              lines.push(`${item.marker}${escapeHtml(item.seriesName)}: <strong>${escapeHtml(formatValue(item.value))}</strong>`);
            }
            lines.push(`<span class="chart-tooltip-total">Day total: <strong>${escapeHtml(formatValue(dayTotals[item.dataIndex] || 0))}</strong></span>`);
            return lines.join("<br>");
          },
        },
        xAxis: {
          type: "category",
          data: dates,
          axisLine: { lineStyle: { color: "#526170" } },
          axisTick: { show: false },
          axisLabel: {
            color: "#9aa7b4",
            fontSize: 11,
            formatter: (date) => new Date(`${date}T00:00:00Z`).toLocaleDateString(undefined, {
              month: "short", day: "numeric", timeZone: "UTC",
            }),
          },
        },
        yAxis: {
          type: "value",
          min: 0,
          name: this.odcrUsageMetric === "cost"
            ? `Unused cost${usage.currency ? ` (${usage.currency})` : ""}`
            : "Unused hours",
          nameTextStyle: { color: "#9aa7b4", padding: [0, 0, 8, 0] },
          axisLabel: {
            color: "#9aa7b4",
            formatter: (value) => new Intl.NumberFormat(undefined, {
              notation: "compact", maximumFractionDigits: 1,
            }).format(value),
          },
          splitLine: { lineStyle: { color: "#2d3846", type: "dashed" } },
        },
        series: groups.map((group) => ({
          name: group.name,
          type: "bar",
          stack: "unused",
          cursor: this.odcrUsageChartGroupBy === "none" ? "default" : "pointer",
          barMaxWidth: 34,
          emphasis: { focus: "series" },
          itemStyle: { color: this._odcrUsageGroupColors[group.name], borderRadius: 2 },
          data: group.values,
        })),
      };
      this._odcrUsageChartAdapter.render(container, option);
    },

    applyOdcrUsageChartGroupFilter(seriesName) {
      if (this.odcrUsageChartGroupBy === "none") return;
      const group = this._odcrUsageChartGroups.find((item) => item.name === seriesName);
      if (!group) return;
      const filterKey = {
        capacityReservationGroup: "capacityReservationGroupName",
        subscription: "subscriptionId",
        resourceGroup: "resourceGroup",
        location: "location",
      }[this.odcrUsageChartGroupBy];
      const contextFieldKey = this.odcrUsageChartGroupBy.startsWith("businessContext:")
        ? this.odcrUsageChartGroupBy.slice("businessContext:".length)
        : null;
      if (contextFieldKey) {
        const filter = this.odcrUsageFilters.find((item) => item.key === businessContextFilterKey(contextFieldKey));
        if (!filter?.options.some((option) => option.value === group.filterValue)) return;
        filter.selected = [group.filterValue];
      } else if (filterKey) {
        const filter = this.odcrUsageFilters.find((item) => item.key === filterKey);
        if (!filter?.options.some((option) => option.value === group.filterValue)) return;
        filter.selected = [group.filterValue];
      } else if (this.odcrUsageChartGroupBy === "capacityReservation") {
        const groupFilter = this.odcrUsageFilters.find((item) => item.key === "capacityReservationGroupName");
        if (group.parentFilterValue && groupFilter?.options.some((option) => option.value === group.parentFilterValue)) {
          groupFilter.selected = [group.parentFilterValue];
        }
        this.odcrUsageFilter = group.filterValue || "";
      }
      this.rebuildOdcrUsageView();
    },

    setOdcrUsageChartGroupBy(groupBy) {
      const allowed = ["none", "capacityReservationGroup", "capacityReservation", "subscription", "resourceGroup", "location"];
      const contextFieldKey = groupBy.startsWith("businessContext:")
        ? groupBy.slice("businessContext:".length)
        : null;
      if (!allowed.includes(groupBy) && !this.odcrUsageBusinessContextFields.some((field) => field.key === contextFieldKey)) return;
      if (this.odcrUsageView === "group" && groupBy === "capacityReservation") return;
      this.odcrUsageChartGroupBy = groupBy;
      this.scheduleOdcrUsageChartRender();
    },

    odcrUsageChartGroupLabel() {
      const fixedLabel = {
        none: "None",
        capacityReservationGroup: "Capacity Reservation Group",
        capacityReservation: "Capacity reservation",
        subscription: "Subscription",
        resourceGroup: "Resource Group",
        location: "Location",
      }[this.odcrUsageChartGroupBy];
      if (fixedLabel) return fixedLabel;
      const fieldKey = this.odcrUsageChartGroupBy.startsWith("businessContext:")
        ? this.odcrUsageChartGroupBy.slice("businessContext:".length)
        : null;
      return this.odcrUsageBusinessContextFields.find((field) => field.key === fieldKey)?.name || "Business Context";
    },

    formatOdcrUsageChartValue(value) {
      if (this.odcrUsageMetric === "cost") {
        const currency = this.odcrUsageMetadata?.usage?.currency;
        if (currency) {
          return new Intl.NumberFormat(undefined, {
            style: "currency", currency, maximumFractionDigits: 2,
          }).format(value);
        }
        return Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
      }
      return `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })} h`;
    },

    odcrUsageReservationDisplayName(row) {
      const groupName = row.capacityReservationGroupName || "";
      const reservationName = row.name || "";
      if (groupName && reservationName) return `${groupName}/${reservationName}`;
      return groupName || reservationName || "—";
    },

    odcrUsageChartMessage() {
      if (this.odcrUsageLoading) return "Loading unused capacity history...";
      if (!this.odcrUsageLoaded) return "";
      if (!this.odcrUsageMetadata?.usage?.isAvailable) return "Usage history is not available yet.";
      if (!this.filteredOdcrUsageRows.length) {
        return this.odcrUsageView === "group"
          ? "No capacity reservation groups match the current filters."
          : "No capacity reservations match the current filters.";
      }
      return "No unused values are available for the selected period.";
    },

    updateOdcrUsagePage() {
      this.paginatedOdcrUsageRows = paginateTableRows(
        this.filteredOdcrUsageRows,
        this.odcrUsagePage,
        this.odcrUsagePageSize,
      );
    },

    setOdcrUsagePage(page) {
      this.odcrUsagePage = Math.max(1, Math.min(page, this.odcrUsagePageCount));
      this.updateOdcrUsagePage();
    },

    setOdcrUsagePageSize(value) {
      const pageSize = Number(value);
      if (![20, 50, 100, 200].includes(pageSize)) return;
      this.odcrUsagePageSize = pageSize;
      this.rebuildOdcrUsageView();
    },

    odcrUsagePageStart() {
      return tablePageStart(
        this.filteredOdcrUsageRows.length,
        this.odcrUsagePage,
        this.odcrUsagePageSize,
      );
    },

    odcrUsagePageEnd() {
      return tablePageEnd(
        this.filteredOdcrUsageRows.length,
        this.odcrUsagePage,
        this.odcrUsagePageSize,
      );
    },

    onOdcrUsageSearchInput(value) {
      this.odcrUsageFilter = value;
      this.rebuildOdcrUsageView();
    },

    onOdcrUsageFilterChange() {
      this.rebuildOdcrUsageView();
    },

    get hasActiveLocalOdcrUsageFilters() {
      return this.odcrUsageFilter.trim().length > 0 || this.odcrUsageFilters.some((filter) =>
        filter.options.length > 0 && filter.selected.length !== filter.options.length
      );
    },

    resetLocalOdcrUsageFilters() {
      this.odcrUsageFilter = "";
      for (const filter of this.odcrUsageFilters) {
        filter.selected = filter.options.map((option) => option.value);
      }
      this.rebuildOdcrUsageView();
    },

    cycleOdcrUsageSort(key) {
      this.odcrUsageSort = nextTableSort(this.odcrUsageSort, key);
      this.rebuildOdcrUsageView();
    },

    odcrUsageSortIndicator(key) {
      if (this.odcrUsageSort.key !== key) return "";
      return this.odcrUsageSort.direction === "asc" ? "↑" : "↓";
    },

    setOdcrUsageMetric(metric) {
      if (!["hours", "cost"].includes(metric)) return;
      this.odcrUsageMetric = metric;
      this.rebuildOdcrUsageView(false);
    },

    toggleOdcrUsageDeleted() {
      this.odcrUsageIncludeDeleted = !this.odcrUsageIncludeDeleted;
      this.rebuildOdcrUsageView();
    },

    odcrUsageValue(row, window) {
      const suffix = this.odcrUsageMetric === "cost" ? "Cost" : "Hours";
      return row[`${window}Unused${suffix}`];
    },

    formatOdcrUsage(row, window) {
      const value = this.odcrUsageValue(row, window);
      if (value === null || value === undefined) return "—";
      if (this.odcrUsageMetric === "cost") {
        const currency = this.odcrUsageMetadata?.usage?.currency;
        if (!currency) return Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
        return new Intl.NumberFormat(undefined, {
          style: "currency",
          currency,
          maximumFractionDigits: 2,
        }).format(value);
      }
      return `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })} h`;
    },

    odcrUsagePeriodLines(days) {
      const usage = this.odcrUsageMetadata?.usage;
      if (!usage?.coverageEnd) return ["Usage coverage is not available yet."];
      const end = new Date(`${usage.coverageEnd}T00:00:00Z`);
      const start = new Date(end);
      start.setUTCDate(start.getUTCDate() - days + 1);
      const format = (value) => value.toISOString().slice(0, 10);
      const lagDays = Number(usage.finalizationLagDays || 0);
      const coverage = days === 1
        ? `Coverage date: ${format(end)}.`
        : `Coverage period: ${format(start)} → ${format(end)}.`;
      const lines = [coverage];
      if (lagDays > 0) {
        lines.push(
          `The latest ${lagDays} calendar day${lagDays === 1 ? " is" : "s are"} excluded because usage data has a ${lagDays}-day finalization lag.`
        );
      }
      lines.push("Missing usage rows inside the covered period count as zero.");
      return lines;
    },

    odcrUsagePeriodDescription(days) {
      return this.odcrUsagePeriodLines(days).join(" ");
    },

    exportOdcrUsageCsv() {
      const currency = this.odcrUsageMetadata?.usage?.currency || "";
      const reservationColumns = [
        ["Capacity reservation", (row) => this.odcrUsageReservationDisplayName(row)],
        ["Resource state", (row) => row.resourceExists === false ? "Deleted" : "Live"],
        ["Subscription", (row) => row.subscriptionName || row.subscriptionId],
        ...this.odcrUsageBusinessContextFields.map((field) => [
          field.name,
          (row) => this.odcrUsageBusinessContextValue(row, field.key),
        ]),
        ["Subscription ID", (row) => row.subscriptionId],
        ["Resource group", (row) => row.resourceGroup],
        ["Region", (row) => locationDisplay(row)],
        ["Deployment", (row) => row.resourceExists === false
          ? ""
          : resourceDeploymentDisplay(row, this.zoneDisplayMode)],
        ["Size", (row) => row.vmSize],
        ["Reserved quantity", (row) => row.reservedQuantity],
        ["Last day unused hours", (row) => row.lastDayUnusedHours],
        ["Last 7 days unused hours", (row) => row.last7DaysUnusedHours],
        ["Last 30 days unused hours", (row) => row.last30DaysUnusedHours],
        ["Last day unused cost", (row) => row.lastDayUnusedCost],
        ["Last 7 days unused cost", (row) => row.last7DaysUnusedCost],
        ["Last 30 days unused cost", (row) => row.last30DaysUnusedCost],
        ["Currency", () => currency],
      ];
      const groupColumns = [
        ["Capacity reservation group", (row) => row.name],
        ["Resource state", (row) => row.resourceExists === false ? "Deleted" : "Live"],
        ["Subscription", (row) => row.subscriptionName || row.subscriptionId],
        ...this.odcrUsageBusinessContextFields.map((field) => [
          field.name,
          (row) => this.odcrUsageBusinessContextValue(row, field.key),
        ]),
        ["Subscription ID", (row) => row.subscriptionId],
        ["Resource group", (row) => row.resourceGroup],
        ["Region", (row) => locationDisplay(row)],
        ["Deployment", (row) => row.deploymentType],
        ["Reservation count", (row) => row.reservationCount],
        ["Total reserved quantity", (row) => row.totalReservedQuantity],
        ["Last day unused hours", (row) => row.lastDayUnusedHours],
        ["Last 7 days unused hours", (row) => row.last7DaysUnusedHours],
        ["Last 30 days unused hours", (row) => row.last30DaysUnusedHours],
        ["Last day unused cost", (row) => row.lastDayUnusedCost],
        ["Last 7 days unused cost", (row) => row.last7DaysUnusedCost],
        ["Last 30 days unused cost", (row) => row.last30DaysUnusedCost],
        ["Currency", () => currency],
      ];
      const columns = this.odcrUsageView === "group" ? groupColumns : reservationColumns;
      downloadCsv(
        `azure-capacity-odcr-usage-${new Date().toISOString().slice(0, 10)}.csv`,
        columns,
        this.filteredOdcrUsageRows,
      );
    },

    odcrUsageLoadedSummaryText() {
      if (this.odcrUsageLoadDurationMs === null || this.odcrUsageLoadedAt === null) return "";
      const duration = this.odcrUsageLoadDurationMs < 1000
        ? `${Math.round(this.odcrUsageLoadDurationMs)} ms`
        : `${(this.odcrUsageLoadDurationMs / 1000).toFixed(1)} s`;
      const count = this.odcrUsageView === "group"
        ? `${this.odcrUsageGroups.length} groups`
        : `${this.odcrUsageReservations.length} reservations`;
      return `${count} loaded in ${duration}, ${this.relativeTimeText(this.odcrUsageLoadedAt)}`;
    },

  };
}
