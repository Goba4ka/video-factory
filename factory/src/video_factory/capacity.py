"""Deterministic expected-yield and WIP planning for a daily video batch.

The planner is deliberately a capacity screen, not a throughput promise. It
uses expected attrition and explicit per-item review-time assumptions. Without
cycle-time inputs, render capacity means one render per slot in each of exactly
three planning waves. With a complete render cycle-time tuple, capacity is
derived from the stated window and utilization; neither mode is a throughput
guarantee.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Real
from typing import Any

from .errors import ValidationError


WAVE_COUNT = 3
DEFAULT_APPROVAL_GATE = "idea_review"


class CapacityPlanningError(ValidationError):
    """Raised when capacity-planner inputs are internally inconsistent."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_nonnegative_int(value: Any, field: str) -> int:
    if not _is_int(value) or value < 0:
        raise CapacityPlanningError(f"{field} must be a non-negative integer")
    return value


def _validate_positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or value <= 0:
        raise CapacityPlanningError(f"{field} must be a positive number")
    return float(value)


def _validate_pods(
    target: int,
    pod_targets: Mapping[str, int],
    pod_capacities: Mapping[str, int],
) -> tuple[dict[str, int], dict[str, int]]:
    if not isinstance(pod_targets, Mapping) or not pod_targets:
        raise CapacityPlanningError("pod_targets must be a non-empty mapping")
    if not isinstance(pod_capacities, Mapping):
        raise CapacityPlanningError("pod_capacities must be a mapping")

    targets: dict[str, int] = {}
    capacities: dict[str, int] = {}
    for raw_name in sorted(pod_targets):
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise CapacityPlanningError("pod names must be non-empty strings")
        name = raw_name.strip()
        if name != raw_name:
            raise CapacityPlanningError(f"pod name {raw_name!r} must not contain outer spaces")
        targets[name] = _validate_nonnegative_int(
            pod_targets[raw_name], f"pod_targets[{name!r}]"
        )
        if name not in pod_capacities:
            raise CapacityPlanningError(f"pod_capacities is missing pod {name!r}")
        capacities[name] = _validate_nonnegative_int(
            pod_capacities[name], f"pod_capacities[{name!r}]"
        )

    unknown_capacities = sorted(set(pod_capacities).difference(targets))
    if unknown_capacities:
        raise CapacityPlanningError(
            "pod_capacities contains pods without targets: "
            + ", ".join(repr(name) for name in unknown_capacities)
        )
    if sum(targets.values()) != target:
        raise CapacityPlanningError(
            f"pod target sum must equal target ({sum(targets.values())} != {target})"
        )
    return targets, capacities


