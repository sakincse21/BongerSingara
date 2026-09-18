"""
Mathematical Optimizer — formulates and solves a Linear Program (LP) to
minimise total grid electricity cost over a 24-hour horizon.

Uses PuLP with the bundled CBC solver (no external solver install needed).

LP Variables (per hour h):
  grid[h]       ≥ 0           Grid electricity purchased.
  solar_used[h] ∈ [0, eff_solar[h]]  Solar energy used.
  charge[h]     ≥ 0           Battery charge amount.
  discharge[h]  ≥ 0           Battery discharge amount.
  energy[h]     ∈ [min, cap]  Battery energy *after* hour h.

Objective:
  minimise Σ grid[h] × tariff[h]

Key constraints:
  • Energy balance every hour.
  • Battery state transition.
  • End-of-day neutrality (final energy = initial energy).
  • Directive-specific bounds (solar_reduction, no_charge, no_discharge,
    minimum_battery_reserve, max_grid_window).

Note on charge/discharge mutual exclusivity:
  In a cost-minimising LP the solver will never simultaneously charge and
  discharge (it would waste money). A post-processing step handles any
  residual floating-point artifact.
"""

from __future__ import annotations

import logging
from typing import Any

import pulp

logger = logging.getLogger(__name__)

TOLERANCE = 1e-6  # floating-point zero threshold


# ──────────────────────── Public Interface ──────────────────────────────


def optimize_schedule(
    hours_data: list[dict[str, Any]],
    battery: dict[str, Any],
    directives: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Build and solve the LP.

    Returns a list of 24 hourly-plan dicts ready for the response schema.
    Raises RuntimeError if the LP is infeasible.
    """
    H = 24

    # ── 1. Pre-compute directive effects ─────────────────────────────────

    effective_solar = [hours_data[h]["solar_kwh"] for h in range(H)]
    no_charge_hours: set[int] = set()
    no_discharge_hours: set[int] = set()
    min_reserve: dict[int, float] = {}
    max_grid: dict[int, float] = {}

    for d in directives:
        if not d.get("applies", False) or d["directive_type"] == "no_op":
            continue
        adj = d["structured_adjustment"]
        dtype = d["directive_type"]

        if dtype == "solar_reduction":
            for hr in adj["hours"]:
                effective_solar[hr] = hours_data[hr]["solar_kwh"] * adj["factor"]

        elif dtype == "no_charge_window":
            no_charge_hours.update(adj["hours"])

        elif dtype == "no_discharge_window":
            no_discharge_hours.update(adj["hours"])

        elif dtype == "minimum_battery_reserve":
            for hr in adj["hours"]:
                existing = min_reserve.get(hr, battery["minimum_energy_kwh"])
                min_reserve[hr] = max(existing, adj["minimum_energy_kwh"])

        elif dtype == "max_grid_window":
            for hr in adj["hours"]:
                if hr in max_grid:
                    max_grid[hr] = min(max_grid[hr], adj["max_grid_kwh"])
                else:
                    max_grid[hr] = adj["max_grid_kwh"]

    # ── 2. Build LP ─────────────────────────────────────────────────────

    prob = pulp.LpProblem("GridWise_24h", pulp.LpMinimize)

    grid = [
        pulp.LpVariable(f"grid_{h}", lowBound=0, upBound=max_grid.get(h))
        for h in range(H)
    ]
    solar_used = [
        pulp.LpVariable(
            f"solar_{h}",
            lowBound=0,
            upBound=max(0.0, effective_solar[h]),
        )
        for h in range(H)
    ]
    charge = [
        pulp.LpVariable(
            f"charge_{h}",
            lowBound=0,
            upBound=(0.0 if h in no_charge_hours else battery["max_charge_kwh_per_hour"]),
        )
        for h in range(H)
    ]
    discharge = [
        pulp.LpVariable(
            f"discharge_{h}",
            lowBound=0,
            upBound=(0.0 if h in no_discharge_hours else battery["max_discharge_kwh_per_hour"]),
        )
        for h in range(H)
    ]

    base_min = battery["minimum_energy_kwh"]
    energy = [
        pulp.LpVariable(
            f"energy_{h}",
            lowBound=max(base_min, min_reserve.get(h, 0.0)),
            upBound=battery["capacity_kwh"],
        )
        for h in range(H)
    ]

    # Objective
    prob += (
        pulp.lpSum(grid[h] * hours_data[h]["tariff_bdt_per_kwh"] for h in range(H)),
        "TotalGridCost",
    )

    # Constraints
    for h in range(H):
        demand = hours_data[h]["demand_kwh"]

        # Energy balance: grid + solar + discharge = demand + charge
        prob += (
            grid[h] + solar_used[h] + discharge[h] == demand + charge[h],
            f"balance_{h}",
        )

        # Battery state transition
        prev = battery["initial_energy_kwh"] if h == 0 else energy[h - 1]
        prob += (
            energy[h] == prev + charge[h] - discharge[h],
            f"battery_transition_{h}",
        )

    # End-of-day neutrality
    prob += (
        energy[H - 1] == battery["initial_energy_kwh"],
        "end_of_day_neutrality",
    )

    # ── 3. Solve ─────────────────────────────────────────────────────────

    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=10)
    status = prob.solve(solver)

    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(
            f"LP solver returned '{pulp.LpStatus[status]}'. "
            "The scenario may be infeasible under the given directives."
        )

    # ── 4. Extract & post-process results ────────────────────────────────

    plan: list[dict[str, Any]] = []

    for h in range(H):
        c_val = max(0.0, charge[h].varValue or 0.0)
        d_val = max(0.0, discharge[h].varValue or 0.0)

        # Eliminate simultaneous charge+discharge (LP floating-point artifact)
        net = c_val - d_val
        if net > TOLERANCE:
            action = "charge"
            batt_kwh = round(net, 6)
        elif net < -TOLERANCE:
            action = "discharge"
            batt_kwh = round(abs(net), 6)
        else:
            action = "idle"
            batt_kwh = 0.0

        plan.append(
            {
                "hour": h,
                "grid_kwh": round(max(0.0, grid[h].varValue or 0.0), 6),
                "solar_used_kwh": round(max(0.0, solar_used[h].varValue or 0.0), 6),
                "battery_action": action,
                "battery_kwh": batt_kwh,
                "battery_energy_after_kwh": round(energy[h].varValue or 0.0, 6),
            }
        )

    return plan

