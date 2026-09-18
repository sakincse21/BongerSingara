"""
End-to-end API tests — verifiable without a live LLM by mocking interpret_notes.
"""

from __future__ import annotations

import json
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

