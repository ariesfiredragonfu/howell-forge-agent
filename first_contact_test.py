"""
first_contact_test.py — ARIA System Integrity Check
────────────────────────────────────────────────────
Confirms four systems are alive before production use:

  1. Persona    — CHRIS_NAME env var resolves correctly
  2. Hands      — FreeCAD Python API creates + exports a 10mm test cube
  3. Eyes       — STEP file lands in /app/exports (write-permission check)
  4. Memory     — Redis connection via the forge-net Docker network

Run inside a running container:
  docker exec -it forge-brain python first_contact_test.py

Run as a one-shot container (needs compose network up):
  docker compose run --rm aria-brain python first_contact_test.py

Run locally (FreeCAD must be installed, Redis must be on localhost):
  REDIS_HOST=localhost python first_contact_test.py
"""

import os
import sys

PASS = "✅"
FAIL = "❌"

# ── 1. Persona ────────────────────────────────────────────────────────────────
CHRIS_NAME = os.getenv("CHRIS_NAME", "Chris")
SHOP_NAME  = os.getenv("SHOP_NAME",  "Howell Forge")
print(f"\n{'═'*55}")
print(f"  ARIA First-Contact Test — {SHOP_NAME}")
print(f"  Operator : {CHRIS_NAME}")
print(f"{'═'*55}\n")

results = {}

# ── 2. Hands (FreeCAD) ────────────────────────────────────────────────────────
print("[1/3] FreeCAD headless engine …")
try:
    import FreeCAD as App
    import Part

    doc  = App.newDocument("FirstContact")
    cube = doc.addObject("Part::Box", "TestCube")
    cube.Length = 10
    cube.Width  = 10
    cube.Height = 10
    doc.recompute()

    export_path = "/app/exports/first_contact.step"
    os.makedirs(os.path.dirname(export_path), exist_ok=True)
    Part.export([cube], export_path)

    if os.path.exists(export_path):
        size_kb = os.path.getsize(export_path) / 1024
        print(f"  {PASS} 10 mm test cube → {export_path}  ({size_kb:.1f} KB)")
        print(f"  ARIA: 'Everything looks solid, {CHRIS_NAME}. "
              f"My hands are working and the forge is cold but ready.'")
        results["freecad"] = True
    else:
        print(f"  {FAIL} STEP file was not created — check folder permissions.")
        results["freecad"] = False

except ImportError:
    print(f"  {FAIL} FreeCAD Python modules not found. "
          f"Rebuild the Docker image (apt-get install freecad).")
    results["freecad"] = False
except Exception as exc:
    print(f"  {FAIL} Unexpected error: {exc}")
    results["freecad"] = False

# ── 3. Memory (Redis) ─────────────────────────────────────────────────────────
print("\n[2/3] Redis nervous system …")
redis_host = os.getenv("REDIS_HOST", "redis-memory")
redis_port = int(os.getenv("REDIS_PORT", 6379))
try:
    import redis
    r = redis.Redis(host=redis_host, port=redis_port, db=0,
                    socket_connect_timeout=3)
    r.set("forge:status", "READY")
    readback = r.get("forge:status")
    assert readback == b"READY", f"Readback mismatch: {readback}"
    print(f"  {PASS} Redis at {redis_host}:{redis_port} — forge:status = READY")
    results["redis"] = True
except Exception as exc:
    print(f"  {FAIL} Redis unreachable ({redis_host}:{redis_port}): {exc}")
    results["redis"] = False

# ── 4. Environment variables ──────────────────────────────────────────────────
print("\n[3/3] Environment variables …")
required_vars = [
    "OPENAI_API_KEY",
    "CHRIS_NAME",
    "SHOP_NAME",
    "CAD_OUTPUT_DIR",
]
missing = [v for v in required_vars if not os.getenv(v)]
if missing:
    print(f"  ⚠️  Not set (non-fatal in dev): {', '.join(missing)}")
else:
    print(f"  {PASS} All required env vars present.")
results["env"] = len(missing) == 0

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'═'*55}")
passed = sum(results.values())
total  = len(results)
status = "ALL SYSTEMS GO" if passed == total else f"{passed}/{total} PASSED"
print(f"  Result: {status}")
for name, ok in results.items():
    icon = PASS if ok else FAIL
    print(f"    {icon}  {name}")
print(f"{'═'*55}\n")

sys.exit(0 if passed == total else 1)
