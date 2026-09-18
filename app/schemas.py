"""
Pydantic models matching the exact Problem Statement contract.

Request:  OptimizeRequest   (POST /optimize-energy body)
Response: OptimizeResponse  (returned JSON)
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ──────────────────────────── Request Models ────────────────────────────


class HourEntry(BaseModel):
    """One hour of the 24-hour energy scenario."""

    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class BatteryConfig(BaseModel):
    """Battery energy storage parameters."""

    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)


class OptimizeRequest(BaseModel):
    """Incoming scenario for POST /optimize-energy."""

    scenario_id: str
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourEntry] = Field(min_length=24, max_length=24)
    battery: BatteryConfig

    @field_validator("hours")
    @classmethod
    def validate_hours_coverage(cls, v: list[HourEntry]) -> list[HourEntry]:
        """Ensure exactly hours 0-23 are present, then sort them."""
        hour_nums = sorted(h.hour for h in v)
        if hour_nums != list(range(24)):
            raise ValueError("hours must contain exactly one entry for each hour 0 through 23")
        return sorted(v, key=lambda h: h.hour)

    @field_validator("operator_notes")
    @classmethod
    def validate_notes_non_empty(cls, v: list[str]) -> list[str]:
        """Each note must be a non-empty string."""
        for i, note in enumerate(v):
            if not note or not note.strip():
                raise ValueError(f"operator_notes[{i}] must be a non-empty string")
        return v


# ──────────────────────────── Response Models ───────────────────────────


class DirectiveInterpretation(BaseModel):
    """One machine-checkable interpretation of an operator note."""

    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: Optional[dict[str, Any]] = None
    explanation: str


class HourlyPlanEntry(BaseModel):
    """One hour of the 24-hour energy schedule."""

    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)


class OptimizeResponse(BaseModel):
    """Full response for POST /optimize-energy."""

    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str