def _validate_attrition(expected_attrition: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(expected_attrition, Mapping):
        raise CapacityPlanningError("expected_attrition must be a mapping")
    validated: dict[str, float] = {}
    for raw_gate in sorted(expected_attrition):
        if not isinstance(raw_gate, str) or not raw_gate.strip():
            raise CapacityPlanningError("attrition gate names must be non-empty strings")
        gate = raw_gate.strip()
        if gate != raw_gate:
            raise CapacityPlanningError(
                f"attrition gate {raw_gate!r} must not contain outer spaces"
            )
        value = expected_attrition[raw_gate]
        if isinstance(value, bool) or not isinstance(value, Real):
            raise CapacityPlanningError(
                f"expected_attrition[{gate!r}] must be a number from 0 through less than 1"
            )
        attrition = float(value)
        if not 0 <= attrition < 1:
            raise CapacityPlanningError(
                f"expected_attrition[{gate!r}] must be from 0 through less than 1"
            )
        validated[gate] = attrition
    return validated


def _apportion(total: int, weights: Mapping[str, int]) -> dict[str, int]:
    """Largest-remainder apportionment with lexical tie-breaking."""

    if total < 0:
        raise CapacityPlanningError("apportionment total cannot be negative")
    ordered = sorted(weights)
    weight_sum = sum(weights.values())
    if weight_sum == 0:
        if total == 0:
            return {name: 0 for name in ordered}
        raise CapacityPlanningError("cannot apportion a positive total over zero weights")

    exact = {name: total * weights[name] / weight_sum for name in ordered}
    allocation = {name: math.floor(exact[name]) for name in ordered}
    remainder = total - sum(allocation.values())
    ranking = sorted(ordered, key=lambda name: (-(exact[name] - allocation[name]), name))
    for name in ranking[:remainder]:
        allocation[name] += 1
    return allocation


def _wave_targets(target: int) -> list[int]:
    base, remainder = divmod(target, WAVE_COUNT)
    return [base + (1 if index < remainder else 0) for index in range(WAVE_COUNT)]


def _pod_wave_matrix(
    pod_targets: Mapping[str, int], wave_targets: list[int]
) -> dict[str, list[int]]:
    """Create exact row/column sums with deterministic max-remaining placement."""

    remaining = list(wave_targets)
    matrix = {pod: [0] * WAVE_COUNT for pod in sorted(pod_targets)}
    for pod in sorted(pod_targets):
        for _ in range(pod_targets[pod]):
            candidates = [index for index, slots in enumerate(remaining) if slots > 0]
            if not candidates:
                raise CapacityPlanningError("internal wave allocation exhausted too early")
            wave = min(candidates, key=lambda index: (-remaining[index], index))
            matrix[pod][wave] += 1
            remaining[wave] -= 1
    if any(remaining):
        raise CapacityPlanningError("internal wave allocation did not conserve target")
    return matrix


def _required_counts(
    target: int,
    attrition: Mapping[str, float],
    approval_gate: str,
) -> tuple[int, int, float, float]:
    approval_survival = 1.0 - attrition.get(approval_gate, 0.0)
    downstream_survival = math.prod(
        1.0 - rate for gate, rate in attrition.items() if gate != approval_gate
    )
    required_approvals = math.ceil(target / downstream_survival)
    required_candidates = math.ceil(required_approvals / approval_survival)
    return (
        required_candidates,
        required_approvals,
        approval_survival,
        downstream_survival,
    )


def plan_daily_batch(
    *,
    target: int,
    pod_targets: Mapping[str, int],
    pod_capacities: Mapping[str, int],
    expected_attrition: Mapping[str, float],
    render_slots: int,
    human_review_minutes: int,
    minutes_per_candidate_review: float = 3.0,
    minutes_per_final_review: float = 2.0,
    approval_gate: str = DEFAULT_APPROVAL_GATE,
    render_minutes_per_output: float | None = None,
    render_window_minutes: float | None = None,
    render_utilization: float | None = None,
) -> dict[str, Any]:
    """Plan one target batch across pods and exactly three WIP waves.

    ``pod_capacities`` are maximum final outputs per pod for this batch window.
    They are not candidate-pool sizes. ``render_slots`` are concurrent slots.
    By default the conservative model assumes each slot completes one render in
    each of three waves. To use measured cycle-time capacity, callers must
    provide all of ``render_minutes_per_output``, ``render_window_minutes``, and
    ``render_utilization``. Human minutes cover every required candidate review
    plus every target final review. Attrition values and cycle-time inputs are
    planning assumptions, not guaranteed outcomes.
    """

    if not _is_int(target) or not 1 <= target <= 15:
        raise CapacityPlanningError("target must be an integer from 1 to 15")
    targets, capacities = _validate_pods(target, pod_targets, pod_capacities)
    attrition = _validate_attrition(expected_attrition)
    render_slots = _validate_nonnegative_int(render_slots, "render_slots")
    human_review_minutes = _validate_nonnegative_int(
        human_review_minutes, "human_review_minutes"
    )
    candidate_review_minutes = _validate_positive_number(
        minutes_per_candidate_review, "minutes_per_candidate_review"
    )
    final_review_minutes = _validate_positive_number(
        minutes_per_final_review, "minutes_per_final_review"
    )
    cycle_inputs = (
        render_minutes_per_output,
        render_window_minutes,
        render_utilization,
    )
    if any(value is not None for value in cycle_inputs) and not all(
        value is not None for value in cycle_inputs
    ):
        raise CapacityPlanningError(
            "render_minutes_per_output, render_window_minutes, and "
            "render_utilization must be provided together"
        )
    cycle_time_modeled = all(value is not None for value in cycle_inputs)
    if cycle_time_modeled:
        render_minutes = _validate_positive_number(
            render_minutes_per_output, "render_minutes_per_output"
        )
        render_window = _validate_positive_number(
            render_window_minutes, "render_window_minutes"
        )
        render_use = _validate_positive_number(
            render_utilization, "render_utilization"
        )
        if render_use > 1:
            raise CapacityPlanningError("render_utilization must be at most 1")
    else:
        render_minutes = None
        render_window = None
        render_use = None
    approval_gate = (
        approval_gate.strip()
        if isinstance(approval_gate, str) and approval_gate.strip()
        else ""
    )
    if not approval_gate:
        raise CapacityPlanningError("approval_gate must be a non-empty string")

    (
        required_candidates,
        required_approvals,
        approval_survival,
        downstream_survival,
    ) = _required_counts(target, attrition, approval_gate)

    pod_candidates = _apportion(required_candidates, targets)
    pod_approvals = _apportion(required_approvals, targets)
    wave_output_targets = _wave_targets(target)
    wave_candidates = _apportion(
        required_candidates,
        {str(index): amount for index, amount in enumerate(wave_output_targets)},
    )
    wave_approvals = _apportion(
        required_approvals,
        {str(index): amount for index, amount in enumerate(wave_output_targets)},
    )
    pod_wave = _pod_wave_matrix(targets, wave_output_targets)

    pod_allocation = []
    warnings: list[dict[str, Any]] = []
    for pod in sorted(targets):
        shortfall = max(0, targets[pod] - capacities[pod])
        if shortfall:
            warnings.append(
                {
                    "code": "pod_capacity_shortfall",
                    "severity": "blocking",
                    "pod": pod,
                    "required": targets[pod],
                    "available": capacities[pod],
                    "shortfall": shortfall,
                }
            )
        pod_allocation.append(
            {
                "pod": pod,
                "target_outputs": targets[pod],
                "capacity_outputs": capacities[pod],
                "required_candidates": pod_candidates[pod],
                "required_approvals": pod_approvals[pod],
                "wave_outputs": pod_wave[pod],
            }
        )

    if cycle_time_modeled:
        render_capacity = math.floor(
            render_slots * render_window * render_use / render_minutes
        )
        render_capacity_mode = "cycle_time_window"
        render_shortfall_code = "render_throughput_shortfall"
        render_shortfall_unit = "renders_in_window"
        wave_render_capacity = _wave_targets(render_capacity)
    else:
        render_capacity = render_slots * WAVE_COUNT
        render_capacity_mode = "conservative_wave_slots"
        render_shortfall_code = "render_slot_shortfall"
        render_shortfall_unit = "renders_across_three_waves"
        wave_render_capacity = [render_slots] * WAVE_COUNT
    if render_capacity < target:
        warnings.append(
            {
                "code": render_shortfall_code,
                "severity": "blocking",
                "required": target,
                "available": render_capacity,
                "shortfall": target - render_capacity,
                "unit": render_shortfall_unit,
            }
        )

    required_human_minutes = math.ceil(
        required_candidates * candidate_review_minutes + target * final_review_minutes
    )
    if required_human_minutes > human_review_minutes:
        warnings.append(
            {
                "code": "human_review_shortfall",
                "severity": "blocking",
                "required": required_human_minutes,
                "available": human_review_minutes,
                "shortfall": required_human_minutes - human_review_minutes,
                "unit": "minutes",
            }
        )

    if required_candidates > target * 2:
        warnings.append(
            {
                "code": "high_expected_attrition",
                "severity": "warning",
                "required_candidates": required_candidates,
                "target": target,
                "ratio": round(required_candidates / target, 4),
            }
        )

    waves = []
    for index, output_target in enumerate(wave_output_targets):
        waves.append(
            {
                "wave": index + 1,
                "target_outputs": output_target,
                "required_candidates": wave_candidates[str(index)],
                "required_approvals": wave_approvals[str(index)],
                "render_slots_required": output_target,
                "render_slots_available": render_slots,
                "render_capacity_outputs_available": wave_render_capacity[index],
                "pod_outputs": {
                    pod: pod_wave[pod][index] for pod in sorted(pod_wave)
                },
            }
        )

    blocking = [warning for warning in warnings if warning["severity"] == "blocking"]
    gate_details = [
        {
            "gate": gate,
            "expected_attrition": attrition[gate],
            "expected_survival": round(1.0 - attrition[gate], 8),
            "phase": "approval" if gate == approval_gate else "post_approval",
        }
        for gate in sorted(attrition)
    ]
    return {
        "target_outputs": target,
        "required_candidates": required_candidates,
        "required_approvals": required_approvals,
        "pod_allocation": pod_allocation,
        "waves": waves,
        "resources": {
            "render_capacity_mode": render_capacity_mode,
            "render_slots_per_wave": render_slots,
            "render_capacity_across_three_waves": render_capacity,
            "render_minutes_per_output": render_minutes,
            "render_window_minutes": render_window,
            "render_utilization": render_use,
            "human_review_minutes_available": human_review_minutes,
            "human_review_minutes_required": required_human_minutes,
        },
        "expected_yield_model": {
            "approval_gate": approval_gate,
            "gates": gate_details,
            "approval_survival_product": round(approval_survival, 8),
            "post_approval_survival_product": round(downstream_survival, 8),
            "formulas": {
                "required_approvals": (
                    "ceil(target_outputs / post_approval_survival_product)"
                ),
                "required_candidates": (
                    "ceil(required_approvals / approval_survival_product)"
                ),
                "human_review_minutes_required": (
                    "ceil(required_candidates * minutes_per_candidate_review + "
                    "target_outputs * minutes_per_final_review)"
                ),
                "render_capacity_across_three_waves": (
                    "floor(render_slots_per_wave * render_window_minutes * "
                    "render_utilization / render_minutes_per_output)"
                    if cycle_time_modeled
                    else "render_slots_per_wave * 3"
                ),
            },
        },
        "assumptions": {
            "planning_model": (
                "expected_yield_cycle_time_capacity_screen_not_throughput_guarantee"
                if cycle_time_modeled
                else "expected_yield_capacity_screen_not_throughput_guarantee"
            ),
            "wave_count": WAVE_COUNT,
            "renders_per_slot_per_wave": 1,
            "minutes_per_candidate_review": candidate_review_minutes,
            "minutes_per_final_review": final_review_minutes,
            "pod_capacity_unit": "maximum_final_outputs_in_batch_window",
            "cycle_time_modeled": cycle_time_modeled,
        },
        "bottleneck_warnings": warnings,
        "feasible": not blocking,
    }


__all__ = ["CapacityPlanningError", "plan_daily_batch"]
