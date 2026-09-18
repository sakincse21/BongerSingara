"""
LLM Interpreter — calls Google Gemini to convert operator notes
into structured directive interpretations.

This module is the ONLY place where the LLM is invoked.  It returns
raw (un-validated) dicts that the guardrails module then validates.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import google.generativeai as genai

from app.config import GEMINI_API_KEY, LLM_MAX_RETRIES, LLM_MODEL

logger = logging.getLogger(__name__)

# ─────────────────────────── System Prompt ──────────────────────────────

SYSTEM_PROMPT = """\
You are an expert energy systems analyst for a smart campus energy management system.
Your task is to interpret each operator note and classify it into exactly ONE directive type.

## Supported Directive Types

1. **solar_reduction** — Solar output is reduced during specific hours.
   structured_adjustment: {"hours": [int, ...], "factor": float}
   CRITICAL: "factor" is the REMAINING usable fraction, NOT the reduction percentage.
     • "80% reduction" → factor = 0.2
     • "drops to about 20%" → factor = 0.2
     • "reduced by half" → factor = 0.5
     • "only 30% available" → factor = 0.3
     • "one-fifth of normal" → factor = 0.2

2. **minimum_battery_reserve** — Battery must maintain a minimum energy level.
   structured_adjustment: {"hours": [int, ...], "minimum_energy_kwh": float}

3. **no_charge_window** — Battery charging is prohibited during specific hours.
   structured_adjustment: {"hours": [int, ...]}

4. **no_discharge_window** — Battery discharging is prohibited during specific hours.
   structured_adjustment: {"hours": [int, ...]}

5. **max_grid_window** — Grid import must not exceed a stated cap during specific hours.
   structured_adjustment: {"hours": [int, ...], "max_grid_kwh": float}

6. **no_op** — The note does NOT affect the energy schedule (irrelevant information).
   structured_adjustment: null
   applies: false

## Time-Window Rules

• Start hour is INCLUSIVE, end hour is EXCLUSIVE.
  "1 PM to 3 PM" → hours [13, 14]
  "6 PM until 9 PM" → hours [18, 19, 20]
  "midnight to 5 AM" → hours [0, 1, 2, 3, 4]
  "10 AM to 2 PM" → hours [10, 11, 12, 13]
• Hours must be unique integers (0-23) in ascending order.

## Output Rules

• Return EXACTLY one interpretation per note, in note_index order (0, 1, ...).
• For no_op: applies = false, structured_adjustment = null.
• For ALL other types: applies = true with the required structured_adjustment.
• Do NOT invent directive types or energy values not stated in the note.
• If a note discusses menus, staffing, weather unrelated to solar, or anything
  that does not affect the 24-hour energy schedule, classify it as no_op.

## Worked Examples

Note: "Solar output will drop to about 20% from 1 PM to 3 PM."
→ {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
   "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
   "explanation": "Solar availability reduced to 20% during hours 13-14."}

Note: "Panel washing from one until three will leave roughly one-fifth of normal solar output."
→ {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
   "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
   "explanation": "Panel washing reduces solar to ~20% during hours 13-14."}

Note: "Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window."
→ {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
   "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
   "explanation": "80% reduction leaves 20% usable solar during hours 13-14."}

Note: "Do not charge the battery between 2 PM and 4 PM."
→ {"note_index": 1, "applies": true, "directive_type": "no_charge_window",
   "structured_adjustment": {"hours": [14, 15]},
   "explanation": "Battery charging prohibited during hours 14-15."}

Note: "Keep at least 120 kWh in reserve from 6 PM until 9 PM."
→ {"note_index": 2, "applies": true, "directive_type": "minimum_battery_reserve",
   "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 120},
   "explanation": "Battery must maintain at least 120 kWh during hours 18-20."}

Note: "The cafeteria menu changes tomorrow."
→ {"note_index": 3, "applies": false, "directive_type": "no_op",
   "structured_adjustment": null,
   "explanation": "Cafeteria menu is irrelevant to energy operations."}

