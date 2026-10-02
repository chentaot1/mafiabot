from __future__ import annotations

"""
Minimize fuzz repro JSON while preserving the same exception type.

Exception messages may change slightly while pruning; if minimization stalls, the
harness only requires matching `type(e).__name__`.

- state_fuzz / raw dict: Game.from_persisted(payload)
- rehydrate_fuzz: rehydrate_members + sync_living_players (see repro_replay_helpers)
- phase_fuzz: replay step list (see repro_replay_helpers)
- tribunal_fuzz: replay initial + structured steps (see repro_replay_helpers)
"""

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from repro_replay_helpers import (  # noqa: E402
    run_phase_fuzz_steps,
    run_phase_repro_payload,
    run_rehydrate_fuzz_payload,
    run_state_fuzz_payload,
    run_tribunal_fuzz_payload,
)


def _fail(fn: Callable[[], None]) -> Tuple[bool, str]:
    try:
        fn()
        return (False, "")
    except Exception as e:
        return (True, type(e).__name__)


def _minimize_dict_payload(payload: Dict[str, Any], base_type: str, runner: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    best = deepcopy(payload)

    changed = True
    while changed:
        changed = False
        for k in list(best.keys()):
            candidate = deepcopy(best)
            candidate.pop(k, None)
            f, t = _fail(lambda c=candidate: runner(c))
            if f and t == base_type:
                best = candidate
                changed = True
                break

    def _simplify(v: Any) -> Any:
        if isinstance(v, dict):
            return {}
        if isinstance(v, list):
            return []
        if isinstance(v, str):
            return ""
        if isinstance(v, bool):
            return False
        if isinstance(v, (int, float)):
            return 0
        return None

    for k in list(best.keys()):
        candidate = deepcopy(best)
        candidate[k] = _simplify(candidate.get(k))
        f, t = _fail(lambda c=candidate: runner(c))
        if f and t == base_type:
            best = candidate

    return best


def _minimize_phase_payload(payload: Dict[str, Any], base_type: str) -> Dict[str, Any]:
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return payload
    best_steps: List[Any] = list(steps)

    changed = True
    while changed and len(best_steps) > 0:
        changed = False
        for i in range(len(best_steps) - 1, -1, -1):
            cand = best_steps[:i] + best_steps[i + 1 :]
            if isinstance(payload.get("initial"), dict):
                p2 = dict(payload)
                p2["steps"] = cand
                f, t = _fail(lambda pl=p2: run_phase_repro_payload(pl))
            else:
                f, t = _fail(lambda s=cand: run_phase_fuzz_steps(s))
            if f and t == base_type:
                best_steps = cand
                changed = True
                break

    out = dict(payload)
    out["steps"] = best_steps
    return out


def _minimize_tribunal_payload(payload: Dict[str, Any], base_type: str) -> Dict[str, Any]:
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return payload
    best_steps: List[Any] = list(steps)

    changed = True
    while changed and len(best_steps) > 0:
        changed = False
        for i in range(len(best_steps) - 1, -1, -1):
            cand = best_steps[:i] + best_steps[i + 1 :]
            p2 = dict(payload)
            p2["steps"] = cand
            f, t = _fail(lambda pl=p2: run_tribunal_fuzz_payload(pl))
            if f and t == base_type:
                best_steps = cand
                changed = True
                break

    out = dict(payload)
    out["steps"] = best_steps
    return out


def _minimize_rehydrate_payload(payload: Dict[str, Any], base_type: str) -> Dict[str, Any]:
    return _minimize_dict_payload(payload, base_type, run_rehydrate_fuzz_payload)


def main() -> None:
    ap = argparse.ArgumentParser(description="Minimize fuzz repros by preserving exception type.")
    ap.add_argument("path", type=str, help="Path to a repro JSON")
    ap.add_argument("--in-place", action="store_true", help="Rewrite the file in-place if minimized.")
    args = ap.parse_args()

    path = Path(args.path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("Repro is not a dict JSON object.")

    kind = str(data.get("kind", ""))
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
    if not isinstance(payload, dict):
        raise SystemExit("No dict payload to minimize.")

    if kind == "phase_fuzz":
        failed, base_type = _fail(lambda: run_phase_repro_payload(payload))
        minimizer = _minimize_phase_payload
    elif kind == "rehydrate_fuzz":
        failed, base_type = _fail(lambda: run_rehydrate_fuzz_payload(payload))
        minimizer = _minimize_rehydrate_payload
    elif kind == "tribunal_fuzz":
        failed, base_type = _fail(lambda: run_tribunal_fuzz_payload(payload))
        minimizer = _minimize_tribunal_payload
    else:
        failed, base_type = _fail(lambda: run_state_fuzz_payload(payload))

        def _min_state(p: Dict[str, Any], bt: str) -> Dict[str, Any]:
            return _minimize_dict_payload(p, bt, run_state_fuzz_payload)

        minimizer = _min_state

    if not failed:
        print("minimize_state_like_repro: payload no longer fails; nothing to minimize.")
        return

    best_payload = minimizer(payload, base_type)
    if best_payload == payload:
        print("minimize_state_like_repro: no minimization found.")
        return

    if "payload" in data and isinstance(data.get("payload"), dict):
        data2 = dict(data)
        data2["payload"] = best_payload
    else:
        data2 = best_payload

    if args.in_place:
        path.write_text(json.dumps(data2, indent=2, sort_keys=True), encoding="utf-8")
        print(f"minimize_state_like_repro: minimized in-place: {path}")
    else:
        print(json.dumps(data2, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
