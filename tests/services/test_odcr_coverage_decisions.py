from __future__ import annotations

import unittest

from services.odcr_coverage_decisions import build_visibility_query, parse_decision_change


VM_1 = "/subscriptions/SUB-A/resourceGroups/RG/providers/Microsoft.Compute/virtualMachines/VM-1"


class OdcrCoverageDecisionsDomainTests(unittest.TestCase):
    def test_required_normalizes_values_and_deduplicates_ids(self):
        change = parse_decision_change({
            "resourceIds": [VM_1, VM_1.lower()],
            "decision": "REQUIRED",
            "priority": "HIGH",
            "note": "  business critical  ",
        })

        self.assertEqual((VM_1.lower(),), change.resource_ids)
        self.assertEqual("required", change.decision)
        self.assertEqual("high", change.priority)
        self.assertEqual("business critical", change.note)

    def test_required_needs_priority(self):
        with self.assertRaisesRegex(ValueError, "priority is required"):
            parse_decision_change({"resourceIds": [VM_1], "decision": "required"})

    def test_not_required_rejects_priority(self):
        with self.assertRaisesRegex(ValueError, "only valid"):
            parse_decision_change({"resourceIds": [VM_1], "decision": "not_required", "priority": "low"})

    def test_note_limit_and_clear_rules(self):
        self.assertEqual(500, len(parse_decision_change({
            "resourceIds": [VM_1], "decision": "not_required", "note": "x" * 500,
        }).note))
        with self.assertRaisesRegex(ValueError, "500"):
            parse_decision_change({"resourceIds": [VM_1], "decision": "not_required", "note": "x" * 501})
        with self.assertRaisesRegex(ValueError, "clearing"):
            parse_decision_change({"resourceIds": [VM_1], "decision": None, "note": "keep me"})

    def test_accepts_500_ids_and_rejects_501(self):
        resource_ids = [VM_1.replace("VM-1", f"VM-{index}") for index in range(500)]
        self.assertEqual(500, len(parse_decision_change({
            "resourceIds": resource_ids, "decision": "not_required",
        }).resource_ids))
        with self.assertRaisesRegex(ValueError, "more than 500"):
            parse_decision_change({
                "resourceIds": resource_ids + [VM_1.replace("VM-1", "VM-500")],
                "decision": "not_required",
            })

    def test_visibility_query_is_vm_scoped(self):
        query = build_visibility_query((VM_1.lower(),))
        self.assertIn("microsoft.compute/virtualmachines", query)
        self.assertIn(VM_1.lower(), query)
        self.assertIn("project id = tolower(id)", query)


if __name__ == "__main__":
    unittest.main()