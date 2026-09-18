"""
End-to-end API tests — verifiable without a live LLM by mocking interpret_notes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ──────────────────────── Sample Fixtures ─────────────────────────────────


SAMPLE_BATTERY = {
    "capacity_kwh": 500,
    "initial_energy_kwh": 200,
    "minimum_energy_kwh": 50,
    "max_charge_kwh_per_hour": 100,
    "max_discharge_kwh_per_hour": 100,
}

SAMPLE_HOURS = [
    {
        "hour": h,
        "demand_kwh": 150,
        "solar_kwh": 80 if 6 <= h <= 17 else 0,
        "tariff_bdt_per_kwh": 12 if 17 <= h <= 21 else 8,
    }
    for h in range(24)
]


def _make_request(
    notes: list[str] | None = None,
    scenario_id: str = "TEST-001",
) -> dict:
    return {
        "scenario_id": scenario_id,
        "operator_notes": notes or ["The cafeteria menu changes tomorrow."],
        "hours": SAMPLE_HOURS,
        "battery": SAMPLE_BATTERY,
    }


# ──────────────────────── Health Endpoint ─────────────────────────────────


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ──────────────────── Malformed Request → 400 ─────────────────────────────


def test_malformed_json():
    """Completely broken JSON should return 400."""
    resp = client.post(
        "/optimize-energy",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code in (400, 422)


def test_missing_fields():
    """Missing required fields should return 400."""
    resp = client.post("/optimize-energy", json={"scenario_id": "X"})
    assert resp.status_code in (400, 422)


# ──────────────────── Full Pipeline (mocked LLM) ─────────────────────────


MOCK_NO_OP_INTERPRETATION = [
    {
        "note_index": 0,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "Irrelevant note.",
    }
]


@patch("app.main.interpret_notes", return_value=MOCK_NO_OP_INTERPRETATION)
def test_optimize_no_op(mock_llm):
    """A single no_op note should still produce a valid 24-hour schedule."""
    resp = client.post("/optimize-energy", json=_make_request())
    assert resp.status_code == 200

    data = resp.json()
    assert data["scenario_id"] == "TEST-001"
    assert len(data["directive_interpretation"]) == 1
    assert data["directive_interpretation"][0]["directive_type"] == "no_op"
    assert len(data["hourly_plan"]) == 24
    assert data["total_grid_kwh"] >= 0
    assert data["total_cost_bdt"] >= 0
    assert data["peak_grid_kwh"] >= 0
    assert isinstance(data["plan_summary"], str)


MOCK_SOLAR_REDUCTION = [
    {
        "note_index": 0,
        "applies": True,
        "directive_type": "solar_reduction",
        "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
        "explanation": "Solar reduced to 20%.",
    },
    {
        "note_index": 1,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "Irrelevant.",
    },
]


@patch("app.main.interpret_notes", return_value=MOCK_SOLAR_REDUCTION)
def test_optimize_solar_reduction(mock_llm):
    """solar_reduction should reduce effective solar and still produce valid schedule."""
    request = _make_request(
        notes=[
            "Solar output will drop to about 20% from 1 PM to 3 PM.",
            "The cafeteria menu changes tomorrow.",
        ]
    )
    resp = client.post("/optimize-energy", json=request)
    assert resp.status_code == 200

    data = resp.json()
    assert len(data["directive_interpretation"]) == 2
    assert data["directive_interpretation"][0]["directive_type"] == "solar_reduction"
    assert data["directive_interpretation"][1]["directive_type"] == "no_op"

    # Verify solar usage in affected hours
    for entry in data["hourly_plan"]:
        if entry["hour"] in [13, 14]:
            # Effective solar = 80 * 0.2 = 16 kWh
            assert entry["solar_used_kwh"] <= 16.01


MOCK_NO_CHARGE = [
    {
        "note_index": 0,
        "applies": True,
        "directive_type": "no_charge_window",
        "structured_adjustment": {"hours": [14, 15]},
        "explanation": "No charging allowed.",
    },
]


@patch("app.main.interpret_notes", return_value=MOCK_NO_CHARGE)
def test_optimize_no_charge_window(mock_llm):
    """no_charge_window hours should have no charging."""
    request = _make_request(
        notes=["Do not charge the battery between 2 PM and 4 PM."]
    )
    resp = client.post("/optimize-energy", json=request)
    assert resp.status_code == 200

    data = resp.json()
    for entry in data["hourly_plan"]:
        if entry["hour"] in [14, 15]:
            if entry["battery_action"] == "charge":
                assert entry["battery_kwh"] <= 0.01


# ──────────────────── Energy Balance Verification ─────────────────────────


@patch("app.main.interpret_notes", return_value=MOCK_NO_OP_INTERPRETATION)
def test_energy_balance(mock_llm):
    """Every hour must satisfy energy balance within tolerance."""
    resp = client.post("/optimize-energy", json=_make_request())
    data = resp.json()

    for entry in data["hourly_plan"]:
        h = entry["hour"]
        g = entry["grid_kwh"]
        s = entry["solar_used_kwh"]
        action = entry["battery_action"]
        bkwh = entry["battery_kwh"]
        demand = SAMPLE_HOURS[h]["demand_kwh"]

        if action == "charge":
            balance = g + s - demand - bkwh
        elif action == "discharge":
            balance = g + s + bkwh - demand
        else:
            balance = g + s - demand

        assert abs(balance) < 0.1, f"Hour {h}: balance off by {balance}"


@patch("app.main.interpret_notes", return_value=MOCK_NO_OP_INTERPRETATION)
def test_end_of_day_neutrality(mock_llm):
    """Battery energy at end of hour 23 must equal initial energy."""
    resp = client.post("/optimize-energy", json=_make_request())
    data = resp.json()

    last_entry = [e for e in data["hourly_plan"] if e["hour"] == 23][0]
    assert abs(last_entry["battery_energy_after_kwh"] - 200) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Public Sample Cases (from BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json)
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_CASES_FILE = (
    Path(__file__).resolve().parent.parent
    / "instructions"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)

with open(SAMPLE_CASES_FILE, "r", encoding="utf-8") as _f:
    _SAMPLE_CASES_DATA = json.load(_f)["cases"]

SAMPLE_CASES_BY_ID = {case["id"]: case for case in _SAMPLE_CASES_DATA}


def _verify_sample_case_constraints(case: dict, data: dict) -> None:
    """Validate all GridWise constraints, directives, and energy conservation."""
    expected = case["expected_output"]
    mock_interp = expected["directive_interpretation"]

    # 1. Scenario ID & Plan Length
    assert data["scenario_id"] == case["id"]
    assert len(data["hourly_plan"]) == 24

    # 2. Directive Interpretation matching
    assert len(data["directive_interpretation"]) == len(mock_interp)
    for actual_d, exp_d in zip(data["directive_interpretation"], mock_interp):
        assert actual_d["note_index"] == exp_d["note_index"]
        assert actual_d["applies"] == exp_d["applies"]
        assert actual_d["directive_type"] == exp_d["directive_type"]
        assert actual_d["structured_adjustment"] == exp_d["structured_adjustment"]

    # 3. Cost & Grid optimality within tolerance
    assert abs(data["total_cost_bdt"] - expected["total_cost_bdt"]) < 0.1
    assert abs(data["total_grid_kwh"] - expected["total_grid_kwh"]) < 0.1

    # 4. Directive constraints extraction
    battery = case["input"]["battery"]
    capacity = battery["capacity_kwh"]
    initial_energy = battery["initial_energy_kwh"]
    min_reserve_base = battery["minimum_energy_kwh"]
    max_charge = battery["max_charge_kwh_per_hour"]
    max_discharge = battery["max_discharge_kwh_per_hour"]

    no_charge_hours: set[int] = set()
    no_discharge_hours: set[int] = set()
    max_grid_caps: dict[int, float] = {}
    min_reserves: dict[int, float] = {h: min_reserve_base for h in range(24)}
    solar_factors: dict[int, float] = {h: 1.0 for h in range(24)}

    for d in mock_interp:
        if not d["applies"] or not d.get("structured_adjustment"):
            continue
        dtype = d["directive_type"]
        adj = d["structured_adjustment"]
        hours = adj.get("hours", [])
        if dtype == "no_charge_window":
            no_charge_hours.update(hours)
        elif dtype == "no_discharge_window":
            no_discharge_hours.update(hours)
        elif dtype == "max_grid_window":
            for h in hours:
                max_grid_caps[h] = adj["max_grid_kwh"]
        elif dtype == "minimum_battery_reserve":
            for h in hours:
                min_reserves[h] = max(min_reserves[h], adj["minimum_energy_kwh"])
        elif dtype == "solar_reduction":
            for h in hours:
                solar_factors[h] = min(solar_factors[h], adj["factor"])

    input_hours = {h["hour"]: h for h in case["input"]["hours"]}

    # 5. Hourly validation
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

        # Effective solar constraint
        effective_solar = ih["solar_kwh"] * solar_factors[h]
        assert solar_used <= effective_solar + 0.01

        # Battery action and hourly energy balance
        if action == "charge":
            assert h not in no_charge_hours, f"Charge attempted during no_charge hour {h}"
            assert bkwh <= max_charge + 0.01
            assert abs((grid + solar_used) - (ih["demand_kwh"] + bkwh)) < 0.1
        elif action == "discharge":
            assert h not in no_discharge_hours, f"Discharge attempted during no_discharge hour {h}"
            assert bkwh <= max_discharge + 0.01
            assert abs((grid + solar_used + bkwh) - ih["demand_kwh"]) < 0.1
        else:
            assert bkwh <= 0.01
            assert abs((grid + solar_used) - ih["demand_kwh"]) < 0.1

        # Battery limits (reserve and capacity)
        assert soc <= capacity + 0.01
        assert soc >= min_reserves[h] - 0.01

        # Max grid cap directive
        if h in max_grid_caps:
            assert grid <= max_grid_caps[h] + 0.01

    # End-of-day neutrality (SOC_23 == initial_energy)
    last_soc = data["hourly_plan"][23]["battery_energy_after_kwh"]
    assert abs(last_soc - initial_energy) < 0.01


def _run_sample_case(case_id: str) -> None:
    """Helper to run a sample case from the JSON pack through /optimize-energy."""
    case = SAMPLE_CASES_BY_ID[case_id]
    expected = case["expected_output"]
    with patch("app.main.interpret_notes", return_value=expected["directive_interpretation"]):
        resp = client.post("/optimize-energy", json=case["input"])
    assert resp.status_code == 200
    _verify_sample_case_constraints(case, resp.json())


# ─────────────────────────────────────────────────────────────────────────────
# Individual Public Sample Case Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_sample_01_solar_cleaning_plus_distractor():
    """
    SAMPLE-01: Solar cleaning + distractor
    Operator Notes:
      - Facilities will wash the rooftop solar panels from noon until 2 PM.
        Usable solar roughly 25% of forecast.
      - The sports office moved next month's registration deadline. (Distractor)
    Directives:
      - Note 0: solar_reduction (hours: [12, 13], factor: 0.25)
      - Note 1: no_op
    """
    _run_sample_case("SAMPLE-01")


def test_sample_02_battery_charging_maintenance():
    """
    SAMPLE-02: Battery charging maintenance
    Operator Notes:
      - The battery charger will be isolated from 2 AM until 5 AM for electrical maintenance.
    Directives:
      - Note 0: no_charge_window (hours: [2, 3, 4])
    """
    _run_sample_case("SAMPLE-02")


def test_sample_03_emergency_reserve_as_percentage():
    """
    SAMPLE-03: Emergency reserve as percentage
    Operator Notes:
      - Keep at least 50% of the battery capacity stored in the battery from 6 PM until 9 PM.
    Directives:
      - Note 0: minimum_battery_reserve (hours: [18, 19, 20], minimum_energy_kwh: 100)
    """
    _run_sample_case("SAMPLE-03")


def test_sample_04_no_discharge_protection_test():
    """
    SAMPLE-04: No-discharge protection test
    Operator Notes:
      - For protection testing, the battery must not discharge from 6 PM until 8 PM.
    Directives:
      - Note 0: no_discharge_window (hours: [18, 19])
    """
    _run_sample_case("SAMPLE-04")


def test_sample_05_temporary_feeder_grid_cap():
    """
    SAMPLE-05: Temporary feeder grid cap
    Operator Notes:
      - From 6 PM until 9 PM, campus grid import must not exceed 155 kWh in any hour.
    Directives:
      - Note 0: max_grid_window (hours: [18, 19, 20], max_grid_kwh: 155)
    """
    _run_sample_case("SAMPLE-05")


def test_sample_06_multiple_notes_with_distractor():
    """
    SAMPLE-06: Multiple notes with distractor
    Operator Notes:
      - Cloud cover during panel inspection leaves about half solar output from 10 AM until noon.
      - The charging circuit will be unavailable from 2 PM until 4 PM.
      - The library is extending book-return hours next week. (Distractor)
    Directives:
      - Note 0: solar_reduction (hours: [10, 11], factor: 0.5)
      - Note 1: no_charge_window (hours: [14, 15])
      - Note 2: no_op
    """
    _run_sample_case("SAMPLE-06")


def test_sample_07_reserve_plus_transformer_cap():
    """
    SAMPLE-07: Reserve plus transformer cap
    Operator Notes:
      - Keep at least 90 kWh in the battery from 6 PM until 10 PM.
      - The evening transformer limit is 180 kWh of grid import from 7 PM until 9 PM.
    Directives:
      - Note 0: minimum_battery_reserve (hours: [18, 19, 20, 21], minimum_energy_kwh: 90)
      - Note 1: max_grid_window (hours: [19, 20], max_grid_kwh: 180)
    """
    _run_sample_case("SAMPLE-07")


def test_sample_08_separate_charge_discharge_outages():
    """
    SAMPLE-08: Separate charge/discharge outages
    Operator Notes:
      - Battery charging is disabled from 11 AM until 1 PM.
      - Do not discharge the battery from 5 PM until 7 PM during relay testing.
    Directives:
      - Note 0: no_charge_window (hours: [11, 12])
      - Note 1: no_discharge_window (hours: [17, 18])
    """
    _run_sample_case("SAMPLE-08")


def test_sample_09_reduction_wording_normalization():
    """
    SAMPLE-09: Reduction wording normalization
    Operator Notes:
      - Expect an 80% reduction in rooftop solar between 11 AM and 2 PM.
      - The student affairs office will publish club notices tomorrow. (Distractor)
    Directives:
      - Note 0: solar_reduction (hours: [11, 12, 13], factor: 0.2)
      - Note 1: no_op
    """
    _run_sample_case("SAMPLE-09")


def test_sample_10_multi_constraint_evening_operation():
    """
    SAMPLE-10: Multi-constraint evening operation
    Operator Notes:
      - The data center requires at least 80 kWh to remain in the battery from 6 PM until 10 PM.
      - Grid intake must stay at or below 190 kWh from 7 PM until 10 PM.
      - A seminar room booking was moved to next week. (Distractor)
    Directives:
      - Note 0: minimum_battery_reserve (hours: [18, 19, 20, 21], minimum_energy_kwh: 80)
      - Note 1: max_grid_window (hours: [19, 20, 21], max_grid_kwh: 190)
      - Note 2: no_op
    """
    _run_sample_case("SAMPLE-10")


