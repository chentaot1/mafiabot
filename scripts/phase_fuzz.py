from __future__ import annotations

"""
State-machine style fuzz for phase transitions and idempotency:
- start_night/start_day should be safe under repeats
- resolve-related guards shouldn't crash with odd internal state

This uses lightweight fake ctx/guild/member objects and focuses on "no crash" +
basic invariants, not perfect ToS gameplay.

Repro output: tests/repros_phase/
"""

import argparse
import json
import random
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game import Game  # noqa: E402
from invariants import assert_post_phase_transition_invariants  # noqa: E402


REPRO_DIR = ROOT / "tests" / "repros_phase"


def _write_repro(*, seed: int, i: int, payload: Dict[str, Any], exc: Exception) -> Path:
    REPRO_DIR.mkdir(parents=True, exist_ok=True)
    p = REPRO_DIR / f"phase_fuzz_seed{seed}_i{i}_{type(exc).__name__}.json"
    p.write_text(
        json.dumps(
            {
                "kind": "phase_fuzz",
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


@dataclass
class FakeMember:
    id: int
    display_name: str
    roles: list[object]
    mention: str
    voice: None = None

    async def send(self, _msg: str) -> None:
        return

    async def add_roles(self, *_roles: object) -> None:
        return

    async def remove_roles(self, *_roles: object) -> None:
        return


class FakeGuild:
    def __init__(self, members: Dict[int, FakeMember]) -> None:
        self.id = 123
        self._members = members
        self.members = list(members.values())
        self.roles = []
        self.default_role = object()

    def get_member(self, uid: int):
        return self._members.get(int(uid))

    async def fetch_member(self, uid: int):
        return self._members.get(int(uid))

    def get_role(self, _rid: int):
        return None

    def get_channel(self, _cid: int):
        return None


class FakeCtx:
    def __init__(self, guild: FakeGuild) -> None:
        self.guild = guild

    async def send(self, _msg: str) -> None:
        return


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase transition fuzz (day/night idempotency + no-crash).")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--iterations", type=int, default=2000)
    ap.add_argument("--players", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(int(args.seed))

    # Avoid file-lock collisions in the real state dir when fuzzing.
    import tempfile
    import persistence as p  # type: ignore

    old_state_dir = p.STATE_DIR
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        p.STATE_DIR = Path(td)  # type: ignore[assignment]

        try:
            for i in range(int(args.iterations)):
                n = int(args.players)
                members = {
                    pid: FakeMember(id=pid, display_name=f"P{pid}", roles=[], mention=f"<@{pid}>")
                    for pid in range(1, n + 1)
                }
                guild = FakeGuild(members)
                ctx = FakeCtx(guild)
                g = Game(guild_id=guild.id)
                g.in_progress = True
                g.players = list(members.values())  # type: ignore[assignment]
                g.living_players = list(members.values())  # type: ignore[assignment]

                # Minimal roles so start_night/start_day can run.
                for pid in range(1, n + 1):
                    g.player_roles[pid] = rng.choice(["Townie", "Doctor", "Mobster", "Witch", "Survivor"])
                    g.role_states.setdefault(pid, {})

                # Randomly exercise idempotent transitions (initial snapshot for faithful replay).
                payload: Dict[str, Any] = {"steps": [], "initial": deepcopy(g.to_persisted())}
                try:
                    import asyncio

                    for _step in range(rng.randint(5, 20)):
                        op = rng.choice(["start_night", "start_day", "persist_roundtrip"])
                        payload["steps"].append(op)
                        snap = {
                            "day_number": getattr(g, "day_number", None),
                            "votes_today": getattr(g, "votes_today", None),
                            "players": list(getattr(g, "players", []) or []),
                        }
                        if op == "start_night":
                            asyncio.run(g.start_night(ctx))  # type: ignore[arg-type]
                            assert_post_phase_transition_invariants(snap, g)
                        elif op == "start_day":
                            asyncio.run(g.start_day(ctx))  # type: ignore[arg-type]
                            assert_post_phase_transition_invariants(snap, g)
                        else:
                            # persist -> from_persisted -> restart (no members)
                            data = g.to_persisted()
                            g2 = Game.from_persisted(data)
                            g = g2
                            g.in_progress = True
                            assert_post_phase_transition_invariants(snap, g)
                except Exception as e:
                    repro_path = _write_repro(seed=int(args.seed), i=i, payload=payload, exc=e)
                    print(f"WROTE REPRO: {repro_path}")
                    raise

            print(f"phase_fuzz.py: OK iterations={args.iterations} seed={args.seed}")
        finally:
            p.STATE_DIR = old_state_dir  # type: ignore[assignment]


if __name__ == "__main__":
    main()
