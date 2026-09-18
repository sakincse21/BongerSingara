# GridWise Energy Optimizer — BUP CSE Fest 2026 Hackathon

LLM-assisted smart-campus energy scheduling API.

## Quick Start (Local)

```bash
# 1. Clone
git clone <your-repo-url>
cd BongerSingara

# 2. Create virtual environment & install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and set GEMINI_API_KEY=<your-key>

# 4. Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 5. Verify health
curl http://localhost:8000/health
# → {"status":"ok"}
```

## Docker

```bash
# Build
docker build -t gridwise-optimizer .

# Run
docker run -p 8000:8000 -e GEMINI_API_KEY=your-key gridwise-optimizer

# Verify
curl http://localhost:8000/health
```

## Environment Variables

| Variable          | Required | Default            | Description              |
| ----------------- | -------- | ------------------ | ------------------------ |
| `GEMINI_API_KEY`  | ✅       | —                  | Google AI Studio API key |
| `LLM_MODEL`       | ❌       | `gemini-2.0-flash` | Gemini model identifier  |
| `PORT`            | ❌       | `8000`             | Server port              |
| `HOST`            | ❌       | `0.0.0.0`          | Bind address             |
| `LLM_MAX_RETRIES` | ❌       | `2`                | LLM call retry count     |

## Architecture

```
Operator Notes + Energy Data
        │
        ▼
┌─────────────────┐
│  LLM Interpreter│  ← Gemini 2.0 Flash (structured JSON output)
│  (llm_interpreter.py)
└────────┬────────┘
         │ raw interpretations
         ▼
┌─────────────────┐
│   Guardrails    │  ← Deterministic validation (guardrails.py)
│   Validator     │    Enforces: type whitelist, hours 0-23 ascending,
└────────┬────────┘    factor ∈ [0,1], reserve ≤ capacity, etc.
         │ validated directives
         ▼
┌─────────────────┐
│  LP Optimizer   │  ← PuLP + CBC solver (optimizer.py)
│                 │    Minimises: Σ grid[h] × tariff[h]
└────────┬────────┘    Subject to: energy balance, battery, directives
         │ 24-hour plan
         ▼
┌─────────────────┐
│ Replay Validator│  ← Independent verification (replay_validator.py)
│                 │    Re-checks all constraints + recomputes totals
└────────┬────────┘
         │
         ▼
    JSON Response
```

## LLM Role

The LLM (Google Gemini 2.0 Flash) is **mandatory** and used exclusively for interpreting natural-language operator notes into structured directive types:

- `solar_reduction`, `minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`, `max_grid_window`, or `no_op`.

The structured output is then validated by deterministic guardrails before being applied to the mathematical optimizer.

## Optimizer / Solver

**PuLP** with the bundled **CBC** (COIN-OR Branch and Cut) LP solver. The 24-hour scheduling problem is formulated as a Linear Program with ~120 variables and ~75 constraints, solving in <10ms.

## Sample Request

```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "TEST-001",
    "operator_notes": [
      "Solar output will drop to about 20% from 1 PM to 3 PM.",
      "Do not charge the battery between 2 PM and 4 PM.",
      "The cafeteria menu changes tomorrow."
    ],
    "hours": [
      {"hour": 0, "demand_kwh": 180, "solar_kwh": 0, "tariff_bdt_per_kwh": 7},
      {"hour": 1, "demand_kwh": 170, "solar_kwh": 0, "tariff_bdt_per_kwh": 7},
      {"hour": 2, "demand_kwh": 160, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 3, "demand_kwh": 155, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 4, "demand_kwh": 160, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 5, "demand_kwh": 170, "solar_kwh": 10, "tariff_bdt_per_kwh": 7},
      {"hour": 6, "demand_kwh": 200, "solar_kwh": 40, "tariff_bdt_per_kwh": 8},
      {"hour": 7, "demand_kwh": 220, "solar_kwh": 70, "tariff_bdt_per_kwh": 8},
      {"hour": 8, "demand_kwh": 250, "solar_kwh": 100, "tariff_bdt_per_kwh": 9},
      {"hour": 9, "demand_kwh": 260, "solar_kwh": 120, "tariff_bdt_per_kwh": 9},
      {"hour": 10, "demand_kwh": 270, "solar_kwh": 140, "tariff_bdt_per_kwh": 10},
      {"hour": 11, "demand_kwh": 280, "solar_kwh": 150, "tariff_bdt_per_kwh": 10},
      {"hour": 12, "demand_kwh": 290, "solar_kwh": 145, "tariff_bdt_per_kwh": 10},
      {"hour": 13, "demand_kwh": 270, "solar_kwh": 130, "tariff_bdt_per_kwh": 9},
      {"hour": 14, "demand_kwh": 260, "solar_kwh": 110, "tariff_bdt_per_kwh": 9},
      {"hour": 15, "demand_kwh": 250, "solar_kwh": 80, "tariff_bdt_per_kwh": 9},
      {"hour": 16, "demand_kwh": 240, "solar_kwh": 50, "tariff_bdt_per_kwh": 10},
      {"hour": 17, "demand_kwh": 260, "solar_kwh": 20, "tariff_bdt_per_kwh": 12},
      {"hour": 18, "demand_kwh": 280, "solar_kwh": 5, "tariff_bdt_per_kwh": 12},
      {"hour": 19, "demand_kwh": 270, "solar_kwh": 0, "tariff_bdt_per_kwh": 12},
      {"hour": 20, "demand_kwh": 250, "solar_kwh": 0, "tariff_bdt_per_kwh": 11},
      {"hour": 21, "demand_kwh": 230, "solar_kwh": 0, "tariff_bdt_per_kwh": 10},
      {"hour": 22, "demand_kwh": 210, "solar_kwh": 0, "tariff_bdt_per_kwh": 8},
      {"hour": 23, "demand_kwh": 200, "solar_kwh": 0, "tariff_bdt_per_kwh": 9}
    ],
    "battery": {
      "capacity_kwh": 500,
      "initial_energy_kwh": 200,
      "minimum_energy_kwh": 50,
      "max_charge_kwh_per_hour": 100,
      "max_discharge_kwh_per_hour": 100
    }
  }'
```

## Running Tests

```bash
source venv/bin/activate
pytest tests/ -v
```

Tests mock the LLM call so they run offline without an API key.

## Dependencies

| Package               | Purpose                                 |
| --------------------- | --------------------------------------- |
| `fastapi`             | HTTP API framework                      |
| `uvicorn`             | ASGI server                             |
| `pydantic`            | Request/response schema validation      |
| `google-generativeai` | Gemini LLM API client                   |
| `PuLP`                | Linear programming solver (bundled CBC) |
| `python-dotenv`       | `.env` file loading                     |
| `httpx`               | HTTP client (FastAPI TestClient)        |
| `pytest`              | Test runner                             |

## Known Limitations

- Requires a valid `GEMINI_API_KEY` with sufficient quota for production use.
- If the Gemini API is completely unreachable, all notes default to `no_op` (safe fallback).
- The LP formulation assumes charge and discharge cannot happen simultaneously (enforced by post-processing).

## Secret Handling

- API keys are loaded from environment variables only — **never committed to the repository**.
- The `.env` file is in `.gitignore`.
- API responses and logs never expose keys or raw stack traces.
