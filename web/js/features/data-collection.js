function createDataCollectionFeature() {
  return {
    statuses: { vm_usage: null, cr_usage: null, activity_log: null, zone_mapping: null, compute_skus: null, subscription_reference: null, location_reference: null, subscription_context: null },
    statusLoading: { vm_usage: false, cr_usage: false, activity_log: false, zone_mapping: false, compute_skus: false, subscription_reference: false, location_reference: false, subscription_context: false },
    dcPipelines: null,
    dcPipelinesLoading: false,
    dcPipelinesError: "",
    dcResetting: false,
    dcConfigOpen: false,
    dcConfigDraft: null,
    dcConfigError: "",
    dcConfigSaving: false,
    dcDetailsOpen: false,
    dcDetailsPipeline: null,
    dcDetailsRuns: [],
    dcDetailsLoading: false,
    dcDetailsError: "",
    dcDetailsContinuation: null,
    collectionSections: [
      { kind: "vm_usage", title: "Virtual Machine Usage", label: "Virtual Machine Usage" },
      { kind: "cr_usage", title: "Capacity Reservation Usage", label: "Capacity Reservation Usage" },
      { kind: "activity_log", title: "Activity Log", label: "Activity Log" },
      { kind: "zone_mapping", title: "Zone Mapping", label: "Zone Mapping" },
      { kind: "compute_skus", title: "Compute SKU Properties", label: "Compute SKU Properties" },
      { kind: "subscription_reference", title: "Subscription Reference Data", label: "Subscription Reference Data", flushable: false },
      { kind: "location_reference", title: "Location Reference Data", label: "Location Reference Data", flushable: false },
      { kind: "subscription_context", title: "Business Context", label: "Business Context" },
    ],

    sectionTitle(kind) {
      const section = this.collectionSections.find((item) => item.kind === kind);
      return section ? section.title : kind;
    },

    isRefreshBlocked(kind) {
      const status = this.statuses[kind];
      return !!(status && status.isRefreshEnabled === false);
    },

    async loadStatuses() {
      if (!this.user.capabilities.administer) return;
      for (const section of this.collectionSections) {
        await this.loadStatus(section.kind);
      }
    },

    // One cheap call feeds the schedule table (registry + config + run state only).
    async loadPipelines() {
      if (!this.user.capabilities.administer) return;
      this.dcPipelinesLoading = true;
      try {
        const data = await requestJson("/api/data-collection", {}, "schedule unavailable");
        this.dcPipelines = data.pipelines || [];
        this.dcPipelinesError = "";
      } catch (error) {
        this.dcPipelines = null;
        this.dcPipelinesError = error && error.message ? error.message : String(error);
      } finally {
        this.dcPipelinesLoading = false;
      }
    },

    // Clears every cadence/enablement override so the schedule reverts to code defaults.
    async resetPipelineDefaults() {
      if (!this.user.capabilities.administer) return;
      if (!confirm("Reset the data collection schedule to defaults? All custom cadences and enablement changes will be discarded.")) return;
      this.dcResetting = true;
      try {
        await requestResponse("/api/data-collection/reset-defaults", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        await this.loadPipelines();
      } catch (error) {
        this.notify(`Reset to defaults failed: ${error && error.message ? error.message : error}`);
      } finally {
        this.dcResetting = false;
      }
    },
    // Frequency choices for the config drawer (scheduled cadences only — "Manual" is
    // expressed by disabling the pipeline, not by a zero frequency).
    dcFrequencyChoices(seconds) {
      const presets = [
        { value: 900, label: "15 minutes" },
        { value: 1800, label: "30 minutes" },
        { value: 3600, label: "1 hour" },
        { value: 21600, label: "6 hours" },
        { value: 43200, label: "12 hours" },
        { value: 86400, label: "1 day" },
        { value: 604800, label: "7 days" },
      ];
      if (seconds && !presets.some((choice) => choice.value === seconds)) {
        presets.push({ value: seconds, label: this.dcFrequencyLabel(seconds) });
        presets.sort((a, b) => a.value - b.value);
      }
      return presets;
    },

    dcFrequencyLabel(seconds) {
      if (!seconds) return "Manual";
      if (seconds % 86400 === 0) return `${seconds / 86400} day(s)`;
      if (seconds % 3600 === 0) return `${seconds / 3600} hour(s)`;
      if (seconds % 60 === 0) return `${seconds / 60} minute(s)`;
      return `${seconds}s`;
    },

    // A disabled pipeline runs on manual refresh only; an enabled one shows its cadence.
    dcFrequencyText(row) {
      return row.enabled && row.frequencySeconds ? this.dcFrequencyLabel(row.frequencySeconds) : "Manual";
    },

    // True when an enabled pipeline's next scheduled run is now overdue.
    pipelineDue(row) {
      this._relativeTimeTick;
      if (!row || !row.enabled || !row.nextDueAt) return false;
      const t = new Date(row.nextDueAt).getTime();
      return !Number.isNaN(t) && t <= Date.now();
    },

    // Percent-complete from either progress shape: normalized {complete 0..1} (schedule
    // list / run-history) or raw {percentCompleted} (status / notifications).
    runningPercent(progress) {
      if (!progress) return null;
      if (typeof progress.complete === "number") return Math.round(progress.complete * 100);
      if (typeof progress.percentCompleted === "number") return Math.round(progress.percentCompleted);
      return null;
    },

    // One status-bullet contract shared by the schedule table, the details drawer, and
    // the notifications drawer. Running shows "Running - xx%" (spinner supplied by the
    // markup) with the custom-status label carried as the hover tooltip.
    collectorStatusDisplay(running, outcome, progress) {
      if (running) {
        const pct = this.runningPercent(progress);
        return {
          running: true,
          cls: "running",
          text: pct === null ? "Running" : `Running - ${pct}%`,
          title: (progress && progress.label) || "",
        };
      }
      const key = (outcome || "").toLowerCase();
      if (!key) return { running: false, cls: "muted", text: "—", title: "" };
      if (key === "completed") return { running: false, cls: "ok", text: "Completed", title: "" };
      if (key === "partial") return { running: false, cls: "warn", text: "Partial", title: "" };
      if (key === "no_work") return { running: false, cls: "muted", text: "No work", title: "" };
      return { running: false, cls: "err", text: key.charAt(0).toUpperCase() + key.slice(1), title: "" };
    },

    // --- Config edit drawer (enable/disable + cadence) ---
    openPipelineConfig(row) {
      if (!this.user.capabilities.administer) return;
      this.dcConfigDraft = {
        id: row.id,
        name: row.name,
        canDisable: row.canDisable,
        enabled: !!row.enabled,
        frequencySeconds: row.frequencySeconds || 86400,
        anchorSeconds: row.anchorSeconds || 0,
        revision: row.revision,
      };
      this.dcConfigError = "";
      this.dcConfigOpen = true;
    },

    closePipelineConfig() {
      // Keep the draft set; a null draft would make surviving drawer bindings
      // throw during Alpine's x-if teardown. openPipelineConfig() overwrites it.
      this.dcConfigOpen = false;
    },

    async savePipelineConfig() {
      if (!this.user.capabilities.administer || !this.dcConfigDraft) return;
      const draft = this.dcConfigDraft;
      this.dcConfigSaving = true;
      this.dcConfigError = "";
      try {
        await requestResponse(`/api/data-collection/${draft.id}/config`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            enabled: !!draft.enabled,
            frequencySeconds: Number(draft.frequencySeconds),
            anchorSeconds: Number(draft.anchorSeconds || 0),
            expectedRevision: draft.revision,
          }),
        });
        this.notify(`${draft.name} schedule updated.`, "ok");
        this.closePipelineConfig();
        await this.loadPipelines();
      } catch (error) {
        this.dcConfigError = error && error.message ? error.message : String(error);
      } finally {
        this.dcConfigSaving = false;
      }
    },

    // --- Details drawer (live status panel + run history) ---
    async openDetails(row) {
      if (!this.user.capabilities.administer) return;
      this.dcDetailsOpen = true;
      this.dcDetailsPipeline = { id: row.id, name: row.name, supportsFlush: row.supportsFlush };
      this.dcDetailsRuns = [];
      this.dcDetailsContinuation = null;
      this.dcDetailsError = "";
      await this.refreshDetails();
    },

    closeDetails() {
      // Keep the pipeline set; a null pipeline would make surviving drawer
      // bindings throw during Alpine's x-if teardown. openDetails() overwrites it.
      this.dcDetailsOpen = false;
    },

    // The drawer's Refresh button reloads both the status panel and the run history.
    async refreshDetails() {
      if (!this.dcDetailsPipeline) return;
      this.dcDetailsRuns = [];
      this.dcDetailsContinuation = null;
      await Promise.all([
        this.loadStatus(this.dcDetailsPipeline.id),
        this.loadRunHistory(),
      ]);
    },

    get dcDetailsStatus() {
      return this.dcDetailsPipeline ? this.statuses[this.dcDetailsPipeline.id] : null;
    },

    // Live progress for a pipeline kind: prefer a running notification, else its status.
    progressForKind(kind) {
      const running = this.notifications.find((n) => n.kind === kind && n.isRunning && n.progress);
      if (running) return running.progress;
      const status = this.statuses[kind];
      return status && status.progress ? status.progress : null;
    },

    // Fetch one page of run history; passes the RowKey continuation to append the next.
    async loadRunHistory() {
      if (!this.dcDetailsPipeline) return;
      this.dcDetailsLoading = true;
      try {
        const params = new URLSearchParams({ limit: "50" });
        if (this.dcDetailsContinuation) params.set("after", this.dcDetailsContinuation);
        const data = await requestJson(
          `/api/data-collection/${this.dcDetailsPipeline.id}/run-history?${params}`,
          {},
          "run history unavailable",
        );
        this.dcDetailsRuns = [...this.dcDetailsRuns, ...(data.runs || [])];
        this.dcDetailsContinuation = data.nextContinuation || null;
        this.dcDetailsError = "";
      } catch (error) {
        this.dcDetailsError = error && error.message ? error.message : String(error);
      } finally {
        this.dcDetailsLoading = false;
      }
    },

    // Flush destroys collected data; require an explicit confirm before dispatching.
    confirmFlush(row) {
      if (!this.user.capabilities.administer) return;
      if (!confirm(`Flush all collected data for ${row.name}? The schedule stays; the next run repopulates it.`)) return;
      this.runOp(row.id, "flush");
    },

    dcRunSummaryText(run) {
      if (run.status === "running") {
        return run.progress && run.progress.label ? run.progress.label : "In progress";
      }
      if (!run.summary) return "—";
      return Object.entries(run.summary)
        .map(([key, value]) => `${key}: ${value}`)
        .join(", ");
    },

    async loadStatus(kind) {
      if (!this.user.capabilities.administer) return;
      this.statusLoading[kind] = true;
      try {
        this.statuses[kind] = await requestJson(
          `/api/data-collection/${kind}/status`,
          {},
          "status unavailable",
        );
      } catch (error) {
        this.statuses[kind] = { error: error && error.message ? error.message : String(error) };
      } finally {
        this.statusLoading[kind] = false;
      }
    },

    refreshStatus(kind) {
      this.loadStatus(kind);
    },

    async runOp(kind, operation) {
      if (!this.user.capabilities.administer) return;
      const title = this.sectionTitle(kind);
      try {
        await requestResponse(`/api/data-collection/${kind}/${operation}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
      } catch (error) {
        this.notify(`${title} ${operation} failed: ${error && error.message ? error.message : error}`);
        this.loadStatus(kind);
        return;
      }
      await this.loadStatus(kind);
      setTimeout(() => {
        this.loadNotifications();
        this.loadPipelines();
        if (this.dcDetailsOpen && this.dcDetailsPipeline && this.dcDetailsPipeline.id === kind) {
          this.refreshDetails();
        }
      }, 800);
    },
  };
}