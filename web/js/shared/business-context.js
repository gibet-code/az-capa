const BUSINESS_CONTEXT_NOT_MAPPED = "\u0000not-mapped";

function businessContextFields(metadata) {
  const context = metadata && metadata.businessContext;
  const fields = context && context.fields && typeof context.fields === "object"
    ? context.fields
    : context;
  return Object.entries(fields || {})
    .filter(([, name]) => typeof name === "string")
    .map(([key, name]) => ({ key, name }));
}

function businessContextFilterKey(fieldKey) {
  return `businessContext:${fieldKey}`;
}

function businessContextValue(row, fieldKey) {
  const value = row && row.businessContext && row.businessContext[fieldKey];
  return value === null || value === undefined || value === "" ? null : String(value);
}

function businessContextFilterValue(row, fieldKey) {
  return businessContextValue(row, fieldKey) ?? BUSINESS_CONTEXT_NOT_MAPPED;
}

function businessContextDisplay(row, fieldKey) {
  return businessContextValue(row, fieldKey) ?? "Not mapped";
}

function syncBusinessContextFilters(filters, metadata) {
  const previous = new Map(
    filters
      .filter((filter) => filter.businessContextKey)
      .map((filter) => [filter.businessContextKey, filter]),
  );
  const next = filters.filter((filter) => !filter.businessContextKey);
  const subscriptionIndex = next.findIndex((filter) => filter.key === "subscriptionId");
  const contextFilters = businessContextFields(metadata).map((field) => {
    const existing = previous.get(field.key);
    if (existing) {
      existing.label = field.name;
      return existing;
    }
    return {
      key: businessContextFilterKey(field.key),
      businessContextKey: field.key,
      label: field.name,
      clientOnly: true,
      searchable: true,
      showSelectAll: true,
      options: [],
      selected: [],
    };
  });
  next.splice(subscriptionIndex + 1, 0, ...contextFilters);
  return next;
}

function seedBusinessContextFilterOptions(filters, metadata, rows) {
  for (const field of businessContextFields(metadata)) {
    const values = new Map();
    let hasMissing = false;
    for (const row of rows) {
      const value = businessContextValue(row, field.key);
      if (value === null) {
        hasMissing = true;
        continue;
      }
      const normalized = value.toLocaleLowerCase();
      if (!values.has(normalized)) values.set(normalized, value);
    }
    const options = [...values.values()]
      .map((value) => ({ value, label: value }))
      .sort((left, right) => left.label.localeCompare(right.label));
    if (hasMissing) options.unshift({ value: BUSINESS_CONTEXT_NOT_MAPPED, label: "Not mapped" });
    setTableFilterOptions(filters, businessContextFilterKey(field.key), options);
  }
}

function matchesBusinessContextFilter(filter, row) {
  const value = businessContextFilterValue(row, filter.businessContextKey);
  if (value === BUSINESS_CONTEXT_NOT_MAPPED) return filter.selected.includes(value);
  return filter.selected.some((selected) =>
    selected !== BUSINESS_CONTEXT_NOT_MAPPED
    && String(selected).toLocaleLowerCase() === String(value).toLocaleLowerCase()
  );
}
