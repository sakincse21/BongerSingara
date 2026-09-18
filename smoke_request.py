"""Smoke-test /optimize-energy against the first public sample case."""
import json
import time
import urllib.request
import urllib.error

with open(r"instructions\BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json", encoding="utf-8") as f:
    pack = json.load(f)

case = pack["cases"][0]
body = json.dumps(case["input"]).encode("utf-8")

req = urllib.request.Request(
    "http://127.0.0.1:8000/optimize-energy",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)

t0 = time.perf_counter()
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        status = resp.status
        payload = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    status = e.code
    payload = {"error": e.read().decode("utf-8")}
elapsed = time.perf_counter() - t0

print(f"HTTP {status} in {elapsed:.2f}s")
print("scenario_id:", payload.get("scenario_id"))
print("total_grid_kwh:", payload.get("total_grid_kwh"))
print("total_cost_bdt:", payload.get("total_cost_bdt"))
print("peak_grid_kwh:", payload.get("peak_grid_kwh"))
print()
print("directive_interpretation:")
for entry in payload.get("directive_interpretation", []):
    print(" ", json.dumps(entry, ensure_ascii=False))
print()
print("hourly_plan length:", len(payload.get("hourly_plan", [])))
print("first hour:", json.dumps(payload.get("hourly_plan", [{}])[0], indent=2))
print("last hour :", json.dumps(payload.get("hourly_plan", [{}])[-1], indent=2))
print()
print("plan_summary:", payload.get("plan_summary"))
print()
print("EXPECTED (reference):")
print(json.dumps(case.get("expected_output", {}).get("directive_interpretation", []), indent=2, ensure_ascii=False))
print("ref total_grid_kwh:", case.get("expected_output", {}).get("total_grid_kwh"))
print("ref total_cost_bdt :", case.get("expected_output", {}).get("total_cost_bdt"))
print("ref peak_grid_kwh  :", case.get("expected_output", {}).get("peak_grid_kwh"))

# Save full response
with open("sample01_response.json", "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)
print("\nFull response written to sample01_response.json")