Note: "Limit grid import to 150 kWh per hour from 5 PM to 8 PM."
→ {"note_index": 0, "applies": true, "directive_type": "max_grid_window",
   "structured_adjustment": {"hours": [17, 18, 19], "max_grid_kwh": 150},
   "explanation": "Grid import capped at 150 kWh/hour during hours 17-19."}

Note: "Do not discharge the battery from 11 PM to 1 AM."
→ {"note_index": 0, "applies": true, "directive_type": "no_discharge_window",
   "structured_adjustment": {"hours": [0, 23]},
   "explanation": "Battery discharge prohibited during hours 23 and 0."}
"""

# ─────────────────────── User Prompt Template ───────────────────────────

USER_PROMPT_TEMPLATE = """\
Interpret the following operator notes for a 24-hour campus energy schedule.
{battery_info}Return a JSON object with an "interpretations" array containing exactly {count} entries (one per note, in order).
If a minimum battery reserve is specified as a percentage (e.g. '50%'), calculate minimum_energy_kwh = (percentage / 100) * battery_capacity.

Operator Notes:
{notes}

Return JSON:
{{
  "interpretations": [
    {{
      "note_index": <int>,
      "applies": <true|false>,
      "directive_type": "<type>",
      "structured_adjustment": <object or null>,
      "explanation": "<brief explanation>"
    }}
  ]
}}"""


# ──────────────────────── Public Interface ──────────────────────────────


def interpret_notes(
    operator_notes: list[str],
    battery_capacity: float | None = None,
) -> list[dict[str, Any]]:
    """
    Call the LLM to interpret operator notes into structured directives.

    Returns a list of raw interpretation dicts (not yet guardrail-validated).
    On total failure, returns safe no_op fallbacks for every note.
    """
    model = _get_model()

    notes_text = "\n".join(f'{i}: "{note}"' for i, note in enumerate(operator_notes))
    battery_info = (
        f"Campus Battery Capacity: {battery_capacity} kWh\n"
        if battery_capacity is not None
        else ""
    )
    user_prompt = USER_PROMPT_TEMPLATE.format(
        count=len(operator_notes),
        notes=notes_text,
        battery_info=battery_info,
    )

    last_error: Exception | None = None
    for attempt in range(1, LLM_MAX_RETRIES + 2):
        try:
            response = model.generate_content(user_prompt)
            parsed = json.loads(response.text)

            # Accept either {"interpretations": [...]} or bare [...]
            if isinstance(parsed, dict) and "interpretations" in parsed:
                return parsed["interpretations"]
            if isinstance(parsed, list):
                return parsed

            logger.warning("LLM returned unexpected shape on attempt %d: %s", attempt, type(parsed))

        except json.JSONDecodeError as exc:
            last_error = exc
            logger.warning("LLM JSON decode error on attempt %d: %s", attempt, exc)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("LLM call failed on attempt %d: %s", attempt, exc)

    logger.error("All %d LLM attempts exhausted. Last error: %s", LLM_MAX_RETRIES + 1, last_error)
    return _fallback_interpretations(operator_notes)


# ──────────────────────── Private Helpers ────────────────────────────────


def _get_model() -> genai.GenerativeModel:
    """Configure and return a Gemini GenerativeModel instance."""
    genai.configure(api_key=GEMINI_API_KEY, transport="rest")
    return genai.GenerativeModel(
        model_name=LLM_MODEL,
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.1,
        },
        system_instruction=SYSTEM_PROMPT,
    )


def _fallback_interpretations(operator_notes: list[str]) -> list[dict[str, Any]]:
    """Safe fallback when the LLM is completely unreachable."""
    return [
        {
            "note_index": i,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "LLM interpretation unavailable; defaulting to no_op.",
        }
        for i in range(len(operator_notes))
    ]

