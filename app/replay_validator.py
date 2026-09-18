"""
Replay Validator — independently re-simulates the 24-hour schedule produced
by the optimizer, verifying every constraint the judge checks:

  • Energy balance each hour.
  • Battery state transitions.
  • Battery capacity / minimum-energy bounds.
  • Charge / discharge rate limits.
  • Directive compliance (solar_reduction, no_charge, no_discharge,
    minimum_battery_reserve, max_grid_window).
  • End-of-day battery neutrality.
  • Recalculated totals (total_grid_kwh, total_cost_bdt, peak_grid_kwh).

This is the "Final Validator" box from the §03 processing flow diagram.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ABS_TOL = 0.01  # Problem Statement §11.5


# ──────────────────────── Public Interface ──────────────────────────────


def replay_and_compute_totals(
    plan: list[dict[str, Any]],
    hours_data: list[dict[str, Any]],
    battery: dict[str, Any],
    directives: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Replay *plan* hour-by-hour and return computed totals + any errors.

    Returns:
      {
        "total_grid_kwh": float,
        "total_cost_bdt": float,
        "peak_grid_kwh": float,
        "errors": list[str],       # empty when schedule is valid
      }
    """
    H = 24
    errors: list[str] = []

    # ── 1. Recompute directive effects (independently of optimizer) ───────

    effective_solar = [hours_data[h]["solar_kwh"] for h in range(H)]
    no_charge_hours: set[int] = set()
    no_discharge_hours: set[int] = set()
    min_reserve: dict[int, float] = {}
    max_grid_limits: dict[int, float] = {}

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
                if hr in max_grid_limits:
                    max_grid_limits[hr] = min(max_grid_limits[hr], adj["max_grid_kwh"])
                else:
                    max_grid_limits[hr] = adj["max_grid_kwh"]

    # ── 2. Replay hour-by-hour ───────────────────────────────────────────

    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0
    prev_energy = battery["initial_energy_kwh"]

    for entry in sorted(plan, key=lambda e: e["hour"]):
        h: int = entry["hour"]
        g: float = entry["grid_kwh"]
        s: float = entry["solar_used_kwh"]
        action: str = entry["battery_action"]
        bkwh: float = entry["battery_kwh"]
        e_after: float = entry["battery_energy_after_kwh"]
        demand: float = hours_data[h]["demand_kwh"]
        tariff: float = hours_data[h]["tariff_bdt_per_kwh"]
        eff_solar: float = effective_solar[h]

        # --- Non-negative values ---
        if g < -ABS_TOL or s < -ABS_TOL or bkwh < -ABS_TOL:
            errors.append(f"Hour {h}: negative energy value (g={g}, s={s}, b={bkwh})")

        # --- Solar usage ≤ effective solar ---
        if s > eff_solar + ABS_TOL:
            errors.append(
                f"Hour {h}: solar_used {s:.4f} > effective_solar {eff_solar:.4f}"
            )

        # --- Energy balance ---
        if action == "charge":
            balance = g + s - demand - bkwh
        elif action == "discharge":
            balance = g + s + bkwh - demand
        else:  # idle
            balance = g + s - demand

        if abs(balance) > ABS_TOL:
            errors.append(f"Hour {h}: energy balance off by {balance:.4f}")

        # --- Battery state transition ---
        if action == "charge":
            expected_e = prev_energy + bkwh
        elif action == "discharge":
            expected_e = prev_energy - bkwh
        else:
            expected_e = prev_energy

        if abs(expected_e - e_after) > ABS_TOL:
            errors.append(
                f"Hour {h}: battery state expected {expected_e:.4f}, got {e_after:.4f}"
            )

        # --- Battery capacity bounds ---
        effective_min = max(battery["minimum_energy_kwh"], min_reserve.get(h, 0.0))
        if e_after < effective_min - ABS_TOL:
            errors.append(
                f"Hour {h}: battery {e_after:.4f} < minimum {effective_min:.4f}"
            )
        if e_after > battery["capacity_kwh"] + ABS_TOL:
            errors.append(
                f"Hour {h}: battery {e_after:.4f} > capacity {battery['capacity_kwh']}"
            )

        # --- Charge / discharge rate limits ---
        if action == "charge" and bkwh > battery["max_charge_kwh_per_hour"] + ABS_TOL:
            errors.append(f"Hour {h}: charge {bkwh} exceeds max rate")
        if action == "discharge" and bkwh > battery["max_discharge_kwh_per_hour"] + ABS_TOL:
            errors.append(f"Hour {h}: discharge {bkwh} exceeds max rate")

        # --- Directive window violations ---
        if h in no_charge_hours and action == "charge" and bkwh > ABS_TOL:
            errors.append(f"Hour {h}: charging during no_charge_window")
        if h in no_discharge_hours and action == "discharge" and bkwh > ABS_TOL:
            errors.append(f"Hour {h}: discharging during no_discharge_window")
        if h in max_grid_limits and g > max_grid_limits[h] + ABS_TOL:
            errors.append(
                f"Hour {h}: grid {g:.4f} > max allowed {max_grid_limits[h]}"
            )

        # --- idle ↔ battery_kwh = 0 ---
        if action == "idle" and bkwh > ABS_TOL:
            errors.append(f"Hour {h}: idle but battery_kwh = {bkwh}")

        total_grid += g
        total_cost += g * tariff
        peak_grid = max(peak_grid, g)
        prev_energy = e_after

    # ── 3. End-of-day neutrality ─────────────────────────────────────────

    if abs(prev_energy - battery["initial_energy_kwh"]) > ABS_TOL:
        errors.append(
            f"End-of-day: battery {prev_energy:.4f} ≠ "
            f"initial {battery['initial_energy_kwh']}"
        )

    # Log any errors found
    for err in errors:
        logger.warning("Replay validation: %s", err)

    return {
        "total_grid_kwh": round(total_grid, 6),
        "total_cost_bdt": round(total_cost, 6),
        "peak_grid_kwh": round(peak_grid, 6),
        "errors": errors,
    }

