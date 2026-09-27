from __future__ import annotations

import unittest

from services.activity_log import recipes
from services.activity_log.recipes import ErrorProjection, ProjectedField, Recipe


class RecipeValidationTests(unittest.TestCase):
    def test_requires_resource_types_or_operation_names(self):
        with self.assertRaises(ValueError):
            Recipe(id="empty", version=1)

    def test_operation_names_only_is_valid(self):
        recipe = Recipe(id="ops-only", version=1, operation_names=("a/b/action",))
        self.assertEqual(("a/b/action",), recipe.operation_names)

    def test_resource_types_only_is_valid(self):
        recipe = Recipe(id="types-only", version=1, resource_types=("ns/type",))
        self.assertEqual(("ns/type",), recipe.resource_types)

    def test_invalid_id_rejected(self):
        with self.assertRaises(ValueError):
            Recipe(id="Bad_Id", version=1, operation_names=("a/b",))

    def test_version_must_be_positive(self):
        with self.assertRaises(ValueError):
            Recipe(id="v", version=0, operation_names=("a/b",))

    def test_duplicate_projected_columns_rejected(self):
        with self.assertRaises(ValueError):
            Recipe(
                id="dup",
                version=1,
                operation_names=("a/b",),
                projected_fields=(
                    ProjectedField("Same", "x"),
                    ProjectedField("Same", "y"),
                ),
            )

    def test_projected_field_requires_column_and_path(self):
        with self.assertRaises(ValueError):
            ProjectedField("", "x")
        with self.assertRaises(ValueError):
            ProjectedField("Col", "")
        with self.assertRaises(ValueError):
            ProjectedField("Col", "x", fallback_paths=("",))

    def test_error_projection_requires_path_and_status(self):
        with self.assertRaises(ValueError):
            ErrorProjection("", "Failed")
        with self.assertRaises(ValueError):
            ErrorProjection("statusMessage.error", "")


class RegistryTests(unittest.TestCase):
    def test_expected_recipes_present(self):
        self.assertEqual(
            {
                "vm-configuration",
                "vm-lifecycle",
                "crg-configuration",
                "cr-configuration",
            },
            set(recipes.ids()),
        )

    def test_unused_recipes_are_disabled(self):
        self.assertIsNone(recipes.try_get("vmss-configuration"))
        self.assertIsNone(recipes.try_get("subscription-registration"))

    def test_lifecycle_opts_out_of_payload_retention(self):
        self.assertFalse(recipes.get("vm-lifecycle").retain_payload)
        self.assertTrue(recipes.get("vm-configuration").retain_payload)

    def test_lifecycle_collects_restart_and_specific_failure_detail(self):
        recipe = recipes.get("vm-lifecycle")

        self.assertEqual(3, recipe.version)
        self.assertIn(
            "microsoft.compute/virtualmachines/restart/action",
            recipe.operation_names,
        )
        self.assertEqual("statusMessage.error", recipe.error_projection.path)
        self.assertEqual("Failed", recipe.error_projection.on_status)

    def test_vm_configuration_owns_fields_by_source_stage(self):
        recipe = recipes.get("vm-configuration")

        self.assertEqual(3, recipe.version)
        fields = {field.column: field for field in recipe.projected_fields}
        for column in (
            "VmSize", "Location", "Zones", "ProvisioningState",
            "CapacityReservationGroupId",
        ):
            self.assertEqual("Accepted", fields[column].on_status)
        self.assertNotIn("ErrorCode", fields)
        self.assertEqual("statusMessage.error", recipe.error_projection.path)
        self.assertEqual("Failed", recipe.error_projection.on_status)

    def test_vm_recipes_share_first_class_error_projection(self):
        configuration = recipes.get("vm-configuration")
        lifecycle = recipes.get("vm-lifecycle")

        self.assertIs(configuration.error_projection, lifecycle.error_projection)

    def test_get_unknown_raises(self):
        with self.assertRaises(KeyError):
            recipes.get("nope")

    def test_try_get_unknown_returns_none(self):
        self.assertIsNone(recipes.try_get("nope"))


class FilterDerivationTests(unittest.TestCase):
    def test_server_operations_unions_and_lowercases(self):
        chosen = (
            Recipe(id="a", version=1, operation_names=("Ns/Type/Write", "ns/type/delete")),
            Recipe(id="b", version=1, operation_names=("NS/TYPE/WRITE",)),
        )
        self.assertEqual(
            ("ns/type/delete", "ns/type/write"),
            recipes.server_operations(chosen),
        )

    def test_server_resource_providers_only_for_operationless_recipes(self):
        chosen = (
            Recipe(id="ops", version=1, operation_names=("ns/type/write",), resource_types=("ns/type",)),
            Recipe(id="types", version=1, resource_types=("other/kind",)),
        )
        self.assertEqual(("other",), recipes.server_resource_providers(chosen))

    def test_resource_provider_of(self):
        self.assertEqual(
            "microsoft.compute",
            recipes.resource_provider_of("microsoft.compute/virtualmachines"),
        )


class MatchingTests(unittest.TestCase):
    def test_operation_filter_is_case_insensitive(self):
        recipe = Recipe(id="m", version=1, operation_names=("ns/type/write",))
        self.assertTrue(recipes.event_matches(recipe, None, "NS/TYPE/WRITE", "Succeeded"))
        self.assertFalse(recipes.event_matches(recipe, None, "ns/type/delete", "Succeeded"))

    def test_resource_type_filter(self):
        recipe = Recipe(id="m", version=1, resource_types=("ns/type",))
        self.assertTrue(recipes.event_matches(recipe, "NS/Type", None, None))
        self.assertFalse(recipes.event_matches(recipe, "ns/other", None, None))

    def test_operation_filtered_recipe_rejects_missing_operation(self):
        recipe = Recipe(id="m", version=1, operation_names=("ns/type/write",))
        self.assertFalse(recipes.event_matches(recipe, None, None, "Succeeded"))

    def test_resource_typed_recipe_rejects_missing_resource_type(self):
        recipe = Recipe(id="m", version=1, resource_types=("ns/type",))
        self.assertFalse(recipes.event_matches(recipe, None, "ns/type/write", None))

    def test_status_filter_when_set(self):
        recipe = Recipe(id="m", version=1, operation_names=("a/b",), statuses=("Failed",))
        self.assertTrue(recipes.event_matches(recipe, None, "a/b", "failed"))
        self.assertFalse(recipes.event_matches(recipe, None, "a/b", "Succeeded"))

    def test_null_status_collects_all_stages(self):
        recipe = recipes.get("vm-configuration")
        self.assertTrue(
            recipes.event_matches(recipe, "microsoft.compute/virtualmachines",
                                  "microsoft.compute/virtualmachines/write", "Accepted")
        )

    def test_matching_recipes_returns_all_satisfied(self):
        matched = recipes.matching_recipes(
            "microsoft.compute/virtualmachines",
            "microsoft.compute/virtualmachines/write",
            "Succeeded",
        )
        self.assertIn("vm-configuration", {recipe.id for recipe in matched})


if __name__ == "__main__":
    unittest.main()
