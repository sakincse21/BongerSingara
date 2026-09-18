"""
Deterministic Guardrail Validator — validates and normalises raw LLM
interpretation output before it reaches the optimizer.

Rules enforced (from Problem Statement §08):
  • directive_type must be one of the 6 supported types.
  • note_index must identify an existing note; each note appears once.
  • hours are unique integers 0-23 in ascending order.
  • solar factor ∈ [0, 1].
  • battery reserve ≥ 0 and ≤ capacity.
  • max_grid_kwh ≥ 0.
  • no_op ↔ applies=false + null adjustment.
  • All other directives ↔ applies=true + required adjustment shape.

If any interpretation is invalid, it is safely demoted to no_op.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

VALID_DIRECTIVE_TYPES: set[str] = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


# ──────────────────────── Public Interface ──────────────────────────────


def validate_and_fix_interpretations(
    raw_interpretations: list[dict[str, Any]],
    num_notes: int,
    battery_capacity: float,
) -> list[dict[str, Any]]:
    """
    Validate each raw LLM interpretation and produce a clean list of
    exactly *num_notes* entries in note_index order 0 … N-1.

    Invalid entries are demoted to no_op with an explanation.
    """
    validated: list[dict[str, Any]] = []

    for i in range(num_notes):
        # Find the raw entry for this note_index
        raw = _find_by_index(raw_interpretations, i)

        if raw is None:
            validated.append(_make_no_op(i, "No LLM interpretation found for this note."))
            continue

        dtype: str = str(raw.get("directive_type", "")).strip().lower()

        # ---- Check directive type -------------------------------------------
        if dtype not in VALID_DIRECTIVE_TYPES:
            validated.append(_make_no_op(i, f"Unsupported directive type '{dtype}'."))
            continue

        # ---- Handle no_op ---------------------------------------------------
        if dtype == "no_op":
            validated.append(_make_no_op(i, raw.get("explanation", "Not applicable.")))
            continue

        # ---- Validate structured_adjustment exists ---------------------------
        adj = raw.get("structured_adjustment")
        if adj is None or not isinstance(adj, dict):
            validated.append(_make_no_op(i, "Missing or invalid structured_adjustment."))
            continue

        # ---- Validate hours --------------------------------------------------
        hours_raw = adj.get("hours", [])
        hours = _normalise_hours(hours_raw)
        if hours is None:
            validated.append(_make_no_op(i, f"Invalid hours list: {hours_raw}"))
            continue

        # ---- Type-specific validation ----------------------------------------
        clean_adj = _validate_type_specific(dtype, adj, hours, battery_capacity)
        if clean_adj is None:
            validated.append(
                _make_no_op(i, f"Invalid adjustment values for {dtype}.")
            )
            continue

        validated.append(
            {
                "note_index": i,
                "applies": True,
                "directive_type": dtype,
                "structured_adjustment": clean_adj,
                "explanation": str(raw.get("explanation", "")),
            }
        )

    return validated


# ──────────────────────── Private Helpers ────────────────────────────────


def _find_by_index(
    raw_list: list[dict[str, Any]], idx: int
) -> dict[str, Any] | None:
    """Return the first entry whose note_index matches *idx*."""
    for item in raw_list:
        if item.get("note_index") == idx:
            return item
    # Fallback: use positional index if note_index is missing/wrong
    if 0 <= idx < len(raw_list):
        return raw_list[idx]
    return None


def _normalise_hours(hours_raw: Any) -> list[int] | None:
    """
    Validate and normalise an hours list.

    Returns sorted unique ints in [0, 23] or None on failure.
    """
    if not isinstance(hours_raw, list) or len(hours_raw) == 0:
        return None

    cleaned: list[int] = []
    for h in hours_raw:
        if isinstance(h, (int, float)) and 0 <= int(h) <= 23:
            cleaned.append(int(h))
        else:
            return None

    result = sorted(set(cleaned))
    if len(result) == 0:
        return None
    return result


def _validate_type_specific(
    dtype: str,
    adj: dict[str, Any],
    hours: list[int],
    battery_capacity: float,
) -> dict[str, Any] | None:
    """
    Return a clean structured_adjustment dict for *dtype*,
    or None if the values are invalid.
    """
    if dtype == "solar_reduction":
        factor = _as_float(adj.get("factor"))
        if factor is None:
            return None
        factor = max(0.0, min(1.0, factor))  # clamp to [0, 1]
        return {"hours": hours, "factor": round(factor, 6)}

    if dtype == "minimum_battery_reserve":
        reserve = _as_float(adj.get("minimum_energy_kwh"))
        if reserve is None or reserve < 0 or reserve > battery_capacity:
            return None
        return {"hours": hours, "minimum_energy_kwh": round(reserve, 6)}

    if dtype == "no_charge_window":
        return {"hours": hours}

    if dtype == "no_discharge_window":
        return {"hours": hours}

    if dtype == "max_grid_window":
        cap = _as_float(adj.get("max_grid_kwh"))
        if cap is None or cap < 0:
            return None
        return {"hours": hours, "max_grid_kwh": round(cap, 6)}

    return None  # unreachable for valid dtype


def _as_float(value: Any) -> float | None:
    """Safely coerce to float."""
    if value is None:
        return None
    try:
        f = float(value)
        if not __import__("math").isfinite(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _make_no_op(note_index: int, explanation: str) -> dict[str, Any]:
    """Create a canonical no_op entry."""
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": explanation,
    }

