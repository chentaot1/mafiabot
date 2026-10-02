from __future__ import annotations

"""
Fuzz persisted-state decoding/rehydration for corruption tolerance.

This targets Game.from_persisted(), which is a common source of "restart bricked the bot"
bugs when JSON is partially written/corrupted.

On failure it writes a repro JSON under tests/repros_state/.
"""

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game import Game  # noqa: E402


REPRO_DIR = ROOT / "tests" / "repros_state"


def _write_repro(*, seed: int, i: int, payload: Dict[str, Any], exc: Exception) -> Path:
    REPRO_DIR.mkdir(parents=True, exist_ok=True)
    p = REPRO_DIR / f"state_fuzz_seed{seed}_i{i}_{type(exc).__name__}.json"
    p.write_text(
        json.dumps(
            {
                "kind": "state_fuzz",
                "seed": int(seed),
                "iteration": int(i),
                "payload": payload,
                "exception": {"type": type(exc).__name__, "message": str(exc)},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return p


def _rand_scalar(rng: random.Random) -> Any:
    return rng.choice([None, True, False, 0, 1, -1, 3.14, "", "0", "false", "true", "NaN", [], {}, {"x": 1}])


def _rand_mapping(rng: random.Random, *, max_items: int = 6) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for _ in range(rng.randint(0, max_items)):
        k = rng.choice(["1", "2", "x", "NaN", "", "999999999999", "-5"])
        d[k] = _rand_scalar(rng)
    return d


def _make_payload(rng: random.Random) -> Dict[str, Any]:
    # Start with a loosely-correct shape, then corrupt fields.
    payload: Dict[str, Any] = {
        "guild_id": rng.choice([123, "123", None, "NaN", -1]),
        "in_progress": rng.choice([True, False, "false", "true", 0, 1, None]),
        "phase": rng.choice([None, "day", "night", 7, ""]),
        "day_number": rng.choice([0, 1, "2", "NaN", None, -3]),
        "player_ids": rng.choice([[1, 2, 3], ["1", "x"], None, 5, []]),
        "living_ids": rng.choice([[1, 2], ["2", "oops"], None, {}]),
        "player_roles": rng.choice([_rand_mapping(rng), None, 1, []]),
        "night_actions": rng.choice([_rand_mapping(rng), None, 1, []]),
        "role_states": rng.choice([_rand_mapping(rng), None, 1, []]),
        "player_slots": rng.choice([_rand_mapping(rng), None, 1, []]),
        "doused_players": rng.choice([[1, "2", "x"], None, {}, "oops"]),
        "graveyard": rng.choice([[], [{"player_id": "NaN"}], None, "oops"]),
        "votes_today": rng.choice([0, 1, "2", None, "NaN"]),
        "locked_channel_ids": rng.choice([[1, "2", "x"], None, "oops", {}]),
        "lockdown_role_id": rng.choice([None, 123, "123", "NaN", {}]),
        "tribunal_muted": rng.choice([True, False, "false", "true", 0, 1, None]),
        "tribunal_defendant_id": rng.choice([None, 1, "2", "x", {}]),
        "stats_committed": rng.choice([True, False, "false", "true", 0, 1, None]),
    }

    # Corrupt harder sometimes.
    if rng.random() < 0.20:
        payload.pop(rng.choice(list(payload.keys())), None)
    if rng.random() < 0.10:
        # This is the most interesting corruption: missing guild_id entirely.
        payload.pop("guild_id", None)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Fuzz Game.from_persisted for corruption tolerance.")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--iterations", type=int, default=2000)
    args = ap.parse_args()

    rng = random.Random(int(args.seed))
    for i in range(int(args.iterations)):
        payload = _make_payload(rng)
        try:
            g = Game.from_persisted(payload)  # type: ignore[arg-type]
            # Roundtrip should never throw either.
            _ = g.to_persisted()
        except Exception as e:
            p = _write_repro(seed=int(args.seed), i=i, payload=payload, exc=e)
            print(f"WROTE REPRO: {p}")
            raise

    print(f"state_fuzz.py: OK iterations={args.iterations} seed={args.seed}")


if __name__ == "__main__":
    main()
