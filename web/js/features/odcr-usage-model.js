const ODCR_CHART_PALETTE = Object.freeze([
  "#4c9aff", "#39c5cf", "#3fb950", "#d29922", "#ff7b72",
  "#a371f7", "#ff8b5c", "#79c0ff", "#56d364", "#e3b341",
  "#1f6feb", "#2f9da6", "#238636", "#9e6a03", "#da3633",
  "#8250df", "#d65d0e", "#58a6ff", "#2ea043", "#bf8700",
  "#db61a2", "#8b949e", "#bc8cff", "#f0883e",
]);

function odcrUsageChartGroup(row, groupBy) {
  if (groupBy === "none") return { name: "Total" };
  if (groupBy.startsWith("businessContext:")) {
    const fieldKey = groupBy.slice("businessContext:".length);
    const value = businessContextValue(row, fieldKey);
    return {
      key: value === null ? BUSINESS_CONTEXT_NOT_MAPPED : `value:${value.toLocaleLowerCase()}`,
      name: value ?? "Not mapped",
      filterValue: value ?? BUSINESS_CONTEXT_NOT_MAPPED,
    };
  }
  if (groupBy === "capacityReservation") {
    const groupName = row.capacityReservationGroupName || "Ungrouped";
    return {
      name: `${groupName}/${row.name || "Unnamed reservation"}`,
      filterValue: row.name,
      parentFilterValue: row.capacityReservationGroupName,
    };
  }
  if (groupBy === "subscription") {
    return {
      name: row.subscriptionName && row.subscriptionId
        ? `${row.subscriptionName} (${row.subscriptionId})`
        : row.subscriptionName || row.subscriptionId || "Unknown subscription",
      filterValue: row.subscriptionId,
    };
  }
  if (groupBy === "resourceGroup") return { name: row.resourceGroup || "Unknown resource group", filterValue: row.resourceGroup };
  if (groupBy === "location") return { name: locationDisplay(row) || "Unknown location", filterValue: row.location };
  return {
    name: row.capacityReservationGroupName || "Ungrouped",
    filterValue: row.capacityReservationGroupName,
  };
}

function buildOdcrUsageChartData(rows, dates, metric, groupBy = "capacityReservationGroup") {
  const field = metric === "cost" ? "unusedCost" : "unusedHours";
  const groups = new Map();
  for (const row of rows) {
    const descriptor = odcrUsageChartGroup(row, groupBy);
    const groupKey = descriptor.key ?? descriptor.name;
    if (!groups.has(groupKey)) {
      groups.set(groupKey, { ...descriptor, values: Array(dates.length).fill(null), totalSpend: 0 });
    }
    const group = groups.get(groupKey);
    const values = Array.isArray(row[field]) ? row[field] : [];
    const costs = Array.isArray(row.unusedCost) ? row.unusedCost : [];
    for (let index = 0; index < dates.length; index += 1) {
      const rawValue = values[index];
      if (rawValue === null || rawValue === undefined || !Number.isFinite(Number(rawValue))) continue;
      group.values[index] = (group.values[index] ?? 0) + Number(rawValue);
    }
    for (const rawCost of costs) {
      if (rawCost === null || rawCost === undefined || !Number.isFinite(Number(rawCost))) continue;
      group.totalSpend += Number(rawCost);
    }
  }
  return [...groups.values()]
    .sort((left, right) => right.totalSpend - left.totalSpend || left.name.localeCompare(right.name));
}

function sumOdcrUsageValues(rows, field, emptyValues) {
  const values = [...emptyValues];
  for (const row of rows) {
    const source = Array.isArray(row[field]) ? row[field] : [];
    for (let index = 0; index < values.length; index += 1) {
      if (source[index] === null || source[index] === undefined || !Number.isFinite(Number(source[index]))) continue;
      values[index] = (values[index] ?? 0) + Number(source[index]);
    }
  }
  return values;
}

function rollingOdcrUsageTotal(values, days) {
  const window = values.slice(-days);
  if (window.length < days || window.some((value) => value === null || value === undefined)) return null;
  return window.reduce((total, value) => total + Number(value), 0);
}

function buildOdcrUsageModels(groups, reservations, metadata) {
  const groupsById = new Map(groups.map((group) => [String(group.id || "").toLowerCase(), group]));
  const reservationRows = reservations.map((reservation) => {
    const group = groupsById.get(String(reservation.capacityReservationGroupId || "").toLowerCase()) || {};
    return {
      ...reservation,
      subscriptionId: group.subscriptionId || reservation.subscriptionId,
      subscriptionName: group.subscriptionName || reservation.subscriptionName,
      resourceGroup: group.resourceGroup || reservation.resourceGroup,
      location: group.location || reservation.location,
      locationDisplayName: group.locationDisplayName || reservation.locationDisplayName,
      capacityReservationGroupName: group.name || reservation.capacityReservationGroupName,
    };
  });
  const reservationsByGroup = new Map();
  for (const reservation of reservationRows) {
    const groupId = String(reservation.capacityReservationGroupId || "").toLowerCase();
    const children = reservationsByGroup.get(groupId) || [];
    children.push(reservation);
    reservationsByGroup.set(groupId, children);
  }
  const usage = metadata?.usage || {};
  const dates = usage.dates || [];
  const emptyValues = dates.map((date) => !usage.isAvailable || (usage.coverageStart && date < usage.coverageStart) ? null : 0);
  const groupRows = groups.map((group) => {
    const children = reservationsByGroup.get(String(group.id || "").toLowerCase()) || [];
    const liveChildren = children.filter((reservation) => reservation.resourceExists !== false);
    const unusedHours = sumOdcrUsageValues(children, "unusedHours", emptyValues);
    const unusedCost = sumOdcrUsageValues(children, "unusedCost", emptyValues);
    return {
      ...group,
      capacityReservationGroupName: group.name,
      reservationCount: liveChildren.length,
      totalReservedQuantity: liveChildren.reduce((total, reservation) =>
        total + (Number.isFinite(Number(reservation.reservedQuantity)) ? Number(reservation.reservedQuantity) : 0), 0),
      unusedHours,
      unusedCost,
      lastDayUnusedHours: rollingOdcrUsageTotal(unusedHours, 1),
      lastDayUnusedCost: rollingOdcrUsageTotal(unusedCost, 1),
      last7DaysUnusedHours: rollingOdcrUsageTotal(unusedHours, 7),
      last7DaysUnusedCost: rollingOdcrUsageTotal(unusedCost, 7),
      last30DaysUnusedHours: rollingOdcrUsageTotal(unusedHours, 30),
      last30DaysUnusedCost: rollingOdcrUsageTotal(unusedCost, 30),
    };
  });
  return { reservationRows, groupRows };
}
