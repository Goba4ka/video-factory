from __future__ import annotations

import sys
import unittest
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.capacity import (  # noqa: E402
    CapacityPlanningError,
    plan_daily_batch,
)


ATTRITION = {
    "idea_review": 0.20,
    "rights": 0.10,
    "production": 0.05,
    "qc": 0.10,
}


class CapacityPlannerTestCase(unittest.TestCase):
    def plan(self, target: int, **overrides):
        if target == 10:
            pod_targets = {
                "nature_animals": 4,
                "people_culture": 3,
                "space_technology": 3,
            }
        else:
            pod_targets = {
                "nature_animals": 5,
                "people_culture": 5,
                "space_technology": 5,
            }
        arguments = {
            "target": target,
            "pod_targets": pod_targets,
            "pod_capacities": {pod: 6 for pod in pod_targets},
            "expected_attrition": ATTRITION,
            "render_slots": 5,
            "human_review_minutes": 240,
            "minutes_per_candidate_review": 3,
            "minutes_per_final_review": 2,
        }
        arguments.update(overrides)
        return plan_daily_batch(**arguments)

    def test_target_10_is_split_into_three_deterministic_waves(self) -> None:
        plan = self.plan(10)
        self.assertTrue(plan["feasible"])
        self.assertEqual([wave["target_outputs"] for wave in plan["waves"]], [4, 3, 3])
        self.assertEqual(plan, self.plan(10))
        self.assertIn("ceil", plan["expected_yield_model"]["formulas"]["required_candidates"])
        self.assertFalse(plan["assumptions"]["cycle_time_modeled"])

    def test_target_15_uses_five_outputs_per_wave(self) -> None:
        plan = self.plan(15)
        self.assertTrue(plan["feasible"])
        self.assertEqual([wave["target_outputs"] for wave in plan["waves"]], [5, 5, 5])
        self.assertEqual(plan["resources"]["render_capacity_across_three_waves"], 15)
        self.assertGreater(plan["required_approvals"], 15)
        self.assertGreaterEqual(plan["required_candidates"], plan["required_approvals"])

    def test_invalid_inputs_are_rejected(self) -> None:
        for target in (0, 16, True):
            with self.subTest(target=target), self.assertRaises(CapacityPlanningError):
                self.plan(target)
        with self.assertRaises(CapacityPlanningError):
            self.plan(
                10,
                pod_targets={"nature_animals": 9},
                pod_capacities={"nature_animals": 9},
            )
        with self.assertRaises(CapacityPlanningError):
            self.plan(10, expected_attrition={"idea_review": 1.0})
        with self.assertRaises(CapacityPlanningError):
            self.plan(10, render_minutes_per_output=8)
        with self.assertRaises(CapacityPlanningError):
            self.plan(
                10,
                render_minutes_per_output=8,
                render_window_minutes=240,
                render_utilization=1.01,
            )

    def test_complete_cycle_time_tuple_models_one_slot_capacity(self) -> None:
        plan = self.plan(
            15,
            render_slots=1,
            render_minutes_per_output=8,
            render_window_minutes=360,
            render_utilization=0.5,
        )
        self.assertTrue(plan["feasible"])
        self.assertTrue(plan["assumptions"]["cycle_time_modeled"])
        self.assertEqual(plan["resources"]["render_capacity_mode"], "cycle_time_window")
        self.assertEqual(plan["resources"]["render_capacity_across_three_waves"], 22)
        self.assertEqual(
            [wave["render_capacity_outputs_available"] for wave in plan["waves"]],
            [8, 7, 7],
        )
        self.assertIn(
            "render_window_minutes",
            plan["expected_yield_model"]["formulas"][
                "render_capacity_across_three_waves"
            ],
        )

    def test_cycle_time_shortfall_is_distinct_from_legacy_slot_shortfall(self) -> None:
        plan = self.plan(
            15,
            render_slots=1,
            render_minutes_per_output=10,
            render_window_minutes=60,
            render_utilization=0.5,
        )
        self.assertFalse(plan["feasible"])
        warning = next(
            item
            for item in plan["bottleneck_warnings"]
            if item["code"] == "render_throughput_shortfall"
        )
        self.assertEqual(warning["available"], 3)
        self.assertEqual(warning["unit"], "renders_in_window")

    def test_insufficient_resources_are_reported_not_hidden(self) -> None:
        pod_targets = {
            "nature_animals": 5,
            "people_culture": 5,
            "space_technology": 5,
        }
        plan = self.plan(
            15,
            pod_capacities={
                "nature_animals": 4,
                "people_culture": 5,
                "space_technology": 5,
            },
            render_slots=2,
            human_review_minutes=10,
            pod_targets=pod_targets,
        )
        self.assertFalse(plan["feasible"])
        codes = {warning["code"] for warning in plan["bottleneck_warnings"]}
        self.assertIn("pod_capacity_shortfall", codes)
        self.assertIn("render_slot_shortfall", codes)
        self.assertIn("human_review_shortfall", codes)

    def test_all_allocations_conserve_totals(self) -> None:
        for target in (10, 15):
            with self.subTest(target=target):
                plan = self.plan(target)
                pods = plan["pod_allocation"]
                waves = plan["waves"]
                self.assertEqual(sum(item["target_outputs"] for item in pods), target)
                self.assertEqual(
                    sum(item["required_candidates"] for item in pods),
                    plan["required_candidates"],
                )
                self.assertEqual(
                    sum(item["required_approvals"] for item in pods),
                    plan["required_approvals"],
                )
                self.assertEqual(sum(wave["target_outputs"] for wave in waves), target)
                self.assertEqual(
                    sum(wave["required_candidates"] for wave in waves),
                    plan["required_candidates"],
                )
                self.assertEqual(
                    sum(wave["required_approvals"] for wave in waves),
                    plan["required_approvals"],
                )
                for wave in waves:
                    self.assertEqual(
                        sum(wave["pod_outputs"].values()), wave["target_outputs"]
                    )
                for pod in pods:
                    self.assertEqual(sum(pod["wave_outputs"]), pod["target_outputs"])


if __name__ == "__main__":
    unittest.main()
