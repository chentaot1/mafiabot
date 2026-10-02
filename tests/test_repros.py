from __future__ import annotations

import json
from pathlib import Path

import pytest

from invariants import assert_post_night_pipeline_invariants, assert_repro_payload_shape
from scripts.sim_test import make_game, run_night_pipeline


ROOT = Path(__file__).resolve().parents[1]
REPRO_DIR = ROOT / "tests" / "repros"


def _repro_files() -> list[Path]:
    if not REPRO_DIR.exists():
        return []
    files = sorted([p for p in REPRO_DIR.glob("*.json") if p.is_file()])
    # Only replay repros that include a night_actions dict (engine repros).
    out: list[Path] = []
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("night_actions"), dict):
                out.append(p)
        except Exception:
            # Malformed repro should fail when/if it gets selected elsewhere; skip here.
            continue
    return out


@pytest.mark.parametrize("path", _repro_files(), ids=lambda p: p.name)
def test_repro_file_replays_without_violating_invariants(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "repro must be a dict payload"
    assert_repro_payload_shape(data)

    roles = data["roles"]
    assert isinstance(roles, list) and roles
    n = len(roles)
    game, guild, _members = make_game(seed=1, n=n)
    for seat, role in enumerate(roles, start=1):
        game.player_roles[seat] = role
        game.role_states.setdefault(seat, {})

    na = data.get("night_actions") or {}
    if isinstance(na, dict):
        game.night_actions = {int(k): v for k, v in na.items()}

    out = asyncio_run(run_night_pipeline(game, guild))
    assert_post_night_pipeline_invariants(game, out)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
