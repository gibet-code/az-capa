function createFeatureActivationFeature() {
  return {
    featureSnapshot: null,
    featureLoadError: "",
    adminFeatures: null,
    featureSaving: null,

    async loadFeatureSnapshot() {
      if (!this.user.capabilities.useOdcr) return;
      try {
        const data = await requestJson("/api/features", {}, "features unavailable");
        const features = Array.isArray(data.features) ? data.features : [];
        this.featureSnapshot = Object.fromEntries(
          features.map((feature) => [feature.id, feature.enabled === true]),
        );
        this.adminFeatures = features.map((feature) => ({ ...feature }));
        this.featureLoadError = "";
      } catch (error) {
        this.featureSnapshot = null;
        this.adminFeatures = null;
        this.featureLoadError = error && error.message ? error.message : String(error);
      }
    },

    featureEnabled(featureId) {
      return !!(this.featureSnapshot && this.featureSnapshot[featureId] === true);
    },

    firstEnabledFeatureRoute() {
      if (this.featureEnabled("odcr")) return "odcr/coverage";
      if (this.featureEnabled("allocation_events")) return "allocation-events";
      return null;
    },

    enabledAdminFeatureCount() {
      return (this.adminFeatures || []).filter((feature) => feature.enabled).length;
    },

    featureToggleDisabled(feature) {
      return this.featureSaving !== null
        || (feature.enabled && this.enabledAdminFeatureCount() === 1);
    },

    async setFeatureEnabled(feature, enabled) {
      if (!this.user.capabilities.administer || this.featureToggleDisabled(feature)) return;
      this.featureSaving = feature.id;
      try {
        const updated = await requestJson(`/api/features/${encodeURIComponent(feature.id)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        });
        this.adminFeatures = this.adminFeatures.map((item) =>
          item.id === updated.id ? { ...item, ...updated } : item
        );
        this.notify("Saved. Reload the app for navigation changes to take effect.", "ok");
      } catch (error) {
        this.adminFeatures = this.adminFeatures.map((item) => ({ ...item }));
        this.notify(`Failed to update feature: ${error && error.message ? error.message : error}`);
      } finally {
        this.featureSaving = null;
      }
    },
  };
}
