"""Shared pytest fixtures (Fake Discord members/guild, minimal Game factory)."""
from __future__ import annotations

import game as game_module


class FakeMember:
    def __init__(self, mid: int):
        self.id = mid
        self.display_name = f"P{mid}"
        self.dms: list[str] = []

    async def send(self, msg: str) -> None:
        self.dms.append(str(msg))


class FakeGuild:
    def __init__(self, members: list[FakeMember]):
        self._members = {m.id: m for m in members}

    def get_member(self, uid: int):
        return self._members.get(int(uid))

    async def fetch_member(self, uid: int):
        return self._members.get(int(uid))


def mk_game(
    members: list[FakeMember],
    roles: dict[int, str],
    role_states: dict[int, dict] | None = None,
    **kwargs,
) -> game_module.Game:
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = members  # type: ignore[assignment]
    g.living_players = members.copy()  # type: ignore[assignment]
    g.player_roles = roles
    g.role_states = role_states or {}
    g.night_actions = {}
    if "doused" in kwargs:
        g.doused_players = set(kwargs["doused"])
    return g

import pytest
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

@pytest.fixture(autouse=True)
def isolate_game_snapshots(monkeypatch, tmp_path):
    import persistence
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path / "state")
