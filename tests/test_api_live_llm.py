"""
Live End-to-End API tests using the REAL Gemini LLM.

Unlike tests/test_api.py (which mocks interpret_notes for instant, deterministic CI),
this test suite sends raw operator notes to the live Gemini LLM to verify:
  1. Real LLM directive interpretation accuracy (applies, directive_type, adjustments)
  2. Guardrail normalization on real model responses
  3. LP solver optimization against live LLM interpretations
  4. 24-hour physical schedule validity and end-of-day battery neutrality
  5. Optimal total cost comparison against the official public sample benchmarks

Run with:
    pytest tests/test_api_live_llm.py -v -s
Or directly with Python for a formatted report:
    python tests/test_api_live_llm.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import GEMINI_API_KEY
from app.main import app

client = TestClient(app)

# ─────────────────────────────────────────────────────────────────────────────
# Load Public Sample Cases
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_CASES_FILE = (
    Path(__file__).resolve().parent.parent
    / "instructions"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)

with open(SAMPLE_CASES_FILE, "r", encoding="utf-8") as _f:
    _SAMPLE_CASES_DATA = json.load(_f)["cases"]

SAMPLE_CASES_BY_ID = {case["id"]: case for case in _SAMPLE_CASES_DATA}

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(
        not GEMINI_API_KEY,
        reason="GEMINI_API_KEY environment variable is not set",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Verification Helper
# ─────────────────────────────────────────────────────────────────────────────


def _verify_live_sample_case(case_id: str) -> dict:
    """
    Run an end-to-end test of a sample case using the real Gemini LLM.
    Validates directive accuracy, energy balance, and schedule optimality.
    """
    case = SAMPLE_CASES_BY_ID[case_id]
    expected = case["expected_output"]
    exp_directives = expected["directive_interpretation"]

    t0 = time.time()
    resp = client.post("/optimize-energy", json=case["input"])
    latency = time.time() - t0

    assert resp.status_code == 200, f"Expected 200 OK, got {resp.status_code}: {resp.text}"
    data = resp.json()

    # 1. Scenario ID & Basic Shape
    assert data["scenario_id"] == case["id"]
    assert len(data["hourly_plan"]) == 24
    assert len(data["directive_interpretation"]) == len(case["input"]["operator_notes"])

    # 2. Compare Real LLM Directives with Expected Directives
    for note_idx, (actual_d, exp_d) in enumerate(zip(data["directive_interpretation"], exp_directives)):
        assert actual_d["note_index"] == exp_d["note_index"], (
            f"Note {note_idx}: note_index mismatch"
        )
        assert actual_d["applies"] == exp_d["applies"], (
            f"Note {note_idx}: applies mismatch (actual={actual_d['applies']}, expected={exp_d['applies']})"
        )
        assert actual_d["directive_type"] == exp_d["directive_type"], (
            f"Note {note_idx}: directive_type mismatch "
            f"(actual={actual_d['directive_type']}, expected={exp_d['directive_type']})"
        )

        actual_adj = actual_d.get("structured_adjustment")
        exp_adj = exp_d.get("structured_adjustment")

        if exp_adj is None:
            assert actual_adj is None, f"Note {note_idx}: expected null adjustment, got {actual_adj}"
        else:
            assert actual_adj is not None, f"Note {note_idx}: missing structured_adjustment"
            # Compare hours
            assert actual_adj.get("hours") == exp_adj.get("hours"), (
                f"Note {note_idx}: hours mismatch (actual={actual_adj.get('hours')}, expected={exp_adj.get('hours')})"
            )
            # Compare factor (solar_reduction)
            if "factor" in exp_adj:
                assert abs(actual_adj["factor"] - exp_adj["factor"]) < 0.05, (
                    f"Note {note_idx}: factor mismatch (actual={actual_adj['factor']}, expected={exp_adj['factor']})"
                )
            # Compare minimum_energy_kwh (minimum_battery_reserve)
            if "minimum_energy_kwh" in exp_adj:
                assert abs(actual_adj["minimum_energy_kwh"] - exp_adj["minimum_energy_kwh"]) < 1.0, (
                    f"Note {note_idx}: minimum_energy_kwh mismatch"
                )
            # Compare max_grid_kwh (max_grid_window)
            if "max_grid_kwh" in exp_adj:
                assert abs(actual_adj["max_grid_kwh"] - exp_adj["max_grid_kwh"]) < 1.0, (
                    f"Note {note_idx}: max_grid_kwh mismatch"
                )

    # 3. Schedule Physical Constraints & Energy Balance
    battery = case["input"]["battery"]
    initial_energy = battery["initial_energy_kwh"]
    capacity = battery["capacity_kwh"]
    max_charge = battery["max_charge_kwh_per_hour"]
    max_discharge = battery["max_discharge_kwh_per_hour"]

    input_hours = {h["hour"]: h for h in case["input"]["hours"]}

    for entry in data["hourly_plan"]:
        h = entry["hour"]
        grid = entry["grid_kwh"]
        solar_used = entry["solar_used_kwh"]
        action = entry["battery_action"]
        bkwh = entry["battery_kwh"]
        soc = entry["battery_energy_after_kwh"]
        ih = input_hours[h]

        assert grid >= -1e-6
        assert solar_used >= -1e-6
        assert bkwh >= -1e-6

        # Energy conservation balance
        if action == "charge":
            assert bkwh <= max_charge + 0.01
            assert abs((grid + solar_used) - (ih["demand_kwh"] + bkwh)) < 0.1
        elif action == "discharge":
            assert bkwh <= max_discharge + 0.01
            assert abs((grid + solar_used + bkwh) - ih["demand_kwh"]) < 0.1
        else:
            assert bkwh <= 0.01
            assert abs((grid + solar_used) - ih["demand_kwh"]) < 0.1

        assert soc <= capacity + 0.01

    # End-of-day battery neutrality
    last_soc = data["hourly_plan"][23]["battery_energy_after_kwh"]
    assert abs(last_soc - initial_energy) < 0.01

    # 4. Total Cost Optimality
    expected_cost = expected["total_cost_bdt"]
    actual_cost = data["total_cost_bdt"]
    assert abs(actual_cost - expected_cost) < 1.0, (
        f"Cost mismatch: actual={actual_cost}, expected={expected_cost}"
    )

    return {
        "scenario_id": case_id,
        "actual_cost": actual_cost,
        "expected_cost": expected_cost,
        "latency_sec": latency,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Explicit Individual Live LLM Test Cases
# ─────────────────────────────────────────────────────────────────────────────


def test_live_sample_01_solar_cleaning_plus_distractor():
    """
    SAMPLE-01: Solar cleaning + distractor
    Operator Notes:
      - Facilities will wash the rooftop solar panels from noon until 2 PM.
        During cleaning, usable solar should be treated as roughly 25% of the forecast.
      - The sports office moved next month's registration deadline. (Distractor)
    Expected LLM Interpretation:
      - Note 0: solar_reduction (hours: [12, 13], factor: 0.25)
      - Note 1: no_op (applies: False)
    """
    _verify_live_sample_case("SAMPLE-01")


def test_live_sample_02_battery_charging_maintenance():
    """
    SAMPLE-02: Battery charging maintenance
    Operator Notes:
      - The battery charger will be isolated from 2 AM until 5 AM for electrical maintenance.
    Expected LLM Interpretation:
      - Note 0: no_charge_window (hours: [2, 3, 4])
    """
    _verify_live_sample_case("SAMPLE-02")


def test_live_sample_03_emergency_reserve_as_percentage():
    """
    SAMPLE-03: Emergency reserve as percentage
    Operator Notes:
      - Keep at least 50% of the battery capacity stored in the battery from 6 PM until 9 PM.
    Expected LLM Interpretation:
      - Note 0: minimum_battery_reserve (hours: [18, 19, 20], minimum_energy_kwh: 100)
    """
    _verify_live_sample_case("SAMPLE-03")


def test_live_sample_04_no_discharge_protection_test():
    """
    SAMPLE-04: No-discharge protection test
    Operator Notes:
      - For protection testing, the battery must not discharge from 6 PM until 8 PM.
    Expected LLM Interpretation:
      - Note 0: no_discharge_window (hours: [18, 19])
    """
    _verify_live_sample_case("SAMPLE-04")


def test_live_sample_05_temporary_feeder_grid_cap():
    """
    SAMPLE-05: Temporary feeder grid cap
    Operator Notes:
      - From 6 PM until 9 PM, campus grid import must not exceed 155 kWh in any hour.
    Expected LLM Interpretation:
      - Note 0: max_grid_window (hours: [18, 19, 20], max_grid_kwh: 155)
    """
    _verify_live_sample_case("SAMPLE-05")


def test_live_sample_06_multiple_notes_with_distractor():
    """
    SAMPLE-06: Multiple notes with distractor
    Operator Notes:
      - Cloud cover during panel inspection will leave about half of forecast solar from 10 AM until noon.
      - The charging circuit will be unavailable from 2 PM until 4 PM.
      - The library is extending book-return hours next week. (Distractor)
    Expected LLM Interpretation:
      - Note 0: solar_reduction (hours: [10, 11], factor: 0.5)
      - Note 1: no_charge_window (hours: [14, 15])
      - Note 2: no_op (applies: False)
    """
    _verify_live_sample_case("SAMPLE-06")


def test_live_sample_07_reserve_plus_transformer_cap():
    """
    SAMPLE-07: Reserve plus transformer cap
    Operator Notes:
      - Keep at least 90 kWh in the battery from 6 PM until 10 PM.
      - The evening transformer limit is 180 kWh of grid import from 7 PM until 9 PM.
    Expected LLM Interpretation:
      - Note 0: minimum_battery_reserve (hours: [18, 19, 20, 21], minimum_energy_kwh: 90)
      - Note 1: max_grid_window (hours: [19, 20], max_grid_kwh: 180)
    """
    _verify_live_sample_case("SAMPLE-07")


def test_live_sample_08_separate_charge_discharge_outages():
    """
    SAMPLE-08: Separate charge/discharge outages
    Operator Notes:
      - Battery charging is disabled from 11 AM until 1 PM.
      - Do not discharge the battery from 5 PM until 7 PM during relay testing.
    Expected LLM Interpretation:
      - Note 0: no_charge_window (hours: [11, 12])
      - Note 1: no_discharge_window (hours: [17, 18])
    """
    _verify_live_sample_case("SAMPLE-08")


def test_live_sample_09_reduction_wording_normalization():
    """
    SAMPLE-09: Reduction wording normalization
    Operator Notes:
      - Expect an 80% reduction in rooftop solar between 11 AM and 2 PM.
      - The student affairs office will publish club notices tomorrow. (Distractor)
    Expected LLM Interpretation:
      - Note 0: solar_reduction (hours: [11, 12, 13], factor: 0.2)
      - Note 1: no_op (applies: False)
    """
    _verify_live_sample_case("SAMPLE-09")


def test_live_sample_10_multi_constraint_evening_operation():
    """
    SAMPLE-10: Multi-constraint evening operation
    Operator Notes:
      - The data center requires at least 80 kWh to remain in the battery from 6 PM until 10 PM.
      - Grid intake must stay at or below 190 kWh from 7 PM until 10 PM.
      - A seminar room booking was moved to next week. (Distractor)
    Expected LLM Interpretation:
      - Note 0: minimum_battery_reserve (hours: [18, 19, 20, 21], minimum_energy_kwh: 80)
      - Note 1: max_grid_window (hours: [19, 20, 21], max_grid_kwh: 190)
      - Note 2: no_op (applies: False)
    """
    _verify_live_sample_case("SAMPLE-10")


# ─────────────────────────────────────────────────────────────────────────────
# Standalone CLI Report Runner
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("\n" + "═" * 78)
    print("  GridWise — Live LLM End-to-End Test Suite")
    print("═" * 78)

    passed_count = 0
    total_cases = len(SAMPLE_CASES_BY_ID)

    for case_id, case in SAMPLE_CASES_BY_ID.items():
        label = case.get("label", "")
        print(f"\n▶ Running {case_id}: {label} ...", end=" ", flush=True)
        try:
            res = _verify_live_sample_case(case_id)
            passed_count += 1
            print("✓ PASSED")
            print(
                f"   Cost: {res['actual_cost']:.2f} BDT (Expected: {res['expected_cost']:.2f} BDT) "
                f"| Latency: {res['latency_sec']:.2f}s"
            )
        except Exception as exc:
            print("✗ FAILED")
            print(f"   Reason: {exc}")

    print("\n" + "═" * 78)
    print(f"  Summary: {passed_count}/{total_cases} sample cases passed with live LLM.")
    print("═" * 78 + "\n")
