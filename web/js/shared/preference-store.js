const FRONTEND_PREFERENCE_SCHEMA_VERSION = 1;
const FRONTEND_PREFERENCE_KEY_PREFIX = "az-capacity.frontend-preferences.v1";
const DEFAULT_GLOBAL_SCOPE = Object.freeze({
  subscriptionIds: null,
  locations: null,
  contextFilters: Object.freeze([]),
});
const DEFAULT_COVERAGE_UNMARKED_OWNER = "required";

function cloneFrontendGlobalScope(scope) {
  return {
    subscriptionIds: scope.subscriptionIds === null ? null : [...scope.subscriptionIds],
    locations: scope.locations === null ? null : [...scope.locations],
    contextFilters: scope.contextFilters.map((criterion) => ({
      fieldKey: criterion.fieldKey,
      values: [...criterion.values],
    })),
  };
}

function defaultFrontendPreferences() {
  return {
    schemaVersion: FRONTEND_PREFERENCE_SCHEMA_VERSION,
    globalScope: cloneFrontendGlobalScope(DEFAULT_GLOBAL_SCOPE),
    coverageUnmarkedOwner: DEFAULT_COVERAGE_UNMARKED_OWNER,
  };
}

function isPlainPreferenceObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNonEmptyStringArray(value) {
  return Array.isArray(value)
    && value.length > 0
    && value.every((item) => typeof item === "string" && item.length > 0);
}

function validateFrontendGlobalScope(value) {
  if (!isPlainPreferenceObject(value)) return null;
  if (value.subscriptionIds !== null && !isNonEmptyStringArray(value.subscriptionIds)) return null;
  if (value.locations !== null && !isNonEmptyStringArray(value.locations)) return null;
  if (!Array.isArray(value.contextFilters)) return null;

  const fieldKeys = new Set();
  for (const criterion of value.contextFilters) {
    if (!isPlainPreferenceObject(criterion)) return null;
    if (typeof criterion.fieldKey !== "string" || criterion.fieldKey.length === 0) return null;
    if (fieldKeys.has(criterion.fieldKey)) return null;
    if (!Array.isArray(criterion.values) || criterion.values.length === 0) return null;
    if (!criterion.values.every((item) => item === null || typeof item === "string")) return null;
    fieldKeys.add(criterion.fieldKey);
  }

  return cloneFrontendGlobalScope(value);
}

function validateFrontendPreferenceRecord(value) {
  if (!isPlainPreferenceObject(value) || value.schemaVersion !== FRONTEND_PREFERENCE_SCHEMA_VERSION) return null;
  const defaults = defaultFrontendPreferences();
  return {
    schemaVersion: FRONTEND_PREFERENCE_SCHEMA_VERSION,
    globalScope: validateFrontendGlobalScope(value.globalScope) || defaults.globalScope,
    coverageUnmarkedOwner: ["required", "not_required"].includes(value.coverageUnmarkedOwner)
      ? value.coverageUnmarkedOwner
      : defaults.coverageUnmarkedOwner,
  };
}

function frontendPreferenceStorageKey(user) {
  if (user && user.local === true) return `${FRONTEND_PREFERENCE_KEY_PREFIX}.local`;
  if (!user || typeof user.tenantId !== "string" || !user.tenantId) return null;
  if (typeof user.objectId !== "string" || !user.objectId) return null;
  return `${FRONTEND_PREFERENCE_KEY_PREFIX}.${user.tenantId}.${user.objectId}`;
}

function createPreferenceStoreFeature() {
  return {
    preferenceStoreInitialized: false,
    _preferenceStorageKey: null,

    initializePreferences(user) {
      this._preferenceStorageKey = frontendPreferenceStorageKey(user);
      const preferences = this._readFrontendPreferences();
      this.globalScope = cloneFrontendGlobalScope(preferences.globalScope);
      this.globalScopeDraft = cloneFrontendGlobalScope(preferences.globalScope);
      this.coverageUnmarkedOwner = preferences.coverageUnmarkedOwner;
      this.preferenceStoreInitialized = true;
    },

    persistGlobalScope(globalScope) {
      const validatedScope = validateFrontendGlobalScope(globalScope);
      if (!validatedScope || !this._preferenceStorageKey) return;
      const preferences = this._readFrontendPreferences();
      preferences.globalScope = validatedScope;
      this._writeFrontendPreferences(preferences);
    },

    persistCoverageUnmarkedOwner(owner) {
      if (!["required", "not_required"].includes(owner) || !this._preferenceStorageKey) return;
      const preferences = this._readFrontendPreferences();
      preferences.coverageUnmarkedOwner = owner;
      this._writeFrontendPreferences(preferences);
    },

    _readFrontendPreferences() {
      const defaults = defaultFrontendPreferences();
      if (!this._preferenceStorageKey) return defaults;
      try {
        const serialized = window.localStorage.getItem(this._preferenceStorageKey);
        if (serialized === null) return defaults;
        const preferences = validateFrontendPreferenceRecord(JSON.parse(serialized));
        if (preferences) return preferences;
        try { window.localStorage.removeItem(this._preferenceStorageKey); } catch (_) { /* best effort */ }
      } catch (_) {
        console.warn("Frontend preferences could not be read; using page-session defaults.");
      }
      return defaults;
    },

    _writeFrontendPreferences(preferences) {
      if (!this._preferenceStorageKey) return;
      try {
        window.localStorage.setItem(this._preferenceStorageKey, JSON.stringify(preferences));
      } catch (_) {
        console.warn("Frontend preferences could not be retained for a future page load.");
      }
    },
  };
}
