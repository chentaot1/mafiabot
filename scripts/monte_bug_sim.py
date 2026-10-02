from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Ensure repo root is importable when executed as scripts/monte_bug_sim.py
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import monte_carlo_sim as sim  # noqa: E402


def _write_repro(*, payload: Dict[str, Any]) -> Path:
    repro_dir = ROOT / "tests" / "repros_monte"
    repro_dir.mkdir(parents=True, exist_ok=True)
    # Keep name stable-ish for easy grepping.
    seed = payload.get("seed", "unknown")
    path = repro_dir / f"monte_bug_sim_seed{seed}.json"
    # Avoid importing json at module import time in case callers only want --help.
    import json

    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _parse_csv_set(v: Optional[str]) -> Optional[Set[str]]:
    if not v:
        return None
    parts = [p.strip() for p in v.split(",") if p.strip()]
    return set(parts) if parts else None


def _role_sample(
    *,
    player_count: int,
    include: Optional[Set[str]],
    exclude: Optional[Set[str]],
    mafia_override: Optional[int],
    neutral_override: Optional[int],
) -> List[str]:
    return sim.sample_generator_roles_constraints(
        player_count,
        include_roles=include,
        exclude_roles=exclude,
        mafia_override=mafia_override,
        neutral_override=neutral_override,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Monte Carlo bug-hunting runner (reuses monte_carlo_sim).")
    ap.add_argument("--player-count", type=int, default=7)
    ap.add_argument("--trials", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--max-days", type=int, default=20)
    ap.add_argument("--write-repro", action="store_true", help="Write tests/repros/*.json payload on failure.")
    ap.add_argument("--include", type=str, default="", help="Comma-separated roles that must appear in every trial")
    ap.add_argument("--exclude", type=str, default="", help="Comma-separated roles that must not appear in any trial")
    ap.add_argument("--mafia-override", type=int, default=None)
    ap.add_argument("--neutral-override", type=int, default=None)
    ap.add_argument("--shuffle-seats", action="store_true", help="Shuffle seat order per trial (default on)")
    ap.add_argument("--no-shuffle-seats", dest="shuffle_seats", action="store_false")
    ap.set_defaults(shuffle_seats=True)
    args = ap.parse_args()

    include = _parse_csv_set(args.include)
    exclude = _parse_csv_set(args.exclude)

    # Quick guard: ensure simulator role universe still matches bot config.
    sim.audit_against_bot_config()

    base_seed = int(args.seed)
    for t in range(int(args.trials)):
        trial_seed = base_seed + t
        random.seed(trial_seed)
        roles = _role_sample(
            player_count=int(args.player_count),
            include=include,
            exclude=exclude,
            mafia_override=args.mafia_override,
            neutral_override=args.neutral_override,
        )
        if args.shuffle_seats:
            random.shuffle(roles)

        try:
            sim.simulate_once(roles, max_days=int(args.max_days), collect_stats=False, trace=False)
        except Exception as e:
            # Re-run with trace enabled to print a minimal repro.
            random.seed(trial_seed)
            try:
                _out, log = sim.simulate_once(roles, max_days=int(args.max_days), collect_stats=False, trace=True)  # type: ignore[misc]
            except Exception:
                log = ["<trace failed: exception also thrown while tracing>"]

            print("\n=== MONTE BUG SIM FAILURE ===")
            print(f"trial={t} seed={trial_seed}")
            print(f"roles={roles}")
            print(f"exception={type(e).__name__}: {e}")
            print("\n--- trace (last 200 lines) ---")
            for line in log[-200:]:
                print(line)
            if args.write_repro:
                payload: Dict[str, Any] = {
                    "kind": "monte_bug_sim",
                    "trial": int(t),
                    "seed": int(trial_seed),
                    "player_count": int(args.player_count),
                    "max_days": int(args.max_days),
                    "roles": list(roles),
                    "exception": {"type": type(e).__name__, "message": str(e)},
                    "trace_tail": list(log[-200:]),
                }
                p = _write_repro(payload=payload)
                print(f"\nWROTE REPRO: {p}", flush=True)
            raise

    print(f"monte_bug_sim.py: OK  trials={args.trials} seed={args.seed} player_count={args.player_count}")


if __name__ == "__main__":
    main()
