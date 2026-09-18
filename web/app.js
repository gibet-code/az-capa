// Alpine root component for the admin SPA. Registered globally so `x-data="app()"`
// resolves when Alpine initializes.
function applyFeatures(root, ...features) {
  for (const feature of features) {
    const descriptors = Object.getOwnPropertyDescriptors(feature);
    for (const property of Object.keys(descriptors)) {
      if (Object.prototype.hasOwnProperty.call(root, property)) {
        throw new Error(`Duplicate Alpine feature property: ${property}`);
      }
    }
    Object.defineProperties(root, descriptors);
  }
  return root;
}

function app() {
  const root = {
    page: "settings",
    tab: "app",
    user: { name: "…", detail: "", authenticated: false, local: false, roles: [], capabilities: { useOdcr: false, administer: false } },
    identityResolved: false,
    localRoleSwitching: false,
    accountMenuOpen: false,
    odcrTab: "coverage",
    toasts: [],
    _toastSeq: 0,
    _timer: null,
    _relativeTimeTick: 0,
    _preferenceInitialization: null,

    // ── Lifecycle ────────────────────────────────────────────
    async init() {
      this.applyRoute(false);
      this._preferenceInitialization = this.initializeIdentityPreferencesAndRoute();
      await this._preferenceInitialization;
      window.addEventListener("hashchange", () => {
        this.applyRoute();
      });
      window.addEventListener("resize", () => setTimeout(() => {
        this.scheduleOdcrUsageChartRender();
        this.resizeCoverageReportCharts();
      }, 0));
      if (this.user.capabilities.administer) {
        this.loadSettings();
        this.loadNotifications();
        this._timer = setInterval(() => this.loadNotifications(), 5000);
      }
      setInterval(() => { this._relativeTimeTick += 1; }, 60000);
    },

    async initializeIdentityPreferencesAndRoute() {
      await this.fetchUser();
      this.identityResolved = true;
      this.initializePreferences(this.user);
      if (this.user.capabilities.useOdcr && this.globalScope.contextFilters.length) {
        await this.loadScopeInventory({ reconcileContextFilters: true });
      }
      this.applyRoute();
    },

    applyRoute(loadReport = true) {
      const hash = (window.location.hash || "#/settings").replace(/^#\//, "");
      if (this.identityResolved && !this.user.capabilities.useOdcr && !this.user.capabilities.administer) {
        this.page = "forbidden";
        this.notificationDrawerOpen = false;
        return;
      }
      if (this.identityResolved && !this.user.capabilities.administer && (hash === "settings" || hash === "notifications")) {
        window.location.hash = "#/odcr/coverage";
        return;
      }
      if (hash === "notifications" && this.user.capabilities.administer) {
        this.notificationDrawerOpen = true;
        this.page = "vm";
      } else {
        this.page = hash === "vm" || hash.startsWith("vm/") || hash.startsWith("odcr/") ? "vm" : "settings";
      }
      if (this.page === "vm") {
        this.activateOdcrTab(
          hash === "odcr/usage" || hash === "vm/usage" ? "usage" : "coverage",
          loadReport,
        );
        if (!loadReport) return;
      }
      if (this.page === "settings") {
        const sub = hash.startsWith("settings/") ? hash.slice("settings/".length) : "";
        const tab = this.settingsTabFromSlug(sub);
        if (this.tab !== tab) this.tab = tab;
        if (!loadReport || !this.user.capabilities.administer) return;
        if (tab === "data") this.loadPipelines();
        else if (tab === "app-scope") this.loadAppScope();
        else if (tab === "business-context") this.loadBusinessContextFields();
      }
    },

    settingsTabSlug(tab) {
      return tab === "data" ? "data-collection" : tab;
    },

    settingsTabFromSlug(slug) {
      switch (slug) {
        case "app-scope": return "app-scope";
        case "data-collection": return "data";
        case "business-context": return "business-context";
        default: return "app";
      }
    },

    go(page) {
      window.location.hash = "#/" + page;
    },

    selectTab(tab) {
      if (!this.user.capabilities.administer) return;
      window.location.hash = "#/settings/" + this.settingsTabSlug(tab);
    },

    selectOdcrTab(tab) {
      window.location.hash = tab === "usage" ? "#/odcr/usage" : "#/odcr/coverage";
      this.activateOdcrTab(tab);
    },

    activateOdcrTab(tab, loadReport = true) {
      this.odcrTab = tab;
      if (!loadReport) return;
      if (tab === "coverage" && !this.vmsLoaded) this.loadVms();
      if (tab === "coverage" && this.vmsLoaded) this.scheduleCoverageReportCharts();
      if (tab === "usage" && !this.odcrUsageLoaded) this.loadOdcrUsage();
      if (tab === "usage" && this.odcrUsageLoaded) this.scheduleOdcrUsageChartRender();
    },

    // ── Toasts / error surfacing ─────────────────────────────
    notify(message, kind = "error") {
      const id = ++this._toastSeq;
      this.toasts.push({ id, kind, message });
      setTimeout(() => this.dismissToast(id), 9000);
    },

    dismissToast(id) {
      this.toasts = this.toasts.filter((t) => t.id !== id);
    },

    // Pull the best human message out of a non-OK response (JSON {message|error},
    // then plain text, then the status line), always tagged with the HTTP code.
    async extractError(res, fallback) {
      return extractResponseError(res, fallback);
    },

    // ── Auth ─────────────────────────────────────────────────
    async fetchUser() {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const data = await requestJson("/api/me", { signal: controller.signal }, "identity unavailable");
        this.user = {
          name: data.name || "Signed in",
          detail: data.detail || "",
          authenticated: data.authenticated === true,
          local: data.local === true,
          roles: Array.isArray(data.roles) ? data.roles : [],
          capabilities: data.capabilities || { useOdcr: false, administer: false },
          tenantId: data.tenantId || "",
          objectId: data.objectId || "",
        };
      } catch (error) {
        this.user = {
          name: "Not authorized",
          detail: error && error.message ? error.message : "Identity unavailable",
          authenticated: false,
          local: false,
          roles: [],
          capabilities: { useOdcr: false, administer: false },
        };
      } finally {
        clearTimeout(timeout);
      }
    },

    async switchLocalRole(role) {
      if (!this.user.local || this.localRoleSwitching) return;
      this.localRoleSwitching = true;
      try {
        await requestResponse("/api/me/role", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role }),
        });
        if (this._timer) clearInterval(this._timer);
        this._timer = null;
        this.notifications = [];
        this.settings = null;
        this.statuses = Object.fromEntries(Object.keys(this.statuses).map((key) => [key, null]));
        await this.fetchUser();
        this.accountMenuOpen = false;
        if (this.user.capabilities.administer) {
          this.loadSettings();
          this.loadNotifications();
          this._timer = setInterval(() => this.loadNotifications(), 5000);
        }
        this.applyRoute();
      } catch (error) {
        this.notify(`Could not switch local role: ${error && error.message ? error.message : error}`);
      } finally {
        this.localRoleSwitching = false;
      }
    },

    logout() {
      if (this.user.authenticated) {
        window.location.href = "/.auth/logout?post_logout_redirect_uri=/";
      }
    },

    // ── Formatting ───────────────────────────────────────────
    fmt(value) {
      if (value === null || value === undefined || value === "") return "—";
      if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
      if (typeof value === "boolean") return value ? "Yes" : "No";
      return String(value);
    },

    coverageText(st) {
      if (!st) return "—";
      if (!st.coverageStart && !st.coverageEnd) return "—";
      return `${st.coverageStart || "?"} → ${st.coverageEnd || "?"}`;
    },

    stalenessText(st) {
      if (!st) return "—";
      if (st.stalenessMinutes !== undefined && st.stalenessMinutes !== null) {
        if (st.stalenessMinutes <= 0) return "Up to date";
        return `${st.stalenessMinutes} minute${st.stalenessMinutes === 1 ? "" : "s"} behind`;
      }
      if (st.stalenessDays === null) return "—";
      if (st.stalenessDays <= 0) return "Up to date";
      return `${st.stalenessDays} day${st.stalenessDays === 1 ? "" : "s"} behind`;
    },

    statusClass(status) {
      switch (status) {
        case "Completed": return "ok";
        case "Failed": case "Terminated": case "Canceled": return "err";
        case "Running": case "Pending": case "ContinuedAsNew": case "Suspended": return "running";
        default: return "muted";
      }
    },

    resourceTypeIcon,
    useGenericResourceIcon,

    timingText(n) {
      if (n.isRunning) {
        return `Started ${this.humanDuration(n.startedSinceSeconds)} ago`;
      }
      const took = n.durationSeconds !== null ? ` · took ${this.humanDuration(n.durationSeconds)}` : "";
      return `Completed ${this.formatDateTime(n.completedOn)}${took}`;
    },

    progressText(progress) {
      if (!progress) return "—";
      return `${progress.completedSubscriptions} / ${progress.totalSubscriptions} subscriptions (${progress.percentCompleted}%)`;
    },

    humanDuration(seconds) {
      if (seconds === null || seconds === undefined) return "—";
      const s = Math.max(0, Math.floor(seconds));
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = s % 60;
      if (h > 0) return `${h}h ${m}m`;
      if (m > 0) return `${m}m ${sec}s`;
      return `${sec}s`;
    },

    // Human-readable time relative to now. Past → "5 min ago", future → "in 5 min".
    // Accepts an ISO string, epoch ms, or Date. opts.short abbreviates "minute" → "min".
    relativeTimeText(input, opts = {}) {
      this._relativeTimeTick;
      if (input === null || input === undefined || input === "") return "";
      const ts = input instanceof Date ? input.getTime()
        : typeof input === "number" ? input
        : new Date(input).getTime();
      if (Number.isNaN(ts)) return "";
      const short = !!opts.short;
      const diffMs = ts - Date.now();
      const future = diffMs > 0;
      const absMin = Math.floor(Math.abs(diffMs) / 60000);
      let core;
      if (absMin < 1) {
        core = short ? "less than 1 min" : "less than 1 minute";
      } else if (absMin < 60) {
        const unit = short ? "min" : `minute${absMin === 1 ? "" : "s"}`;
        core = `${absMin} ${unit}`;
      } else {
        const hours = Math.floor(absMin / 60);
        if (hours < 48) {
          core = `${hours} hour${hours === 1 ? "" : "s"}`;
        } else {
          const days = Math.floor(hours / 24);
          core = `${days} day${days === 1 ? "" : "s"}`;
        }
      }
      return future ? `in ${core}` : `${core} ago`;
    },

    formatDateTime(iso) {
      if (!iso) return "—";
      try {
        return new Date(iso).toLocaleString();
      } catch (_) {
        return iso;
      }
    },
  };
  return applyFeatures(
    root,
    createNotificationsFeature(),
    createAdministrationFeature(),
    createDataCollectionFeature(),
    createPreferenceStoreFeature(),
    createScopeFeature(),
    createVmCoverageFeature(),
    createVmCoverageChartsFeature(),
    createCoverageEditorFeature(),
    createOdcrUsageFeature(),
  );
}
