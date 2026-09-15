const GENERIC_RESOURCE_ICON = "/icons/generic-resource.svg";
const RESOURCE_TYPE_ICONS = Object.freeze({
  "microsoft.compute/virtualmachines": "/icons/virtual-machine.svg",
});

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  })[character]);
}

function resourceTypeIcon(resourceType) {
  const normalizedType = (resourceType || "").trim().toLowerCase();
  return RESOURCE_TYPE_ICONS[normalizedType] || GENERIC_RESOURCE_ICON;
}

function useGenericResourceIcon(event) {
  const image = event.currentTarget;
  if (!image.src.endsWith(GENERIC_RESOURCE_ICON)) image.src = GENERIC_RESOURCE_ICON;
}

function locationDisplay(row) {
  return row && (row.locationDisplayName || row.location) || "";
}

function locationTableOptions(rows) {
  const locations = new Map();
  for (const row of rows) {
    if (!row.location) continue;
    locations.set(row.location, locationDisplay(row));
  }
  return [...locations.entries()]
    .map(([value, label]) => ({ value, label }))
    .sort((left, right) => left.label.localeCompare(right.label));
}

function resourceDeploymentFilterValue(row, mode) {
  if (!row || !row.logicalZone) return "Regional";
  const zone = mode === "physical" ? row.physicalZone : row.logicalZone;
  return zone ? `Zone ${zone}` : "Unresolved";
}

function resourceDeploymentDisplay(row, mode) {
  const value = resourceDeploymentFilterValue(row, mode);
  if (value === "Regional" || value === "Unresolved") return value;
  return `${mode === "physical" ? "Phy" : "Log"} ${value}`;
}
