const COVERAGE_PREREQUISITE_STATUSES = Object.freeze(["Supported", "Unsupported", "Unknown"]);
const COVERAGE_REQUIRED_PRIORITIES = Object.freeze(["high", "medium", "low"]);
const COVERAGE_REQUIRED_CATEGORY_VALUES = Object.freeze([
  ...COVERAGE_REQUIRED_PRIORITIES.map((priority) => `required:${priority}`),
  "required:undefined",
]);

function coverageReportCategoryKey(row) {
  const coverage = row && row.odcrCoverage;
  if (!coverage || !coverage.decision) return "unmarked";
  if (coverage.decision !== "required") return coverage.decision;
  return COVERAGE_REQUIRED_PRIORITIES.includes(coverage.priority)
    ? `required:${coverage.priority}`
    : "required:undefined";
}

function coverageReportCategoryValues(unmarkedOwner = "required") {
  const required = [...COVERAGE_REQUIRED_CATEGORY_VALUES];
  const notRequired = ["not_required"];
  (unmarkedOwner === "not_required" ? notRequired : required).push("unmarked");
  return { required, notRequired };
}

function coverageReportBlockPopulation(rows, block, unmarkedOwner = "required") {
  const categories = coverageReportCategoryValues(unmarkedOwner);
  const ownedCategories = new Set(block === "not_required" ? categories.notRequired : categories.required);
  return rows.filter((row) => ownedCategories.has(coverageReportCategoryKey(row)));
}

function coverageReportAssociation(row) {
  return row && row.reservationStatus !== "Not associated" ? "assigned" : "not_associated";
}

function coverageReportRequirementStatus(row, requirementKey, metadata) {
  const explicit = (row && row.odcrRequirements || [])
    .find((result) => result.key === requirementKey);
  return explicit && explicit.status
    || metadata && metadata.odcrRequirements && metadata.odcrRequirements.defaultStatus
    || "Passed";
}

function coverageReportPercentage(numerator, denominator) {
  return denominator ? numerator * 100 / denominator : null;
}

function coverageReportCount(rows, predicate) {
  return rows.filter(predicate).length;
}

function coverageReportGroups(rows, groupBy, groupDescriptor) {
  if (groupBy === "none") {
    return [{ key: "total", label: "Total", missing: false, rows: [...rows] }];
  }
  const groups = new Map();
  for (const row of rows) {
    const descriptor = groupDescriptor(row, groupBy);
    const group = groups.get(descriptor.key) || { ...descriptor, rows: [] };
    group.rows.push(row);
    groups.set(descriptor.key, group);
  }
  return [...groups.values()].sort((left, right) => {
    if (left.missing !== right.missing) return left.missing ? 1 : -1;
    return left.label.localeCompare(right.label, undefined, { sensitivity: "base", numeric: true });
  });
}

function buildCoverageMarkingRows(groups) {
  return groups.map((group) => {
    const marked = coverageReportCount(group.rows, (row) =>
      ["required", "not_required"].includes(row && row.odcrCoverage && row.odcrCoverage.decision)
    );
    return {
      key: group.key,
      label: group.label,
      missing: group.missing,
      referenceCount: group.rows.length,
      markedReferenceCount: marked,
      markedPercent: coverageReportPercentage(marked, group.rows.length),
    };
  }).sort((left, right) =>
    (right.markedPercent ?? -1) - (left.markedPercent ?? -1)
      || left.label.localeCompare(right.label)
  );
}

function buildCoverageAssociationRows(groups) {
  return groups.map((group) => {
    const assigned = coverageReportCount(group.rows, (row) =>
      coverageReportAssociation(row) === "assigned"
    );
    return {
      key: group.key,
      label: group.label,
      missing: group.missing,
      referenceCount: group.rows.length,
      assignedReferenceCount: assigned,
      assignedPercent: coverageReportPercentage(assigned, group.rows.length),
      reviewReferenceCount: null,
    };
  });
}

function buildCoverageUnassociationRows(groups) {
  return groups.map((group) => {
    const unassociated = coverageReportCount(group.rows, (row) =>
      coverageReportAssociation(row) === "not_associated"
    );
    return {
      key: group.key,
      label: group.label,
      missing: group.missing,
      referenceCount: group.rows.length,
      unassociatedReferenceCount: unassociated,
      unassociatedPercent: coverageReportPercentage(unassociated, group.rows.length),
    };
  });
}

function buildCoverageDetails(rows) {
  const statuses = new Set(rows.map((row) => row.reservationStatus || "Unknown"));
  return [...statuses].map((status) => {
    const count = coverageReportCount(rows, (row) =>
      (row.reservationStatus || "Unknown") === status
    );
    return {
      key: status,
      label: status,
      referenceCount: count,
      percent: coverageReportPercentage(count, rows.length),
    };
  }).sort((left, right) =>
    right.referenceCount - left.referenceCount || left.label.localeCompare(right.label)
  );
}

