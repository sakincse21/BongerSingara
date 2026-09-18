"""
FastAPI application — the single HTTP service the judge harness exercises.

Endpoints:
  GET  /health          → {"status": "ok"}
  POST /optimize-energy → full interpretation + 24-hour schedule
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.guardrails import validate_and_fix_interpretations
from app.llm_interpreter import interpret_notes
from app.optimizer import optimize_schedule
from app.replay_validator import replay_and_compute_totals
from app.schemas import (
    DirectiveInterpretation,
    HourlyPlanEntry,
    OptimizeRequest,
    OptimizeResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="GridWise Energy Optimizer",
    description="BUP CSE Fest 2026 — LLM-assisted smart campus energy scheduling",
    version="1.0.0",
)


# ──────────────────────── Exception Handlers ─────────────────────────────


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return 400 for malformed / structurally invalid JSON (Problem Statement §06.1)."""
    return JSONResponse(
        status_code=400,
        content={"detail": f"Invalid request: {exc}"},
    )


@app.exception_handler(Exception)
async def generic_error_handler(
    _request: Request, exc: Exception
) -> JSONResponse:
    """Controlled 500 — never expose secrets or raw stack traces."""
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error."},
    )


# ──────────────────────── Endpoints ──────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    """Readiness probe for the judge harness."""
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse)
def optimize_energy(request: OptimizeRequest) -> OptimizeResponse:
    """
    Main endpoint: interpret operator notes → guardrail → optimise → validate → respond.

    Uses a synchronous def so FastAPI runs it in a thread-pool, keeping the
    event loop free while the LLM call and LP solve execute.
    """
    logger.info("Received scenario %s with %d notes", request.scenario_id, len(request.operator_notes))

    try:
        # ── Step 1: LLM Interpretation ───────────────────────────────────
        raw_interpretations = interpret_notes(request.operator_notes)
        logger.info("LLM returned %d raw interpretations", len(raw_interpretations))

        # ── Step 2: Guardrail Validation ─────────────────────────────────
        validated_directives = validate_and_fix_interpretations(
            raw_interpretations,
            num_notes=len(request.operator_notes),
            battery_capacity=request.battery.capacity_kwh,
        )

        # ── Step 3: Prepare data for optimizer ───────────────────────────
        hours_data = [h.model_dump() for h in request.hours]
        battery_data = request.battery.model_dump()

        # ── Step 4: Optimise (LP solve) ──────────────────────────────────
        plan = optimize_schedule(hours_data, battery_data, validated_directives)

        # ── Step 5: Independent replay & total recalculation ─────────────
        totals = replay_and_compute_totals(
            plan, hours_data, battery_data, validated_directives
        )

        if totals["errors"]:
            logger.warning(
                "Replay found %d issue(s) — schedule may lose validity points",
                len(totals["errors"]),
            )

        # ── Step 6: Build response ───────────────────────────────────────
        return OptimizeResponse(
            scenario_id=request.scenario_id,
            directive_interpretation=[
                DirectiveInterpretation(**d) for d in validated_directives
            ],
            hourly_plan=[HourlyPlanEntry(**p) for p in plan],
            total_grid_kwh=totals["total_grid_kwh"],
            total_cost_bdt=totals["total_cost_bdt"],
            peak_grid_kwh=totals["peak_grid_kwh"],
            plan_summary=_build_summary(validated_directives, totals),
        )

    except RuntimeError as exc:
        logger.error("Optimization error: %s", exc)
        return JSONResponse(  # type: ignore[return-value]
            status_code=500,
            content={"detail": "Optimization failed: scenario may be infeasible."},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error in pipeline: %s", exc, exc_info=True)
        return JSONResponse(  # type: ignore[return-value]
            status_code=500,
            content={"detail": "Internal server error."},
        )


# ──────────────────────── Helpers ────────────────────────────────────────


def _build_summary(
    directives: list[dict[str, Any]], totals: dict[str, Any]
) -> str:
    """Generate a short human-readable plan summary."""
    applied = [d for d in directives if d["applies"]]
    parts: list[str] = []
    if applied:
        types = ", ".join(d["directive_type"] for d in applied)
        parts.append(f"Applied {len(applied)} directive(s): {types}.")
    else:
        parts.append("No operator directives applied.")
    parts.append(f"Total grid: {totals['total_grid_kwh']:.2f} kWh.")
    parts.append(f"Total cost: {totals['total_cost_bdt']:.2f} BDT.")
    parts.append(f"Peak grid hour: {totals['peak_grid_kwh']:.2f} kWh.")
    return " ".join(parts)


# ──────────────────────── Entrypoint ─────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    from app.config import HOST, PORT

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)

