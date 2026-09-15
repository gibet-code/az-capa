function createAdministrationFeature() {
  return {
    settings: null,
    appScope: null,
    appScopeLoading: false,
    businessContext: null,
    businessContextLoading: false,
    businessContextEditorOpen: false,
    businessContextEditingKey: null,
    businessContextDraft: null,
    businessContextFile: null,
    businessContextPreview: null,
    businessContextPreviewValid: false,
    businessContextEditorError: "",
    businessContextPreviewing: false,
    businessContextSaving: false,
    businessContextDeleting: false,
    businessContextTagOptions: [],
    businessContextTagOptionsLoading: false,
    async loadSettings() {
      if (!this.user.capabilities.administer) return;
      try {
        this.settings = await requestJson("/api/settings");
      } catch (error) {
        this.settings = null;
        this.notify(`Failed to load settings: ${error && error.message ? error.message : error}`);
      }
    },

    // Reload the live App Scope diagnostic from its own endpoint with a panel-local
    // loading indicator so repeat tab visits and manual refreshes show progress. Kept
    // separate from /api/settings so the App settings tab never triggers the live read.
    async loadAppScope() {
      if (!this.user.capabilities.administer) return;
      this.appScopeLoading = true;
      try {
        this.appScope = await requestJson("/api/app_scope");
      } catch (error) {
        this.appScope = { error: error && error.message ? error.message : String(error) };
      } finally {
        this.appScopeLoading = false;
      }
    },

    async loadBusinessContextFields() {
      if (!this.user.capabilities.administer) return;
      this.businessContextLoading = true;
      try {
        this.businessContext = await requestJson("/api/subscription-context");
      } catch (error) {
        this.businessContext = null;
        this.notify(`Failed to load Business Context: ${error && error.message ? error.message : error}`);
      } finally {
        this.businessContextLoading = false;
      }
    },

    businessContextSourceLabel(field) {
      const sourceType = field && field.valueSource ? field.valueSource.type : "";
      return {
        manual_file: "Mapping file",
        management_group_level: "Management group",
        subscription_tag: "Subscription tag",
        mca_invoice_section: "MCA invoice section",
      }[sourceType] || sourceType || "—";
    },

    openBusinessContextEditor(field = null) {
      this.businessContextEditingKey = field ? field.key : null;
      this.businessContextDraft = field
        ? JSON.parse(JSON.stringify(field))
        : {
            key: "",
            name: "",
            enabled: true,
            valueSource: { type: "manual_file", version: 1, config: {} },
          };
      this.businessContextFile = null;
      this.businessContextPreview = null;
      this.businessContextPreviewValid = false;
      this.businessContextEditorError = "";
      this.businessContextEditorOpen = true;
      if (this.businessContextDraft.valueSource.type === "subscription_tag") {
        this.loadBusinessContextTagOptions();
      }
    },

    closeBusinessContextEditor(force = false) {
      if (!force && (this.businessContextPreviewing || this.businessContextSaving || this.businessContextDeleting)) return;
      this.businessContextEditorOpen = false;
      this.businessContextEditingKey = null;
      this.businessContextDraft = null;
      this.businessContextFile = null;
      this.businessContextPreview = null;
      this.businessContextEditorError = "";
    },

    invalidateBusinessContextPreview() {
      this.businessContextPreview = null;
      this.businessContextPreviewValid = false;
      this.businessContextEditorError = "";
    },

    setBusinessContextSource(sourceType) {
      const config = sourceType === "management_group_level"
        ? { level: 1, outputMode: "selected_level" }
        : sourceType === "subscription_tag"
          ? { tagKey: "" }
          : {};
      this.businessContextDraft.valueSource = { type: sourceType, version: 1, config };
      this.businessContextFile = null;
      this.invalidateBusinessContextPreview();
      if (sourceType === "subscription_tag") this.loadBusinessContextTagOptions();
    },

    selectBusinessContextFile(event) {
      this.businessContextFile = event.target.files && event.target.files[0] ? event.target.files[0] : null;
      this.invalidateBusinessContextPreview();
    },

    async loadBusinessContextTagOptions() {
      if (!this.user.capabilities.administer) return;
      if (this.businessContextTagOptionsLoading) return;
      this.businessContextTagOptionsLoading = true;
      try {
        const data = await requestJson("/api/subscription-context/value-source-options/tags");
        this.businessContextTagOptions = data.value || [];
      } catch (error) {
        this.businessContextTagOptions = [];
        this.notify(`Could not load subscription tag keys: ${error && error.message ? error.message : error}`);
      } finally {
        this.businessContextTagOptionsLoading = false;
      }
    },

    businessContextDefinition() {
      const draft = this.businessContextDraft;
      const definition = {
        key: (draft.key || "").trim().toLowerCase(),
        name: (draft.name || "").trim(),
        enabled: !!draft.enabled,
        valueSource: JSON.parse(JSON.stringify(draft.valueSource)),
      };
      if (draft.revision) definition.revision = draft.revision;
      return definition;
    },

    businessContextCanPreview() {
      const draft = this.businessContextDraft;
      if (!draft || !(draft.key || "").trim() || !(draft.name || "").trim()) return false;
      const source = draft.valueSource;
      if (source.type === "manual_file") return !!this.businessContextFile;
      if (source.type === "management_group_level") return !!source.config.level && !!source.config.outputMode;
      if (source.type === "subscription_tag") return !!(source.config.tagKey || "").trim();
      return false;
    },

    businessContextRequest(endpoint, method) {
      const definition = this.businessContextDefinition();
      if (definition.valueSource.type === "manual_file") {
        const body = new FormData();
        body.append("definition", JSON.stringify(definition));
        body.append("file", this.businessContextFile);
        return requestJson(endpoint, { method, body });
      }
      return requestJson(endpoint, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(definition),
      });
    },

    async previewBusinessContext() {
      if (!this.businessContextCanPreview()) return;
      this.businessContextPreviewing = true;
      this.businessContextEditorError = "";
      try {
        this.businessContextPreview = await this.businessContextRequest("/api/subscription-context/preview", "POST");
        this.businessContextPreviewValid = true;
      } catch (error) {
        this.businessContextPreview = null;
        this.businessContextPreviewValid = false;
        this.businessContextEditorError = error && error.message ? error.message : String(error);
      } finally {
        this.businessContextPreviewing = false;
      }
    },

    async saveBusinessContext() {
      if (!this.businessContextPreviewValid) return;
      this.businessContextSaving = true;
      this.businessContextEditorError = "";
      const editing = !!this.businessContextEditingKey;
      const endpoint = editing
        ? `/api/subscription-context/${encodeURIComponent(this.businessContextEditingKey)}`
        : "/api/subscription-context";
      try {
        const data = await this.businessContextRequest(endpoint, editing ? "PATCH" : "POST");
        this.notify(`${data.field.name} saved.`, "ok");
        await this.loadBusinessContextFields();
        this.closeBusinessContextEditor(true);
      } catch (error) {
        this.businessContextEditorError = error && error.message ? error.message : String(error);
      } finally {
        this.businessContextSaving = false;
      }
    },

    async deleteBusinessContext() {
      if (!this.businessContextEditingKey) return;
      if (!confirm(`Delete ${this.businessContextDraft.name}? The Field key can be reused after deletion.`)) return;
      this.businessContextDeleting = true;
      this.businessContextEditorError = "";
      try {
        await requestResponse(`/api/subscription-context/${encodeURIComponent(this.businessContextEditingKey)}`, {
          method: "DELETE",
        });
        this.notify(`${this.businessContextDraft.name} deleted.`, "ok");
        await this.loadBusinessContextFields();
        this.closeBusinessContextEditor(true);
      } catch (error) {
        this.businessContextEditorError = error && error.message ? error.message : String(error);
      } finally {
        this.businessContextDeleting = false;
      }
    },

    downloadBusinessContextTemplate(format) {
      const params = new URLSearchParams({ format });
      if (this.businessContextEditingKey && this.businessContextDraft.valueSource.type === "manual_file") {
        params.set("fieldKey", this.businessContextEditingKey);
      }
      window.location.href = `/api/subscription-context/templates/manual?${params}`;
    },

    businessContextTopValues(summary) {
      if (!summary || !summary.valueCounts) return [];
      const combined = new Map();
      for (const item of summary.valueCounts) {
        const key = String(item.value).toLocaleLowerCase();
        const current = combined.get(key);
        if (current) current.subscriptionCount += item.subscriptionCount;
        else combined.set(key, { value: item.value, subscriptionCount: item.subscriptionCount });
      }
      return [...combined.values()]
        .sort((left, right) => right.subscriptionCount - left.subscriptionCount || String(left.value).localeCompare(String(right.value)))
        .slice(0, 15);
    },

    get appScopeError() {
      const scope = this.appScope;
      return scope && scope.error ? scope.error : null;
    },

    get appScopeHealth() {
      const scope = this.appScope;
      if (!scope || scope.error) {
        return { state: "error", label: "Error", badge: "err", message: "App Scope diagnostics are unavailable." };
      }
      if (scope.isEmpty) {
        return { state: "error", label: "Error", badge: "err", message: "The effective scope resolved to zero subscriptions; queries will return nothing." };
      }
      if (scope.hasUnresolved) {
        return { state: "warning", label: "Warning", badge: "warn", message: "Some configured selectors did not resolve." };
      }
      return { state: "ok", label: "OK", badge: "ok", message: "The effective scope resolved to one or more subscriptions." };
    },

    get appScopeResolvedSummary() {
      const scope = this.appScope;
      if (!scope || !Array.isArray(scope.subscriptions)) return "";
      const resolved = scope.subscriptions.filter((row) => row.resolved).length;
      return `${resolved} of ${scope.subscriptions.length} configured subscriptions resolved`;
    },

    scopeRows(scope) {
      const subscriptionIds = scope.subscriptionIds || [];
      const managementGroupIds = scope.managementGroupIds || [];
      const locations = scope.locations || [];
      let subscriptionScope = "All Subscriptions";
      if (subscriptionIds.length) {
        subscriptionScope = `${subscriptionIds.length} ${subscriptionIds.length === 1 ? "Subscription" : "Subscriptions"} selected`;
      } else if (managementGroupIds.length) {
        subscriptionScope = `${managementGroupIds.length} ${managementGroupIds.length === 1 ? "Management group" : "Management groups"} selected`;
      }
      return [
        { key: "Subscription Scope", value: subscriptionScope },
        {
          key: "Locations",
          value: locations.length
            ? `${locations.length} ${locations.length === 1 ? "Location" : "Locations"} selected`
            : "All Locations",
        },
      ];
    },

    get settingsGroups() {
      const settings = this.settings;
      if (!settings) return [];
      const rows = (value, labels) =>
        Object.entries(labels).map(([key, label]) => ({ key: label, value: this.fmt(value[key]) }));
      return [
        { title: "Scope", rows: this.scopeRows(settings.scope) },
        {
          title: "Cost Management",
          rows: rows(settings.costManagement, {
            method: "Method",
            agreementType: "Agreement type",
            billingAccountId: "Billing account ID",
            billingProfileId: "Billing profile ID",
            maxConcurrency: "Max concurrency",
            timeChunkDays: "Time chunk (days)",
            finalizationLagDays: "Finalization lag (days)",
          }),
        },
        {
          title: "VM Usage",
          rows: rows(settings.vmUsage, {
            historicalDays: "Historical days",
            tableName: "Table name",
            checkpointTableName: "Checkpoint table",
          }),
        },
        {
          title: "Capacity Reservation Usage",
          rows: rows(settings.crUsage, {
            tableName: "Table name",
            checkpointTableName: "Checkpoint table",
          }),
        },
        {
          title: "Activity Log",
          rows: rows(settings.activityLog, {
            operationNames: "Operation names",
            terminalStatuses: "Terminal statuses",
            historicalDays: "Historical days",
            refreshOverlapHours: "Refresh overlap (hours)",
            ingestionLagMinutes: "Ingestion lag (minutes)",
            maxConcurrency: "Max concurrency",
            locations: "Locations",
            tableName: "Table name",
            checkpointTableName: "Checkpoint table",
          }),
        },
        {
          title: "Zone Mapping",
          rows: rows(settings.zoneMapping, {
            maxConcurrency: "Max concurrency",
            locations: "Locations",
            containerName: "Blob container",
            snapshotBlob: "Current snapshot",
          }),
        },
        {
          title: "Compute SKU Properties",
          rows: rows(settings.computeSkus, {
            schedule: "Schedule",
            locations: "Locations",
            containerName: "Blob container",
            manifestBlob: "Current manifest",
          }),
        },
        {
          title: "Runtime",
          rows: rows(settings.runtime, {
            runningLocally: "Running locally",
            tableStorageConnection: "Table storage connection",
          }),
        },
      ];
    },
  };
}