function buildCoveragePrerequisiteRows(groups) {
  return groups.map((group) => {
    const statuses = {};
    for (const status of COVERAGE_PREREQUISITE_STATUSES) {
      const count = coverageReportCount(group.rows, (row) =>
        row.odcrSupportabilityStatus === status
      );
      statuses[status] = {
        referenceCount: count,
        percent: coverageReportPercentage(count, group.rows.length),
      };
    }
    return {
      key: group.key,
      label: group.label,
      missing: group.missing,
      referenceCount: group.rows.length,
      unsupportedOrUnknownCount:
        statuses.Unsupported.referenceCount + statuses.Unknown.referenceCount,
      fullySupported: statuses.Unsupported.referenceCount === 0 && statuses.Unknown.referenceCount === 0,
      statuses,
    };
  }).sort((left, right) =>
    right.statuses.Unsupported.referenceCount - left.statuses.Unsupported.referenceCount
      || right.statuses.Unknown.referenceCount - left.statuses.Unknown.referenceCount
      || left.label.localeCompare(right.label)
  );
}

function buildCoveragePrerequisiteIssues(rows, metadata) {
  const definitions = metadata && metadata.odcrRequirements && metadata.odcrRequirements.definitions || [];
  return definitions.map((definition) => {
    const count = coverageReportCount(rows, (row) =>
      ["Failed", "Unknown"].includes(coverageReportRequirementStatus(row, definition.key, metadata))
    );
    return {
      key: definition.key,
      label: definition.label || definition.key,
      referenceCount: count,
    };
  }).filter((row) => row.referenceCount > 0).sort((left, right) =>
    right.referenceCount - left.referenceCount || left.label.localeCompare(right.label)
  );
}

function buildCoverageReportViewModel({
  referenceRows = [],
  focusRows = referenceRows,
  visualPopulations = null,
  metadata = null,
  groupBy = "subscription",
  groupDescriptor,
}) {
  if (groupBy !== "none" && typeof groupDescriptor !== "function") {
    throw new Error("Coverage report grouping requires a group descriptor");
  }
  const populations = {
    marking: visualPopulations && visualPopulations.marking || referenceRows,
    coverage: visualPopulations && visualPopulations.coverage || referenceRows,
    unassociation: visualPopulations && visualPopulations.unassociation || referenceRows,
    coverageDetails: visualPopulations && visualPopulations.coverageDetails || referenceRows,
    prerequisites: visualPopulations && visualPopulations.prerequisites || referenceRows,
    prerequisiteIssues: visualPopulations && visualPopulations.prerequisiteIssues || referenceRows,
  };
  const markingGroups = coverageReportGroups(populations.marking, groupBy, groupDescriptor);
  const coverageGroups = coverageReportGroups(populations.coverage, groupBy, groupDescriptor);
  const unassociationGroups = coverageReportGroups(populations.unassociation, groupBy, groupDescriptor);
  const prerequisiteGroups = coverageReportGroups(populations.prerequisites, groupBy, groupDescriptor);
  return Object.freeze({
    referenceCount: referenceRows.length,
    focusedCount: focusRows.length,
    marking: Object.freeze({
      rows: buildCoverageMarkingRows(markingGroups),
      total: buildCoverageMarkingRows([{
        key: "total",
        label: "Total",
        missing: false,
        rows: populations.marking,
      }])[0],
      populationCount: populations.marking.length,
    }),
    coverage: Object.freeze({
      rows: buildCoverageAssociationRows(coverageGroups),
      total: buildCoverageAssociationRows([{
        key: "total",
        label: "Total",
        missing: false,
        rows: populations.coverage,
      }])[0],
      populationCount: populations.coverage.length,
    }),
    unassociation: Object.freeze({
      rows: buildCoverageUnassociationRows(unassociationGroups),
      total: buildCoverageUnassociationRows([{
        key: "total",
        label: "Total",
        missing: false,
        rows: populations.unassociation,
      }])[0],
      populationCount: populations.unassociation.length,
    }),
    coverageDetails: Object.freeze({
      rows: buildCoverageDetails(populations.coverageDetails),
      populationCount: populations.coverageDetails.length,
    }),
    prerequisites: Object.freeze({
      statuses: COVERAGE_PREREQUISITE_STATUSES,
      rows: buildCoveragePrerequisiteRows(prerequisiteGroups),
      total: buildCoveragePrerequisiteRows([
        { key: "total", label: "Total", missing: false, rows: populations.prerequisites },
      ])[0],
      populationCount: populations.prerequisites.length,
    }),
    prerequisiteIssues: Object.freeze({
      rows: buildCoveragePrerequisiteIssues(
        populations.prerequisiteIssues,
        metadata,
      ),
      populationCount: populations.prerequisiteIssues.length,
    }),
  });
}

function createCoverageReportFacetMatchers({ groupDescriptor, metadata }) {
  return {
    dimension: (row, value) => groupDescriptor(row).key === value,
    coverageCategory: (row, value) => coverageReportCategoryKey(row) === value,
    marking: (row, value) => {
      const marked = coverageReportCategoryKey(row) !== "unmarked";
      return value === "marked" ? marked : value === "unmarked" && !marked;
    },
    association: (row, value) => coverageReportAssociation(row) === value,
    reservationStatus: (row, value) => row.reservationStatus === value,
    prerequisiteStatus: (row, value) => row.odcrSupportabilityStatus === value,
    prerequisiteIssue: (row, value) => ["Failed", "Unknown"].includes(
      coverageReportRequirementStatus(row, value, metadata)
    ),
  };
}
