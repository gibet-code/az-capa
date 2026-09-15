function setTableFilterOptions(filters, key, options, reset = false) {
  const filter = filters.find((item) => item.key === key);
  const previouslyAll = filter.options.length === 0 || filter.selected.length === filter.options.length;
  const available = new Set(options.map((option) => option.value));
  filter.options = options;
  const retained = filter.selected.filter((value) => available.has(value));
  filter.selected = reset || previouslyAll || retained.length === 0
    ? options.map((option) => option.value)
    : retained;
}

function distinctTableOptions(rows, field) {
  return [...new Set(rows.map((row) => row[field]).filter(Boolean))]
    .sort((left, right) => left.localeCompare(right))
    .map((value) => ({ value, label: value }));
}

function subscriptionTableOptions(rows) {
  const subscriptions = new Map();
  for (const row of rows) {
    if (!row.subscriptionId) continue;
    subscriptions.set(
      row.subscriptionId,
      row.subscriptionName ? `${row.subscriptionName} (${row.subscriptionId})` : row.subscriptionId,
    );
  }
  return [...subscriptions.entries()]
    .map(([value, label]) => ({ value, label }))
    .sort((left, right) => left.label.localeCompare(right.label));
}

function seedResourceTableFilterOptions(filters, metadata, rows, resetSubscription = false) {
  setTableFilterOptions(filters, "subscriptionId", subscriptionTableOptions(rows), resetSubscription);
  seedBusinessContextFilterOptions(filters, metadata, rows);
  setTableFilterOptions(filters, "resourceGroup", distinctTableOptions(rows, "resourceGroup"));
  setTableFilterOptions(filters, "location", locationTableOptions(rows));
}

function paginateTableRows(rows, page, pageSize) {
  const start = (page - 1) * pageSize;
  return rows.slice(start, start + pageSize);
}

function tablePageStart(rowCount, page, pageSize) {
  return rowCount ? (page - 1) * pageSize + 1 : 0;
}

function tablePageEnd(rowCount, page, pageSize) {
  return Math.min(page * pageSize, rowCount);
}

function filterTableRows(rows, filters, matchesFilter) {
  return rows.filter((row) => filters.every((filter) => {
    if (!filter.options.length || filter.selected.length === filter.options.length) return true;
    return matchesFilter(row, filter);
  }));
}

function searchTableRows(rows, query, valuesFor) {
  const normalizedQuery = String(query || "").trim().toLocaleLowerCase();
  if (!normalizedQuery) return rows;
  return rows.filter((row) => valuesFor(row).some((value) =>
    String(value ?? "").toLocaleLowerCase().includes(normalizedQuery)
  ));
}

function nextTableSort(current, key) {
  if (current.key !== key) return { key, direction: "asc" };
  return { key, direction: current.direction === "asc" ? "desc" : "asc" };
}

function sortTableRows(rows, sort, valueFor) {
  if (!sort.key || !sort.direction) return rows;
  const direction = sort.direction === "asc" ? 1 : -1;
  return rows.sort((left, right) => String(valueFor(left, sort.key) ?? "").localeCompare(
    String(valueFor(right, sort.key) ?? ""),
    undefined,
    { numeric: true, sensitivity: "base" },
  ) * direction);
}

function multiSelectDropdown(config) {
  config.searchable = config.searchable === true;
  config.showSelectAll = config.showSelectAll !== false;
  return {
    config,
    open: false,
    search: "",
    draftSelected: [],

    get visibleOptions() {
      const query = this.search.trim().toLowerCase();
      if (!this.config.searchable || !query) return this.config.options;
      return this.config.options.filter((option) => option.label.toLowerCase().includes(query));
    },

    get allSelected() {
      return this.config.options.length > 0
        && this.draftSelected.length === this.config.options.length;
    },

    get someSelected() {
      return this.draftSelected.length > 0 && !this.allSelected;
    },

    get summary() {
      if (!this.config.options.length) return "No options";
      if (this.config.selected.length === this.config.options.length) return "all";
      if (this.config.selected.length === 1) {
        const selected = this.config.options.find((option) => option.value === this.config.selected[0]);
        return selected ? selected.label : this.config.selected[0];
      }
      return `${this.config.selected.length} selected`;
    },

    isSelected(value) {
      return this.draftSelected.includes(value);
    },

    toggleMenu() {
      if (this.open) {
        this.cancel();
        return;
      }
      this.draftSelected = [...this.config.selected];
      this.search = "";
      this.open = true;
    },

    toggleAll() {
      this.draftSelected = this.allSelected
        ? []
        : this.config.options.map((option) => option.value);
    },

    toggleOption(value, checked) {
      this.draftSelected = checked
        ? [...new Set([...this.draftSelected, value])]
        : this.draftSelected.filter((selected) => selected !== value);
    },

    apply() {
      this.config.selected = this.draftSelected.length
        ? [...this.draftSelected]
        : this.config.options.map((option) => option.value);
      this.open = false;
      this.$dispatch("multi-select-change", { key: this.config.key });
    },

    cancel() {
      this.draftSelected = [...this.config.selected];
      this.open = false;
    },
  };
}
