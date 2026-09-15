"""Static, code-defined Activity Log collection recipes.

A recipe declares only *what* to collect and *what to keep* (Section 1 of
``docs/specs/activity-log-history.md``). It carries no behavior and imports
nothing outside the standard library, so it stays a pure, cheaply-importable
data table that the client (server-side filter derivation), projection, and
pipeline all read.

Collection tuning — lookback, overlap, ingestion lag — is global (Section 3),
never per-recipe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# Source-event status values that end an operation (Section 8 outcomes).
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "cancelled"})


@dataclass(frozen=True)
class ProjectedField:
    """A source path promoted to a typed, queryable column on the operation row.

    ``path`` is resolved against the event ``properties`` bag; embedded JSON
    strings (``responseBody``, ``statusMessage``) are parsed while descending.
    ``on_status`` restricts derivation to events whose status folds to that
    value (e.g. ``ErrorCode`` only on ``Failed``).
    """

    column: str
    path: str
    on_status: str | None = None

    def __post_init__(self) -> None:
        if not self.column:
            raise ValueError("ProjectedField.column must be non-empty")
        if not self.path:
            raise ValueError("ProjectedField.path must be non-empty")


@dataclass(frozen=True)
class Recipe:
    id: str
    version: int
    resource_types: tuple[str, ...] = ()
    operation_names: tuple[str, ...] = ()
    statuses: tuple[str, ...] | None = None
    projected_fields: tuple[ProjectedField, ...] = ()
    # Whether the full source event is retained for later re-projection; recipes
    # that only need the outcome (lifecycle actions) set this False to save storage.
    retain_payload: bool = True

    def __post_init__(self) -> None:
        if not _ID_PATTERN.match(self.id):
            raise ValueError(f"Invalid recipe id {self.id!r}")
        if self.version < 1:
            raise ValueError(f"Recipe {self.id!r} version must be >= 1")
        if not self.resource_types and not self.operation_names:
            raise ValueError(
                f"Recipe {self.id!r} must declare at least one of "
                "resource_types or operation_names"
            )
        columns = [field.column for field in self.projected_fields]
        if len(columns) != len(set(columns)):
            raise ValueError(f"Recipe {self.id!r} has duplicate projected columns")


_RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="vm-configuration",
        version=1,
        resource_types=("microsoft.compute/virtualmachines",),
        operation_names=(
            "microsoft.compute/virtualmachines/write",
            "microsoft.compute/virtualmachines/delete",
        ),
        projected_fields=(
            ProjectedField("VmSize", "responseBody.properties.hardwareProfile.vmSize"),
            ProjectedField("Location", "responseBody.location"),
            ProjectedField("Zones", "responseBody.zones"),
            ProjectedField("ProvisioningState", "responseBody.properties.provisioningState"),
            ProjectedField("CapacityReservationGroupId", "responseBody.properties.capacityReservation.capacityReservationGroup.id"),
            ProjectedField("ErrorCode", "statusMessage.error.code", on_status="Failed"),
        ),
    ),
    Recipe(
        id="vm-lifecycle",
        version=1,
        resource_types=("microsoft.compute/virtualmachines",),
        operation_names=(
            "microsoft.compute/virtualmachines/start/action",
            "microsoft.compute/virtualmachines/deallocate/action",
            "microsoft.compute/virtualmachines/reapply/action",
            "microsoft.compute/virtualmachines/redeploy/action",
        ),
        projected_fields=(
            ProjectedField("ErrorCode", "statusMessage.error.code", on_status="Failed"),
            ProjectedField("ErrorMessage", "statusMessage.error.message", on_status="Failed"),
        ),
        retain_payload=False,
    ),
    Recipe(
        id="vmss-configuration",
        version=1,
        resource_types=("microsoft.compute/virtualmachinescalesets",),
        operation_names=(
            "microsoft.compute/virtualmachinescalesets/write",
            "microsoft.compute/virtualmachinescalesets/delete",
        ),
        projected_fields=(
            ProjectedField("SkuName", "responseBody.sku.name"),
            ProjectedField("Capacity", "responseBody.sku.capacity"),
            ProjectedField("Location", "responseBody.location"),
            ProjectedField("Zones", "responseBody.zones"),
            ProjectedField("ProvisioningState", "responseBody.properties.provisioningState"),
            ProjectedField("ErrorCode", "statusMessage.error.code", on_status="Failed"),
        ),
    ),
    Recipe(
        id="crg-configuration",
        version=1,
        resource_types=("microsoft.compute/capacityreservationgroups",),
        operation_names=(
            "microsoft.compute/capacityreservationgroups/write",
            "microsoft.compute/capacityreservationgroups/delete",
        ),
        projected_fields=(
            ProjectedField("Location", "responseBody.location"),
            ProjectedField("Zones", "responseBody.zones"),
            ProjectedField("ProvisioningState", "responseBody.properties.provisioningState"),
            ProjectedField("ErrorCode", "statusMessage.error.code", on_status="Failed"),
        ),
    ),
    Recipe(
        id="cr-configuration",
        version=1,
        resource_types=("microsoft.compute/capacityreservationgroups/capacityreservations",),
        operation_names=(
            "microsoft.compute/capacityreservationgroups/capacityreservations/write",
            "microsoft.compute/capacityreservationgroups/capacityreservations/delete",
        ),
        projected_fields=(
            ProjectedField("SkuName", "responseBody.sku.name"),
            ProjectedField("Capacity", "responseBody.sku.capacity"),
            ProjectedField("Zones", "responseBody.zones"),
            ProjectedField("ProvisioningState", "responseBody.properties.provisioningState"),
            ProjectedField("ErrorCode", "statusMessage.error.code", on_status="Failed"),
        ),
    ),
    Recipe(
        id="subscription-registration",
        version=1,
        operation_names=("microsoft.management/register/action",),
        projected_fields=(),
    ),
)

_BY_ID: dict[str, Recipe] = {recipe.id: recipe for recipe in _RECIPES}


def all_recipes() -> tuple[Recipe, ...]:
    return _RECIPES


def ids() -> tuple[str, ...]:
    return tuple(recipe.id for recipe in _RECIPES)


def get(recipe_id: str) -> Recipe:
    return _BY_ID[recipe_id]


def try_get(recipe_id: str) -> Recipe | None:
    return _BY_ID.get(recipe_id)


def resource_provider_of(resource_type: str) -> str:
    """The provider namespace pushed as ``resourceProvider eq`` (Section 3)."""
    return resource_type.split("/", 1)[0]


def server_operations(recipes: tuple[Recipe, ...]) -> tuple[str, ...]:
    """Union of operation names for the server-side ``operations eq`` filter.

    Case is irrelevant server-side (the filter folds case), so names are lowered
    and de-duplicated. Empty means no recipe constrains operations, so the
    coarser ``resourceProvider eq`` push-down is used instead.
    """
    names: set[str] = set()
    for recipe in recipes:
        names.update(name.lower() for name in recipe.operation_names)
    return tuple(sorted(names))


def server_resource_providers(recipes: tuple[Recipe, ...]) -> tuple[str, ...]:
    """Provider namespaces for recipes that lack an operation filter."""
    providers: set[str] = set()
    for recipe in recipes:
        if recipe.operation_names:
            continue
        for resource_type in recipe.resource_types:
            providers.add(resource_provider_of(resource_type))
    return tuple(sorted(providers))


def event_matches(recipe: Recipe, resource_type: str | None, operation_name: str | None, status: str | None) -> bool:
    """Client-side re-filter for one event against one recipe (Section 3, step 5)."""
    if recipe.operation_names:
        if not operation_name:
            return False
        if operation_name.lower() not in {name.lower() for name in recipe.operation_names}:
            return False
    if recipe.resource_types:
        if not resource_type:
            return False
        if resource_type.lower() not in {value.lower() for value in recipe.resource_types}:
            return False
    if recipe.statuses is not None:
        if not status or status.lower() not in {value.lower() for value in recipe.statuses}:
            return False
    return True


def matching_recipes(resource_type: str | None, operation_name: str | None, status: str | None) -> tuple[Recipe, ...]:
    """All recipes an event satisfies (an event can feed more than one)."""
    return tuple(
        recipe
        for recipe in _RECIPES
        if event_matches(recipe, resource_type, operation_name, status)
    )
