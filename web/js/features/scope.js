function createScopeFeature() {
  return {
    scopeMenuOpen: false,
    scopeCatalogue: { subscriptions: [], locations: [], contextFields: [] },
    scopeInventoryLoaded: false,
    scopeInventoryLoading: false,
    scopeInventoryError: "",
    scopeSearch: { subscriptions: "", locations: "", context: {} },
    globalScope: { subscriptionIds: null, locations: null, contextFilters: [] },
    globalScopeDraft: { subscriptionIds: null, locations: null, contextFilters: [] },
    _scopeRequests: createLatestRequest(),

    openScopeMenu() {
      this.scopeMenuOpen = true;
      this.accountMenuOpen = false;
      this.notificationDrawerOpen = false;
      this.scopeSearch = { subscriptions: "", locations: "", context: {} };
      this.resetGlobalScopeDraft();
      if (!this.scopeInventoryLoaded && !this.scopeInventoryLoading) this.loadScopeInventory();
    },

    closeScopeMenu() {
      this.scopeMenuOpen = false;
      this.resetGlobalScopeDraft();
    },

    resetGlobalScopeDraft() {
      this.globalScopeDraft = JSON.parse(JSON.stringify(this.globalScope));
    },

    async loadScopeInventory({ reconcileContextFilters = false } = {}) {
      const request = this._scopeRequests.begin();
      this.scopeInventoryLoading = true;
      this.scopeInventoryError = "";
      try {
        const data = await requestJson("/api/global-filters/catalogue", { signal: request.signal });
        if (!this._scopeRequests.isCurrent(request.id)) return;
        this.scopeCatalogue = {
          subscriptions: data.subscriptions || [],
          locations: data.locations || [],
          contextFields: data.contextFields || [],
        };
        this.scopeInventoryLoaded = true;
        if (reconcileContextFilters) this.reconcileUnavailableContextFilters();
      } catch (error) {
        if (error && error.name === "AbortError") return;
        this.scopeInventoryError = error.message || String(error);
      } finally {
        if (this._scopeRequests.isCurrent(request.id)) this.scopeInventoryLoading = false;
        this._scopeRequests.finish(request.id);
      }
    },

    reconcileUnavailableContextFilters() {
      const availableFieldKeys = new Set(this.scopeCatalogue.contextFields.map((field) =>
        String(field.fieldKey || "").trim().toLocaleLowerCase()
      ));
      const unavailableCriteria = this.globalScope.contextFilters.filter((criterion) =>
        !availableFieldKeys.has(String(criterion.fieldKey).trim().toLocaleLowerCase())
      );
      if (!unavailableCriteria.length) return;

      const unavailableFieldKeys = new Set(unavailableCriteria.map((criterion) => criterion.fieldKey));
      this.globalScope = {
        ...this.globalScope,
        contextFilters: this.globalScope.contextFilters.filter((criterion) =>
          !unavailableFieldKeys.has(criterion.fieldKey)
        ),
      };
      this.globalScopeDraft = {
        ...this.globalScopeDraft,
        contextFilters: this.globalScopeDraft.contextFilters.filter((criterion) =>
          !unavailableFieldKeys.has(criterion.fieldKey)
        ),
      };
      if (typeof this.persistGlobalScope === "function") this.persistGlobalScope(this.globalScope);
      if (typeof this.notify === "function") {
        const labels = unavailableCriteria.map((criterion) => criterion.fieldKey).join(", ");
        this.notify(`Removed unavailable global Context filter${unavailableCriteria.length === 1 ? "" : "s"}: ${labels}.`, "warning");
      }
    },

    scopeOptions(kind) {
      const options = this.scopeCatalogue[kind] || [];
      const query = String(this.scopeSearch[kind] || "").trim().toLocaleLowerCase();
      if (!query) return options;
      return options.filter((option) =>
        `${option.label || ""} ${option.secondaryLabel || ""}`.toLocaleLowerCase().includes(query)
      );
    },

    scopeContextOptions(field) {
      const query = String(this.scopeSearch.context[field.fieldKey] || "").trim().toLocaleLowerCase();
      if (!query) return field.values || [];
      return (field.values || []).filter((option) =>
        String(option.label || "").toLocaleLowerCase().includes(query)
      );
    },

    scopeDimensionEnabled(kind) {
      return this.globalScopeDraft[kind] !== null;
    },

    setScopeDimensionEnabled(kind, enabled) {
      this.globalScopeDraft = { ...this.globalScopeDraft, [kind]: enabled ? [] : null };
    },

    toggleScopeDimensionValue(kind, value, checked) {
      const selected = new Set(this.globalScopeDraft[kind] || []);
      if (checked) selected.add(value); else selected.delete(value);
      this.globalScopeDraft = { ...this.globalScopeDraft, [kind]: [...selected] };
    },

    toggleAllScopeDimensionValues(kind) {
      const options = this.scopeCatalogue[kind] || [];
      const selected = this.globalScopeDraft[kind] || [];
      this.globalScopeDraft = {
        ...this.globalScopeDraft,
        [kind]: selected.length === options.length ? [] : options.map((option) => option.value),
      };
    },

    scopeContextCriterion(fieldKey) {
      return this.globalScopeDraft.contextFilters.find((item) => item.fieldKey === fieldKey) || null;
    },

    setScopeContextEnabled(fieldKey, enabled) {
      const contextFilters = this.globalScopeDraft.contextFilters
        .filter((item) => item.fieldKey !== fieldKey);
      if (enabled) contextFilters.push({ fieldKey, values: [] });
      this.globalScopeDraft = { ...this.globalScopeDraft, contextFilters };
    },

    toggleScopeContextValue(fieldKey, value, checked) {
      const criterion = this.scopeContextCriterion(fieldKey);
      if (!criterion) return;
      const selected = new Set(criterion.values);
      if (checked) selected.add(value); else selected.delete(value);
      this.globalScopeDraft = {
        ...this.globalScopeDraft,
        contextFilters: this.globalScopeDraft.contextFilters.map((item) =>
          item.fieldKey === fieldKey ? { ...item, values: [...selected] } : item
        ),
      };
    },

    get globalScopeDraftValid() {
      if (this.globalScopeDraft.subscriptionIds !== null && !this.globalScopeDraft.subscriptionIds.length) return false;
      if (this.globalScopeDraft.locations !== null && !this.globalScopeDraft.locations.length) return false;
      return this.globalScopeDraft.contextFilters.every((criterion) => criterion.values.length > 0);
    },

    async applyGlobalScope() {
      if (!this.globalScopeDraftValid) return;
      await this.commitGlobalScope(this.globalScopeDraft);
    },

    async removeAllGlobalScope() {
      await this.commitGlobalScope({ subscriptionIds: null, locations: null, contextFilters: [] });
    },

    async commitGlobalScope(scope) {
      this.globalScope = JSON.parse(JSON.stringify(scope));
      this.globalScopeDraft = JSON.parse(JSON.stringify(scope));
      if (typeof this.persistGlobalScope === "function") this.persistGlobalScope(this.globalScope);
      this.scopeMenuOpen = false;
      if (this.odcrTab === "usage") {
        this.vmsLoaded = false;
        await this.loadOdcrUsage(true);
      } else {
        this.odcrUsageLoaded = false;
        await this.loadVms(true);
      }
    },

    get globalScopeActive() {
      return this.globalScope.subscriptionIds !== null
        || this.globalScope.locations !== null
        || this.globalScope.contextFilters.length > 0;
    },

    globalScopeAppliesToFilter(filter) {
      if (!filter) return false;
      if (filter.key === "subscriptionId") return this.globalScope.subscriptionIds !== null;
      if (filter.key === "location") return this.globalScope.locations !== null;
      const fieldKey = filter.businessContextKey
        || (filter.key.startsWith("businessContext:") ? filter.key.slice("businessContext:".length) : null);
      return fieldKey !== null
        && this.globalScope.contextFilters.some((criterion) => criterion.fieldKey === fieldKey);
    },

    globalScopeSubscriptionIds() {
      return this.globalScope.subscriptionIds;
    },

    globalScopeRequest() {
      const request = {};
      if (this.globalScope.subscriptionIds !== null) request.subscriptionIds = this.globalScope.subscriptionIds;
      if (this.globalScope.locations !== null) request.locations = this.globalScope.locations;
      if (this.globalScope.contextFilters.length) request.contextFilters = this.globalScope.contextFilters;
      return request;
    },
  };
}
