"""
Monte Carlo preflight — run before trusting balance numbers after rule changes.

  python scripts/mc_preflight.py
  python scripts/mc_preflight.py --parity
  python scripts/mc_preflight.py --no-pytest
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description="MC balance preflight (audit + optional parity + pytest).")
    ap.add_argument(
        "--parity",
        action="store_true",
        help="Run scripts/n1_golden_nights parity locks (slower).",
    )
    ap.add_argument(
        "--no-pytest",
        action="store_true",
        help="Skip pytest MC invariant suite.",
    )
    args = ap.parse_args()

    print("mc_preflight: role audit …", flush=True)
    from scripts.monte_carlo.audit import audit_against_bot_config

    audit_against_bot_config()

    if args.parity:
        print("mc_preflight: golden nights …", flush=True)
        from scripts.n1_golden_nights import run_all_golden_nights

        fails = asyncio.run(run_all_golden_nights())
        if fails:
            raise SystemExit(f"parity nights failed: {fails} scenario(s)")
        print("parity nights: OK", flush=True)

    if not args.no_pytest:
        print("mc_preflight: pytest …", flush=True)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_monte_engine_invariants.py",
            "tests/test_monte_night_bridge.py",  # engine bridge via scripts.monte_carlo.bridge
            "tests/test_stalemate_wins.py",
            "tests/test_faction_win_logic.py",
            "-q",
        ]
        rc = subprocess.call(cmd, cwd=str(ROOT))
        if rc != 0:
            raise SystemExit(rc)

    print("mc_preflight: OK", flush=True)


if __name__ == "__main__":
    main()
