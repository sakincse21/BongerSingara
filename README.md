<div align="center">

# ⚡ GridWise Energy Optimizer

**LLM-assisted 24-hour smart-campus energy scheduler**

[![BUP CSE Fest 2026](https://img.shields.io/badge/BUP%20CSE%20Fest-2026-ff6b35?style=for-the-badge)](https://fest.bupcopc.tech)
[![Challenge](https://img.shields.io/badge/Challenge-GridWise%20%2F%20LLM-2ea44f?style=for-the-badge)]()
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/license-MIT-purple?style=for-the-badge)]()

A single deployable HTTP service that converts natural-language operator notes into structured
directives using a language model, validates them with deterministic guardrails, and runs a
linear-program optimizer to produce the lowest-cost 24-hour energy schedule that satisfies
every directive and the underlying GridWise energy rules.

[Quick Start](#-quick-start) · [Architecture](#-architecture) · [API](#-api) · [Docker](#-docker-fallback) · [Testing](#-testing) · [Deployment](#-deployment)

</div>

---

## ✨ Features

- 🧠 **LLM-Mandated Interpretation** — Every operator note is routed through Google Gemini, which classifies it into one of six supported directive types (or `no_op`).
- 🛡️ **Deterministic Guardrails** — The LLM's structured output is validated against the canonical schema (`directive_type`, `hours ∈ 0..23`, `factor ∈ [0,1]`, etc.) before it is ever allowed to touch the optimizer.
- 📐 **Linear-Program Optimizer** — A 24-hour schedule with ~120 variables and ~75 constraints is solved by PuLP + COIN-OR CBC in under 10 ms, minimizing `Σ grid[h] × tariff[h]`.
- 🔁 **Independent Replay Validator** — Every hour is replayed from the returned `hourly_plan` to confirm energy balance, battery neutrality, rate limits, and directive application.
- 🔄 **Resilient Model Fallback** — If the configured model returns 503/404, the interpreter automatically walks a chain of fallback models so the service stays available.
- 🚨 **Controlled Errors** — Malformed JSON returns 400 with field-level detail. LLM/provider failure falls back to safe `no_op` (never crashes, never invents unsupported directive types).

---

## 📑 Table of Contents

1. [Quick Start](#-quick-start)
2. [Environment Variables](#-environment-variables)
3. [Architecture](#-architecture)
4. [API](#-api)
5. [Sample Request / Response](#-sample-request--response)
6. [LLM Role](#-llm-role)
7. [Optimizer / Solver](#-optimizer--solver)
8. [Testing](#-testing)
9. [Docker Fallback](#-docker-fallback)
10. [Deployment](#-deployment)
11. [Dependencies](#-dependencies)
12. [Known Limitations](#-known-limitations)
13. [Secret Handling](#-secret-handling)
14. [Credits](#-credits)

---

## 🚀 Quick Start

> Copy-paste local quickstart from a clean environment. Works on Linux / macOS / WSL.

### 1. Clone the repository

```bash
git clone https://github.com/<your-org>/BongerSingara.git
cd BongerSingara
```

### 2. Create a virtual environment & install dependencies

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file in the project root:

```dotenv
# --- LLM Provider (REQUIRED) ---
GEMINI_API_KEY=<your-google-ai-studio-key>

# --- Model Selection (optional) ---
LLM_MODEL=gemini-3.7-flash

# --- Server ---
HOST=0.0.0.0
PORT=8000

# --- Retry behavior ---
LLM_MAX_RETRIES=2
```

> 🔐 **Never commit `.env`.** It is already in `.gitignore`. Get your key from
> [Google AI Studio](https://aistudio.google.com/app/apikey).

### 4. Start the service

```bash
export $(grep -v '^#' .env | xargs)   # load .env into the shell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

You should see:

```text
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     Application startup complete.
```

### 5. Verify health

```bash
curl http://localhost:8000/health
# -> {"status":"ok"} (JSON: {"status":"ok"})
```

### 6. Run the public sample case

A reproducible request lives at `tests/fixtures/sample_request.json`. Hit the endpoint
with it and confirm the response shape:

```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/sample_request.json | python3 -m json.tool
```

A successful response is HTTP 200 with `scenario_id`, `directive_interpretation`,
`hourly_plan` (24 entries), `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, and
`plan_summary`.

---

## 🔑 Environment Variables

| Variable          | Required | Default                  | Description                                                                 |
| ----------------- | :------: | ------------------------ | --------------------------------------------------------------------------- |
| `GEMINI_API_KEY`  | ✅       | —                        | Google AI Studio / Vertex API key used to interpret operator notes.         |
| `LLM_MODEL`       | ❌       | `gemini-3.7-flash`       | Primary Gemini model. The interpreter auto-falls-back to other Flash models on 503/404. |
| `HOST`            | ❌       | `0.0.0.0`                | Bind address. Use `0.0.0.0` for container deployments.                     |
| `PORT`            | ❌       | `8000`                   | HTTP port exposed by uvicorn.                                               |
| `LLM_MAX_RETRIES` | ❌       | `2`                      | Per-model retry count before walking the fallback chain.                    |

---

## 🏗️ Architecture

A four-stage deterministic pipeline turns natural-language operator notes into a
cost-optimal 24-hour schedule:

```text
                     ┌─────────────────────────────────────┐
                     │      POST /optimize-energy           │
                     │  (scenario_id + operator_notes +     │
                     │   hours[24] + battery)               │
                     └────────────────┬─────────────────────┘
                                      │
                                      ▼
       ┌──────────────────────────────────────────────────────┐
       │ 1️⃣  LLM INTERPRETER  (app/llm_interpreter.py)        │
       │     Gemini 3.7-flash  ->  structured JSON              │
       │     Fallback chain on 503/404                        │
       └────────────────────────┬─────────────────────────────┘
                                │ raw interpretations
                                ▼
       ┌──────────────────────────────────────────────────────┐
       │ 2️⃣  GUARDRAILS  (app/guardrails.py)                   │
       │     Directive-type whitelist                          │
       │     hours in 0..23, ascending, unique                 │
       │     factor in [0,1], reserve <= capacity              │
       │     Demotes invalid entries to safe no_op             │
       └────────────────────────┬─────────────────────────────┘
                                │ validated directives
                                ▼
       ┌──────────────────────────────────────────────────────┐
       │ 3️⃣  LP OPTIMIZER  (app/optimizer.py)                  │
       │     PuLP + CBC solver                                 │
       │     Minimise Σ grid[h]·tariff[h]                      │
       │     Subject to energy balance, battery, directives   │
       └────────────────────────┬─────────────────────────────┘
                                │ 24-hour plan
                                ▼
       ┌──────────────────────────────────────────────────────┐
       │ 4️⃣  REPLAY VALIDATOR  (app/replay_validator.py)       │
       │     Re-walks the plan hour-by-hour                    │
       │     Verifies balance, bounds, rates, neutrality      │
       │     Recomputes total_grid_kwh / total_cost_bdt       │
       └────────────────────────┬─────────────────────────────┘
                                │
                                ▼
                     ┌─────────────────────────────────────┐
                     │  HTTP 200 + OptimizeResponse JSON   │
                     └─────────────────────────────────────┘
```

### Why a separate replay validator?

The judge replays the schedule using the organizer ground-truth directive. We do the
same in-process (`app/replay_validator.py`) so that any mismatch between what the LLM
emitted, what the optimizer applied, and what we report is caught **before** we send the
response — keeping `total_grid_kwh`, `total_cost_bdt`, and `peak_grid_kwh` honest.

---

## 🔌 API

### `GET /health`

Readiness probe for the judge harness.

```bash
curl http://localhost:8000/health
```

```json
{ "status": "ok" }
```

### `POST /optimize-energy`

The main LLM-assisted scheduling endpoint. Accepts one scenario JSON object and returns
one optimization-plan JSON object.

| Field | Type | Requirement |
| ----- | ---- | ----------- |
| `scenario_id` | string | Unique synthetic scenario identifier. |
| `operator_notes` | array[1..3] of string | Natural-language notes to interpret. |
| `hours` | array[24] | One entry per hour `0..23` with `demand_kwh`, `solar_kwh`, `tariff_bdt_per_kwh`. |
| `battery` | object | `capacity_kwh`, `initial_energy_kwh`, `minimum_energy_kwh`, `max_charge_kwh_per_hour`, `max_discharge_kwh_per_hour`. |

Response fields: `scenario_id`, `directive_interpretation[]`, `hourly_plan[24]`,
`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`.

### HTTP status codes

| Code | Meaning |
| ---- | ------- |
| 200  | Successful optimization response. |
| 400  | Malformed JSON or structurally invalid request (with field-level `errors[]`). |
| 500  | Controlled internal error. Secrets and stack traces are never exposed. |

Interactive Swagger UI is available at **`/docs`** when the service is running locally.

---

## 🧪 Sample Request / Response

### Request

```json
{
  "scenario_id": "TEST-001",
  "operator_notes": [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Do not charge the battery between 2 PM and 4 PM.",
    "The cafeteria menu changes tomorrow."
  ],
  "hours": [
    {"hour": 0,  "demand_kwh": 180, "solar_kwh": 0,   "tariff_bdt_per_kwh": 7},
    {"hour": 1,  "demand_kwh": 170, "solar_kwh": 0,   "tariff_bdt_per_kwh": 7},
    {"hour": 2,  "demand_kwh": 160, "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 3,  "demand_kwh": 155, "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 4,  "demand_kwh": 160, "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 5,  "demand_kwh": 170, "solar_kwh": 10,  "tariff_bdt_per_kwh": 7},
    {"hour": 6,  "demand_kwh": 200, "solar_kwh": 40,  "tariff_bdt_per_kwh": 8},
    {"hour": 7,  "demand_kwh": 220, "solar_kwh": 70,  "tariff_bdt_per_kwh": 8},
    {"hour": 8,  "demand_kwh": 250, "solar_kwh": 100, "tariff_bdt_per_kwh": 9},
    {"hour": 9,  "demand_kwh": 260, "solar_kwh": 120, "tariff_bdt_per_kwh": 9},
    {"hour": 10, "demand_kwh": 270, "solar_kwh": 140, "tariff_bdt_per_kwh": 10},
    {"hour": 11, "demand_kwh": 280, "solar_kwh": 150, "tariff_bdt_per_kwh": 10},
    {"hour": 12, "demand_kwh": 290, "solar_kwh": 145, "tariff_bdt_per_kwh": 10},
    {"hour": 13, "demand_kwh": 270, "solar_kwh": 130, "tariff_bdt_per_kwh": 9},
    {"hour": 14, "demand_kwh": 260, "solar_kwh": 110, "tariff_bdt_per_kwh": 9},
    {"hour": 15, "demand_kwh": 250, "solar_kwh": 80,  "tariff_bdt_per_kwh": 9},
    {"hour": 16, "demand_kwh": 240, "solar_kwh": 50,  "tariff_bdt_per_kwh": 10},
    {"hour": 17, "demand_kwh": 260, "solar_kwh": 20,  "tariff_bdt_per_kwh": 12},
    {"hour": 18, "demand_kwh": 280, "solar_kwh": 5,   "tariff_bdt_per_kwh": 12},
    {"hour": 19, "demand_kwh": 270, "solar_kwh": 0,   "tariff_bdt_per_kwh": 12},
    {"hour": 20, "demand_kwh": 250, "solar_kwh": 0,   "tariff_bdt_per_kwh": 11},
    {"hour": 21, "demand_kwh": 230, "solar_kwh": 0,   "tariff_bdt_per_kwh": 10},
    {"hour": 22, "demand_kwh": 210, "solar_kwh": 0,   "tariff_bdt_per_kwh": 8},
    {"hour": 23, "demand_kwh": 200, "solar_kwh": 0,   "tariff_bdt_per_kwh": 9}
  ],
  "battery": {
    "capacity_kwh": 500,
    "initial_energy_kwh": 200,
    "minimum_energy_kwh": 50,
    "max_charge_kwh_per_hour": 100,
    "max_discharge_kwh_per_hour": 100
  }
}
```

### Response (abridged)

```json
{
  "scenario_id": "TEST-001",
  "directive_interpretation": [
    {"note_index": 0, "applies": true,  "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
     "explanation": "Solar availability reduced to 20% during hours 13-14."},
    {"note_index": 1, "applies": true,  "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [14, 15]},
     "explanation": "Battery charging prohibited during hours 14-15."},
    {"note_index": 2, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null,
     "explanation": "Cafeteria menu is irrelevant to energy operations."}
  ],
  "hourly_plan": [ /* 24 entries, one per hour 0..23 */ ],
  "total_grid_kwh": 4507,
  "total_cost_bdt": 39028,
  "peak_grid_kwh": 344,
  "plan_summary": "Applied 2 directive(s): solar_reduction, no_charge_window. ..."
}
```

A more detailed request body lives at `tests/api.http` (VS Code REST Client) and
`tests/fixtures/sample_request.json`.

---

## 🧠 LLM Role

The language model is **mandatory** and sits at the head of the interpretation path:

- **Provider / model:** Google Gemini — primary `gemini-3.7-flash`, with an automatic
  fallback chain (`gemini-flash-lite-latest`, `gemini-flash-latest`, `gemini-3.6-flash`,
  `gemini-3.5-flash`, `gemini-3.8-flash`) on 503 / 404 / network error.
- **Input:** 1–3 free-text operator notes.
- **Output:** One `directive_interpretation` entry per note, each classifying into exactly
  one of:
  - `solar_reduction`         — `{"hours": [...], "factor": float}`
  - `minimum_battery_reserve` — `{"hours": [...], "minimum_energy_kwh": float}`
  - `no_charge_window`        — `{"hours": [...]}`
  - `no_discharge_window`     — `{"hours": [...]}`
  - `max_grid_window`         — `{"hours": [...], "max_grid_kwh": float}`
  - `no_op`                   — `null` (with `applies: false`)
- **Robustness:** The system prompt contains 8 worked examples covering every supported
  type and the `1 PM to 3 PM → [13, 14]` whole-hour convention. Hidden paraphrases of the
  same underlying rule resolve to the same directive type.

The LLM never touches the optimizer directly; its structured output is validated by
`app/guardrails.py` first.

---

## 📐 Optimizer / Solver

| Component | Choice |
| --------- | ------ |
| Solver    | [PuLP](https://coin-or.github.io/pulp/) with bundled COIN-OR **CBC** |
| Variables | ~120 (24 hours × grid/solar/charge/discharge) |
| Constraints | ~75 (energy balance, battery bounds, rate limits, directive bounds) |
| Solve time | under 10 ms per scenario on a single CPU |
| Objective  | Minimise `Σ grid[h] × tariff[h]` for `h ∈ 0..23` |
| End-of-day constraint | Battery must return to `initial_energy_kwh` |

The LP formulation treats `charge` and `discharge` as disjoint binary choices per hour;
the optimizer returns the lowest-cost plan that satisfies every applicable directive and
every energy rule.

---

## 🧪 Testing

```bash
source venv/bin/activate
pytest tests/ -v
```

Tests mock the LLM call so they run **offline without an API key**. The suite covers:

- API contract for `/health` and `/optimize-energy`
- All five directive types end-to-end
- `no_op` classification for irrelevant notes
- Guardrail demotion of garbage LLM output to safe `no_op`
- The five official public sample cases (interpretation + totals)

Expected result: **19 passed**.

---

## 🐳 Docker Fallback

The provided `Dockerfile` builds a slim, reproducible image that exposes port `8000` and
binds to `0.0.0.0`. No secrets are baked in — every secret is supplied at runtime via
environment variables.

### Build

```bash
docker build -t gridwise-optimizer:latest .
```

### Run

```bash
docker run --rm -p 8000:8000 \
  -e GEMINI_API_KEY="<your-google-ai-studio-key>" \
  -e LLM_MODEL="gemini-3.7-flash" \
  gridwise-optimizer:latest
```

### Verify

```bash
curl http://localhost:8000/health
# -> {"status":"ok"}
```

The container has a built-in `HEALTHCHECK` (10 s interval, 5 s timeout, 30 s grace) so any
container orchestrator (Render, Fly, Railway, ECS) will mark the instance ready only after
`/health` is responding.

---

## ☁️ Deployment

This service can be deployed on any reachable platform that runs Docker or Python 3.11+.
The minimum required environment is:

| Setting | Value |
| ------- | ----- |
| Runtime | Docker (recommended) **or** Python 3.11+ with `uvicorn` |
| Exposed port | `8000` (TCP) |
| Bind address | `0.0.0.0` |
| Health check path | `/health` (must return 200 within 60 s of start) |
| Environment variables | `GEMINI_API_KEY` (required), `LLM_MODEL` (optional, default `gemini-3.7-flash`) |

### Render.com (one-paragraph walkthrough)

1. Push this repo to GitHub.
2. New → Web Service → connect the repo.
3. **Runtime:** Docker. Render auto-detects the `Dockerfile`.
4. **Health Check Path:** `/health`.
5. Add environment variables: `GEMINI_API_KEY=<your key>`, `LLM_MODEL=gemini-3.7-flash`.
6. Deploy. Wait for "Live". Hit `https://<service>.onrender.com/health`.

### Generic Docker host

```bash
docker pull <registry>/gridwise-optimizer:<tag>
docker run -d -p 8000:8000 \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  -e LLM_MODEL=gemini-3.7-flash \
  --restart unless-stopped \
  <registry>/gridwise-optimizer:<tag>
```

---

## 📦 Dependencies

| Package               | Version    | Purpose                                       |
| --------------------- | ---------- | --------------------------------------------- |
| `fastapi`             | ≥ 0.100.0  | HTTP API framework + automatic Swagger UI     |
| `uvicorn[standard]`   | ≥ 0.25.0   | ASGI server with HTTP/1.1 + WebSocket support |
| `pydantic`            | ≥ 2.0.0    | Strict request/response schema validation     |
| `google-genai`        | ≥ 1.0.0    | Gemini LLM API client                         |
| `PuLP`                | ≥ 2.7.0    | Linear-programming formulation + CBC solver   |
| `python-dotenv`       | ≥ 1.0.0    | `.env` file loading                           |
| `httpx`               | ≥ 0.25.0   | HTTP client used by FastAPI TestClient        |
| `pytest`              | ≥ 8.0.0    | Test framework                                |

Install everything with `pip install -r requirements.txt`.

---

## ⚠️ Known Limitations

- **Gemini quota:** The free tier has per-minute and per-day rate limits. The fallback
  chain absorbs transient 503s, but sustained traffic may require a paid API key.
- **LLM unreachable:** If every model in the chain is unreachable, all notes default to
  `no_op` (safe fallback). The schedule is still valid; only the operator directives are
  ignored.
- **Single-zone:** No support for multi-campus, grid export, or stochastic demand.
- **English-only system prompt:** The worked examples are in English. Other languages may
  work but are not officially covered.

---

## 🔐 Secret Handling

- All credentials are loaded from environment variables — **never** committed to the repo.
- The `.env` file is in `.gitignore`.
- API responses and application logs never contain API keys, raw prompts, or stack
  traces.
- The Docker image contains no baked-in secrets; every secret is supplied at runtime via
  `-e` flags or a secret manager.

---

## 🏆 Credits

Built by **BongerSingara** for **BUP CSE Fest 2026 — Online Preliminary**, in
association with Poridhi.

External tools and libraries used:

- [FastAPI](https://fastapi.tiangolo.com/) — HTTP framework
- [Pydantic](https://docs.pydantic.dev/) — schema validation
- [Google Gemini](https://aistudio.google.com/) via `google-genai` — language model
- [PuLP](https://coin-or.github.io/pulp/) + [CBC](https://github.com/coin-or/Cbc) —
  linear-programming solver
- [python-dotenv](https://pypi.org/project/python-dotenv/) — environment loading
- [pytest](https://docs.pytest.org/) — test framework

---

<div align="center">

**[⬆ Back to top](#-gridwise-energy-optimizer)**

Made with ⚡ for BUP CSE Fest 2026

</div>
