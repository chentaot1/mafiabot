from __future__ import annotations

"""
Fuzz restart rehydration: Game.from_persisted() -> rehydrate_members() -> sync_living_players().

This targets "bot restarts mid-game" crashers where persisted IDs/structures are odd
and guild lookups may return None / NotFound.

On failure it writes a repro JSON under tests/repros_rehydrate/.
"""

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import discord


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game import Game  # noqa: E402


REPRO_DIR = ROOT / "tests" / "repros_rehydrate"


def _write_repro(*, seed: int, i: int, payload: Dict[str, Any], exc: Exception) -> Path:
    REPRO_DIR.mkdir(parents=True, exist_ok=True)
    p = REPRO_DIR / f"rehydrate_fuzz_seed{seed}_i{i}_{type(exc).__name__}.json"
    p.write_text(
        json.dumps(
            {
                "kind": "rehydrate_fuzz",
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
class FakeRole:
    id: int
    name: str = "Alive"


@dataclass
class FakeMember:
    id: int
    display_name: str
    roles: list[object] = field(default_factory=list)

    async def add_roles(self, *_roles: object) -> None:
        # no-op
        return


class FakeGuild:
    def __init__(self, members: Dict[int, FakeMember]) -> None:
        self._members = members
        self._alive_role = FakeRole(111, "Alive")

    def get_member(self, uid: int) -> Optional[FakeMember]:
        return self._members.get(int(uid))

    async def fetch_member(self, uid: int) -> FakeMember:
        m = self.get_member(uid)
        if m is None:
            class _DummyResp:
                status = 404
                reason = "Not Found"

            raise discord.NotFound(response=_DummyResp(), message="Member not found")  # type: ignore[arg-type]
        return m

    def get_role(self, role_id: Optional[int]):
        if role_id == self._alive_role.id:
            return self._alive_role
        return None


def _rand_scalar(rng: random.Random) -> Any:
    return rng.choice([None, True, False, 0, 1, -1, 3.14, "", "0", "false", "true", "NaN", [], {}, {"x": 1}])


def _make_payload(rng: random.Random) -> Dict[str, Any]:
    # Minimal-ish persisted shape with corruption.
    payload: Dict[str, Any] = {
        "guild_id": rng.choice([123, "123", "NaN", None]),
        "in_progress": rng.choice([True, False, "true", "false", 1, 0, None]),
        "phase": rng.choice(["night", "day", None, 7]),
        "day_number": rng.choice([0, 1, "2", "NaN", None]),
        "player_ids": rng.choice([[1, 2, 3], ["1", "x", 2], None, 5, []]),
        "living_ids": rng.choice([[1, 2], ["2", "oops"], None, {}]),
        "player_roles": rng.choice([{"1": "Doctor", "2": "Mobster", "x": "Townie"}, None, 1]),
        "night_actions": rng.choice([{"1": {"type": "heal", "actor": 1, "target": 1}}, None, 1]),
        "role_states": rng.choice([{"1": {"self_heals_remaining": 1}}, None, 1]),
        "graveyard": rng.choice([[], [{"player_id": "2", "real_role": "Townie"}, {"player_id": "NaN"}], "oops"]),
        "alive_role_id": rng.choice([111, "111", None, "NaN"]),
    }

    # Occasionally inject a totally broken night_actions value entry.
    if isinstance(payload.get("night_actions"), dict) and rng.random() < 0.2:
        payload["night_actions"]["2"] = rng.choice(["oops", 1, None, {"type": "kill", "actor": 2, "target": 1}])

    # Occasionally drop required-ish keys.
    if rng.random() < 0.15:
        payload.pop(rng.choice(list(payload.keys())), None)

    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Fuzz rehydrate_members/sync_living_players restart paths.")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--iterations", type=int, default=2000)
    args = ap.parse_args()

    rng = random.Random(int(args.seed))

    # Members 1..3 exist; others will be NotFound.
    members = {i: FakeMember(i, f"P{i}") for i in range(1, 4)}
    guild = FakeGuild(members)

    import asyncio

    for i in range(int(args.iterations)):
        payload = _make_payload(rng)
        try:
            g = Game.from_persisted(payload)  # type: ignore[arg-type]
            # Rehydrate + sync should never throw.
            asyncio.run(g.rehydrate_members(guild))  # type: ignore[arg-type]
            g.in_progress = True
            g.alive_role_id = 111
            asyncio.run(g.sync_living_players(guild))  # type: ignore[arg-type]
        except Exception as e:
            p = _write_repro(seed=int(args.seed), i=i, payload=payload, exc=e)
            print(f"WROTE REPRO: {p}")
            raise

    print(f"rehydrate_fuzz.py: OK iterations={args.iterations} seed={args.seed}")


if __name__ == "__main__":
    main()
