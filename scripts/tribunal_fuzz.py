from __future__ import annotations

"""
Tribunal/vote state-machine fuzz.

Goal: ensure tribunal-related state is restart-safe and resilient:
- game.vote_in_progress / tribunal_muted / tribunal_defendant_id should not get stuck
- loading/saving persisted state should tolerate weird values without crashing

This is not a Discord integration test; we use fake guild/channel objects and call
Game.from_persisted()/to_persisted() + light helpers.

Repro output: tests/repros_tribunal/
"""

import argparse
import json
import random
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game import Game  # noqa: E402


REPRO_DIR = ROOT / "tests" / "repros_tribunal"


def _write_repro(*, seed: int, i: int, payload: Dict[str, Any], exc: Exception) -> Path:
    REPRO_DIR.mkdir(parents=True, exist_ok=True)
    p = REPRO_DIR / f"tribunal_fuzz_seed{seed}_i{i}_{type(exc).__name__}.json"
    p.write_text(
        json.dumps(
            {
                "kind": "tribunal_fuzz",
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

    async def send(self, _msg: str) -> None:
        return

    async def add_roles(self, *_roles: object) -> None:
        return

    async def remove_roles(self, *_roles: object) -> None:
        return


class FakeChannel:
    async def set_permissions(self, *_args: object, **_kwargs: object) -> None:
        return


class FakeGuild:
    def __init__(self, members: Dict[int, FakeMember]) -> None:
        self.id = 123
        self._members = members
        self.members = list(members.values())

    def get_member(self, uid: int):
        return self._members.get(int(uid))

    async def fetch_member(self, uid: int):
        return self._members.get(int(uid))

    def get_role(self, _rid: int):
        return object()

    def get_channel(self, _cid: int):
        return FakeChannel()


def _randomize_tribunal_fields(rng: random.Random, data: Dict[str, Any]) -> None:
    # Corrupt tribunal fields to simulate interrupted tribunal + corrupted JSON.
    data["vote_in_progress"] = rng.choice([True, False, 0, 1, "false", None, {}, []])
    data["tribunal_muted"] = rng.choice([True, False, 0, 1, "true", "false", None, {}, []])
    data["tribunal_defendant_id"] = rng.choice([None, 1, 2, 999, "NaN", {}, [], -1])
    data["votes_today"] = rng.choice([0, 1, 2, 999, -5, "2", None])
    data["day_number"] = rng.choice([1, 2, 3, 0, -1, "3", None])
    data["phase"] = rng.choice(["day", "night", "setup", None, 123, "DAY"])


def _assert_tribunal_sanity(g: Game) -> None:
    # These should always be safe to access and stay in a small domain.
    assert isinstance(getattr(g, "votes_today", 0), int)
    assert getattr(g, "votes_today", 0) >= 0
    assert getattr(g, "tribunal_defendant_id", None) is None or isinstance(getattr(g, "tribunal_defendant_id", None), int)
    assert isinstance(getattr(g, "tribunal_muted", False), bool)
    assert isinstance(getattr(g, "vote_in_progress", False), bool)


def main() -> None:
    ap = argparse.ArgumentParser(description="Tribunal/vote state-machine fuzz.")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--iterations", type=int, default=2000)
    ap.add_argument("--players", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(int(args.seed))

    for i in range(int(args.iterations)):
        n = int(args.players)
        members = {pid: FakeMember(id=pid, display_name=f"P{pid}", roles=[], mention=f"<@{pid}>") for pid in range(1, n + 1)}
        guild = FakeGuild(members)

        g = Game(guild_id=guild.id)
        g.in_progress = True
        g.phase = "day"
        g.players = list(members.values())  # type: ignore[assignment]
        g.living_players = list(members.values())  # type: ignore[assignment]
        for pid in range(1, n + 1):
            g.player_roles[pid] = rng.choice(["Townie", "Doctor", "Mobster", "Witch", "Survivor", "Mayor"])
            g.role_states.setdefault(pid, {})

        payload: Dict[str, Any] = {"steps": [], "initial": deepcopy(g.to_persisted())}
        try:
            # Model some operations that happen during tribunal lifecycle.
            for _ in range(rng.randint(8, 25)):
                op = rng.choice(["mark_vote_in_progress", "set_defendant", "mute_toggle", "persist_roundtrip", "clear_all"])
                if op == "mark_vote_in_progress":
                    val = bool(rng.getrandbits(1))
                    g.vote_in_progress = val
                    payload["steps"].append({"op": op, "vote_in_progress": val})
                elif op == "set_defendant":
                    if rng.random() < 0.3:
                        g.tribunal_defendant_id = None
                        payload["steps"].append({"op": op, "defendant_id": None})
                    else:
                        did = rng.randint(1, n)
                        g.tribunal_defendant_id = did
                        payload["steps"].append({"op": op, "defendant_id": did})
                elif op == "mute_toggle":
                    val = bool(rng.getrandbits(1))
                    g.tribunal_muted = val
                    payload["steps"].append({"op": op, "tribunal_muted": val})
                elif op == "clear_all":
                    g.vote_in_progress = False
                    g.tribunal_muted = False
                    g.tribunal_defendant_id = None
                    payload["steps"].append({"op": op})
                else:
                    # Persist roundtrip, with optional corruption of tribunal fields.
                    data = g.to_persisted()
                    if rng.random() < 0.35:
                        _randomize_tribunal_fields(rng, data)
                    g2 = Game.from_persisted(data)
                    g = g2
                    g.in_progress = True
                    payload["steps"].append({"op": "persist_roundtrip", "persisted": deepcopy(data)})

                # Invariants should always hold.
                _assert_tribunal_sanity(g)
        except Exception as e:
            repro_path = _write_repro(seed=int(args.seed), i=i, payload=payload, exc=e)
            print(f"WROTE REPRO: {repro_path}")
            raise

    print(f"tribunal_fuzz.py: OK iterations={args.iterations} seed={args.seed} time={int(time.time())}")


if __name__ == "__main__":
    main()
