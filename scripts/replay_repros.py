from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure repo root importable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sim_test import make_game, run_night_pipeline, assert_post_night_invariants  # noqa: E402


async def _replay_one(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    roles = data.get("roles")
    if not isinstance(roles, list) or not roles:
        raise RuntimeError(f"{path}: missing roles")

    n = len(roles)
    game, guild, _members = make_game(seed=1, n=n)
    for seat, role in enumerate(roles, start=1):
        game.player_roles[seat] = role
        game.role_states.setdefault(seat, {})

    na = data.get("night_actions") or {}
    game.night_actions = {int(k): v for k, v in na.items()}
    out = await run_night_pipeline(game, guild)
    assert_post_night_invariants(game, out)


def main() -> None:
    import asyncio

    repro_dir = ROOT / "tests" / "repros"
    if not repro_dir.exists():
        print("No repro directory found.")
        return

    files = sorted([p for p in repro_dir.glob("*.json") if p.is_file()])
    if not files:
        print("No repro files found.")
        return

    async def _run_all() -> None:
        for p in files:
            await _replay_one(p)
            print(f"replayed: {p.name}")

    asyncio.run(_run_all())
    print(f"replay_repros.py: OK  count={len(files)}")


if __name__ == "__main__":
    main()
