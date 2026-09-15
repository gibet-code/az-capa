function createVmCoverageChartsFeature() {
  const goodColor = "#3fb950";
  const badColor = "#f85149";
  const warningColor = "#d29922";
  const neutralColor = "#8b949e";
  return {
    _coverageChartAdapters: null,
    _coverageResizeTimer: null,

    coveragePercent(value) {
      if (value === null || value === undefined) return "—";
      if (value === 0) return "0%";
      if (value < 1) return "<1%";
      return `${Math.round(value)}%`;
    },

    coverageReservationStatusColor(status) {
      if (status === "Allocated, within capacity") return goodColor;
      if (status === "Not associated") return neutralColor;
      if (status.includes("available") || status.includes("fully used")) return warningColor;
      return badColor;
    },

    scheduleCoverageReportCharts(attempt = 0) {
      if (typeof echarts === "undefined" || typeof this.$nextTick !== "function") return;
      this.$nextTick(() => {
        // Chart containers live inside an x-if-managed page; on route changes the refs
        // can lag a tick behind, so defer rendering until they remount rather than
        // rendering into a missing container (which would dispose the adapters).
        if (!this.$refs?.coverageDetailsChart) {
          if (attempt < 10) this.scheduleCoverageReportCharts(attempt + 1);
          return;
        }
        this.renderCoverageReportCharts();
      });
    },

    resizeCoverageReportCharts() {
      for (const adapter of Object.values(this._coverageChartAdapters || {})) adapter.resize();
      clearTimeout(this._coverageResizeTimer);
      this._coverageResizeTimer = setTimeout(() => {
        for (const adapter of Object.values(this._coverageChartAdapters || {})) adapter.resize();
      }, 120);
    },

    coveragePieOption(items, centerText = "", centerFontSize = 17, showPercentLabels = false) {
      return {
        animationDuration: 300,
        backgroundColor: "transparent",
        color: ODCR_CHART_PALETTE,
        tooltip: {
          trigger: "item",
          confine: true,
          formatter: (item) => `${escapeHtml(item.name)}: <strong>${item.value}</strong> (${item.percent}%)`,
        },
        legend: { show: false },
        graphic: centerText ? [{
          type: "text", left: "center", top: "center",
          style: { text: centerText, fill: "#e6edf3", fontSize: centerFontSize, fontWeight: 600 },
        }] : [],
        series: [{
          type: "pie",
          radius: ["48%", "72%"],
          center: ["50%", "50%"],
          label: showPercentLabels ? {
            show: true,
            position: "inside",
            formatter: "{d}%",
            color: "#e6edf3",
            fontSize: 9,
            fontWeight: 600,
          } : { show: false },
          labelLine: { show: false },
          minShowLabelAngle: showPercentLabels ? 8 : 0,
          data: items.map((item) => ({
            ...item,
            itemStyle: { color: item.color },
          })),
        }],
      };
    },

    coverageChartAdapter(name) {
      if (!this._coverageChartAdapters) this._coverageChartAdapters = {};
      if (!this._coverageChartAdapters[name]) {
        this._coverageChartAdapters[name] = createEchartsAdapter({
          onClick: (event) => {
            const data = event && event.data;
            if (!data) return;
            const visual = ({
              coverageGrouped: "coverage",
              unassociationGrouped: "unassociation",
              markingGrouped: "marking",
              prerequisitesGrouped: "prerequisites",
            })[name] || name;
            if (data.actions) {
              const action = data.actions[0];
              this.setCoverageFacetValues(
                visual,
                action.facet,
                action.values || [action.value],
              );
            } else if (data.facet) {
              this.setCoverageFacet(visual, data.facet, data.key);
            }
          },
        });
      }
      return this._coverageChartAdapters[name];
    },

    renderCoverageReportCharts() {
      const view = this.coverageReportView;
      if (!view || this.odcrTab !== "coverage") return;
      const details = view.coverageDetails.rows.map((row) => ({
        name: row.label, value: row.referenceCount, key: row.key,
        facet: "reservationStatus",
        selected: this.coverageFacetSelected("reservationStatus", row.key),
        color: this.coverageReservationStatusColor(row.key),
      }));
      this.coverageChartAdapter("details").render(
        this.$refs?.coverageDetailsChart,
        this.coveragePieOption(details, "", 17, true),
      );
      const totalMarking = view.marking.total;
      const markingItems = totalMarking ? [
        { name: "Decision recorded", value: totalMarking.markedReferenceCount, facet: "marking", key: "marked", selected: this.coverageFacetSelected("marking", "marked"), color: goodColor },
        { name: "No decision", value: totalMarking.referenceCount - totalMarking.markedReferenceCount, facet: "marking", key: "unmarked", selected: this.coverageFacetSelected("marking", "unmarked"), color: badColor },
      ] : [];
      const associationTotal = view.coverage.total;
      const associationItems = associationTotal ? [
        { name: "ODCR assigned", value: associationTotal.assignedReferenceCount, facet: "association", key: "assigned", selected: this.coverageFacetSelected("association", "assigned"), color: goodColor },
        { name: "Not associated", value: associationTotal.referenceCount - associationTotal.assignedReferenceCount, facet: "association", key: "not_associated", selected: this.coverageFacetSelected("association", "not_associated"), color: neutralColor },
      ] : [];
      const unassociationTotal = view.unassociation.total;
      const unassociationItems = unassociationTotal ? [
        { name: "Not associated", value: unassociationTotal.unassociatedReferenceCount, facet: "association", key: "not_associated", selected: this.coverageFacetSelected("association", "not_associated"), color: neutralColor },
        { name: "ODCR assigned", value: unassociationTotal.referenceCount - unassociationTotal.unassociatedReferenceCount, facet: "association", key: "assigned", selected: this.coverageFacetSelected("association", "assigned"), color: goodColor },
      ] : [];
      if (this.coverageReportGroupBy !== "none") {
        this.coverageChartAdapter("coverageGrouped").render(
          this.$refs?.coverageAssociationGroupedChart,
          this.coveragePieOption(associationItems, this.coveragePercent(associationTotal?.assignedPercent), 14),
        );
        this.coverageChartAdapter("unassociationGrouped").render(
          this.$refs?.coverageUnassociationGroupedChart,
          this.coveragePieOption(unassociationItems, this.coveragePercent(unassociationTotal?.unassociatedPercent), 14),
        );
        this.coverageChartAdapter("markingGrouped").render(
          this.$refs?.coverageMarkingGroupedChart,
          this.coveragePieOption(markingItems, this.coveragePercent(totalMarking?.markedPercent), 14),
        );
        const prerequisiteTotal = view.prerequisites.total;
        const prerequisiteGroupedItems = [
          {
            name: "Supported",
            value: prerequisiteTotal.statuses.Supported.referenceCount,
            facet: "prerequisiteStatus",
            key: "Supported",
            selected: this.coverageFacetSelected("prerequisiteStatus", "Supported"),
            color: goodColor,
          },
          {
            name: "Unsupported or unknown",
            value: prerequisiteTotal.statuses.Unsupported.referenceCount
              + prerequisiteTotal.statuses.Unknown.referenceCount,
            selected: this.coverageFacetSelected("prerequisiteStatus", "Unsupported")
              && this.coverageFacetSelected("prerequisiteStatus", "Unknown"),
            color: badColor,
            actions: [{ facet: "prerequisiteStatus", values: ["Unsupported", "Unknown"] }],
          },
        ];
        this.coverageChartAdapter("prerequisitesGrouped").render(
          this.$refs?.coveragePrerequisitesGroupedChart,
          this.coveragePieOption(
            prerequisiteGroupedItems,
            this.coveragePercent(prerequisiteTotal.statuses.Supported.percent),
            14,
          ),
        );
        for (const name of ["marking", "coverage", "unassociation", "prerequisites"]) {
          if (this._coverageChartAdapters?.[name]) this._coverageChartAdapters[name].clear();
        }
        return;
      }
      if (this._coverageChartAdapters?.markingGrouped) this._coverageChartAdapters.markingGrouped.clear();
      if (this._coverageChartAdapters?.prerequisitesGrouped) this._coverageChartAdapters.prerequisitesGrouped.clear();
      if (this._coverageChartAdapters?.coverageGrouped) this._coverageChartAdapters.coverageGrouped.clear();
      if (this._coverageChartAdapters?.unassociationGrouped) this._coverageChartAdapters.unassociationGrouped.clear();
      this.coverageChartAdapter("marking").render(
        this.$refs?.coverageMarkingChart,
        this.coveragePieOption(markingItems, this.coveragePercent(totalMarking?.markedPercent)),
      );
      const prerequisiteTotal = view.prerequisites.total;
      const prerequisiteItems = [
        {
          name: "Supported",
          value: prerequisiteTotal.statuses.Supported.referenceCount,
          facet: "prerequisiteStatus",
          key: "Supported",
          selected: this.coverageFacetSelected("prerequisiteStatus", "Supported"),
          color: goodColor,
        },
        {
          name: "Unsupported or unknown",
          value: prerequisiteTotal.statuses.Unsupported.referenceCount
            + prerequisiteTotal.statuses.Unknown.referenceCount,
          selected: this.coverageFacetSelected("prerequisiteStatus", "Unsupported")
            && this.coverageFacetSelected("prerequisiteStatus", "Unknown"),
          color: badColor,
          actions: [{ facet: "prerequisiteStatus", values: ["Unsupported", "Unknown"] }],
        },
      ];
      this.coverageChartAdapter("prerequisites").render(
        this.$refs?.coveragePrerequisitesChart,
        this.coveragePieOption(
          prerequisiteItems,
          this.coveragePercent(prerequisiteTotal.statuses.Supported.percent),
        ),
      );
      this.coverageChartAdapter("coverage").render(
        this.$refs?.coverageAssociationChart,
        this.coveragePieOption(associationItems, this.coveragePercent(associationTotal.assignedPercent)),
      );
      this.coverageChartAdapter("unassociation").render(
        this.$refs?.coverageUnassociationChart,
        this.coveragePieOption(unassociationItems, this.coveragePercent(unassociationTotal.unassociatedPercent)),
      );
    },
  };
}