#!/usr/bin/env python3
"""
Run smoke_test.py, then bounded night-engine harness (sim_test + property_test).

Use this for a single command that validates in-process smoke checks and the
night resolution pipeline (subprocess sim fuzz + Hypothesis properties).

Env:
  SMOKE_NIGHT_SIM_FUZZ — fuzz iterations for sim_test (default 100)
  NIGHT_GATE_SKIP_PROPERTY — if 1, skip property_test.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    # Plain smoke first; sim_test + property follow (avoid double sim if SMOKE_WITH_NIGHT_SIM is set).
    env = os.environ.copy()
    env.pop("SMOKE_WITH_NIGHT_SIM", None)
    subprocess.run([sys.executable, str(ROOT / "smoke_test.py")], cwd=str(ROOT), check=True, env=env)

    n = int(os.environ.get("SMOKE_NIGHT_SIM_FUZZ", "100"))
    n = max(1, min(n, 50_000))
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "sim_test.py"),
            "--player-count",
            "7",
            "--seed",
            str(int(os.environ.get("SMOKE_NIGHT_SIM_SEED", "12345"))),
            "--fuzz-iterations",
            str(n),
            "--skip-scenarios",
            "--skip-exhaustive",
        ],
        cwd=str(ROOT),
        check=True,
    )

    if os.environ.get("NIGHT_GATE_SKIP_PROPERTY", "").strip().lower() not in ("1", "true", "yes"):
        subprocess.run([sys.executable, str(ROOT / "scripts" / "property_test.py")], cwd=str(ROOT), check=True)

    print("night_smoke_gate.py: OK", flush=True)


if __name__ == "__main__":
    main()
