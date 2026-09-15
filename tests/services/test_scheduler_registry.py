from __future__ import annotations

import unittest

from services.data_collection import registry
from services.data_collection.registry import PipelineDescriptor


class RegistryTests(unittest.TestCase):
    def test_all_pipelines_present(self):
        self.assertEqual(
            {
                "subscription_reference",
                "location_reference",
                "compute_skus",
                "subscription_context",
                "cr_usage",
                "vm_usage",
                "activity_log",
                "zone_mapping",
            },
            set(registry.ids()),
        )

    def test_all_descriptors_sorted_by_priority(self):
        priorities = [descriptor.priority for descriptor in registry.all_descriptors()]
        self.assertEqual(priorities, sorted(priorities))

    def test_subscription_reference_warms_first_and_cannot_disable(self):
        first = registry.all_descriptors()[0]
        self.assertEqual("subscription_reference", first.id)
        self.assertFalse(first.can_disable)

    def test_cost_management_pipelines_share_contention_group(self):
        self.assertEqual("cost_management", registry.get("cr_usage").contention_group)
        self.assertEqual("cost_management", registry.get("vm_usage").contention_group)

    def test_reference_pipelines_have_no_flush(self):
        for pipeline_id in ("subscription_reference", "location_reference"):
            descriptor = registry.get(pipeline_id)
            self.assertFalse(descriptor.supports_flush)
            self.assertIsNone(descriptor.flush_orchestrator)

    def test_zone_mapping_defaults_to_daily(self):
        descriptor = registry.get("zone_mapping")
        self.assertEqual(86400, descriptor.default_frequency_seconds)

    def test_get_unknown_raises_keyerror(self):
        with self.assertRaises(KeyError):
            registry.get("does_not_exist")

    def test_try_get_unknown_returns_none(self):
        self.assertIsNone(registry.try_get("does_not_exist"))

    def test_invalid_id_rejected(self):
        with self.assertRaises(ValueError):
            PipelineDescriptor(
                id="Bad-Id",
                label="x",
                refresh_orchestrator="o",
                flush_orchestrator=None,
                scope="x",
                contention_group=None,
                priority=1,
                default_frequency_seconds=0,
                default_anchor_seconds=0,
                default_enabled=True,
                supports_flush=False,
                can_disable=True,
            )

    def test_supports_flush_requires_flush_orchestrator(self):
        with self.assertRaises(ValueError):
            PipelineDescriptor(
                id="x",
                label="x",
                refresh_orchestrator="o",
                flush_orchestrator=None,
                scope="x",
                contention_group=None,
                priority=1,
                default_frequency_seconds=0,
                default_anchor_seconds=0,
                default_enabled=True,
                supports_flush=True,
                can_disable=True,
            )


if __name__ == "__main__":
    unittest.main()
