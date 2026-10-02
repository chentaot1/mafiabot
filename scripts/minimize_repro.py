from __future__ import annotations

"""
Minimize an engine repro JSON from tests/repros/*.json.

Strategy (best-effort, fast):
- Remove actions one-by-one if failure still reproduces
- Then simplify common fields (target/targets) if still reproduces

This is intentionally conservative: it only rewrites the repro file if it can
confirm the minimized payload still reproduces the same failure type.
"""

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from invariants import assert_post_night_pipeline_invariants, assert_repro_payload_shape  # noqa: E402
from scripts.sim_test import make_game, run_night_pipeline  # noqa: E402


def _run_payload(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Returns (failed, failure_type).
    If it throws: failed=True, type=Exception class name
    If it passes: failed=False, type=""
    """
    roles = payload["roles"]
    n = len(roles)
    game, guild, _members = make_game(seed=1, n=n)
    for seat, role in enumerate(roles, start=1):
        game.player_roles[seat] = role
        game.role_states.setdefault(seat, {})

    na = payload.get("night_actions") or {}
    if isinstance(na, dict):
        game.night_actions = {int(k): v for k, v in na.items()}

    import asyncio

    try:
        out = asyncio.run(run_night_pipeline(game, guild))
        assert_post_night_pipeline_invariants(game, out)
        return (False, "")
    except Exception as e:
        return (True, type(e).__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="Minimize a tests/repros engine repro JSON.")
    ap.add_argument("path", type=str)
    args = ap.parse_args()

    path = Path(args.path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert_repro_payload_shape(data)
    if not isinstance(data.get("night_actions"), dict):
        raise SystemExit("This repro has no night_actions dict; not an engine repro.")

    # Baseline: must currently fail.
    failed, base_type = _run_payload(data)
    if not failed:
        print("minimize_repro: repro no longer fails; nothing to minimize.")
        return

    best = deepcopy(data)

    # Pass 1: remove actions.
    changed = True
    while changed:
        changed = False
        keys = list((best.get("night_actions") or {}).keys())
        for k in keys:
            candidate = deepcopy(best)
            na = dict(candidate.get("night_actions") or {})
            na.pop(k, None)
            candidate["night_actions"] = na
            f, t = _run_payload(candidate)
            if f and t == base_type:
                best = candidate
                changed = True
                break

    # Pass 2: simplify target/targets shapes.
    def _simplify_action(act: Any) -> Any:
        if not isinstance(act, dict):
            return act
        a = dict(act)
        if "targets" in a:
            a["targets"] = []
        if "target" in a:
            a["target"] = None
        return a

    na0 = best.get("night_actions") or {}
    if isinstance(na0, dict):
        for k, v in list(na0.items()):
            candidate = deepcopy(best)
            na = dict(candidate.get("night_actions") or {})
            na[k] = _simplify_action(na.get(k))
            candidate["night_actions"] = na
            f, t = _run_payload(candidate)
            if f and t == base_type:
                best = candidate

    if best != data:
        path.write_text(json.dumps(best, indent=2, sort_keys=True), encoding="utf-8")
        print(f"minimize_repro: minimized in-place: {path}")
    else:
        print("minimize_repro: no minimization found.")


if __name__ == "__main__":
    main()
