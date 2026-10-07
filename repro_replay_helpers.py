from __future__ import annotations

"""
Replay helpers for fuzz repro JSON (phase / rehydrate / tribunal).
Used by tests and scripts/minimize_state_like_repro.py to avoid drift.
"""

import asyncio
import tempfile
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import discord

from game import Game

# Serialize phase replays: they temporarily repoint persistence.STATE_DIR.
_PHASE_REPLAY_LOCK = threading.Lock()

# --- Phase fuzz fakes (match tests/test_phase_repros.py) ---


@dataclass
class _PhaseMember:
    id: int
    display_name: str
    roles: list[object]
    mention: str
    voice: None = None

    async def send(self, _msg: str, **kwargs) -> None:
        return

    async def add_roles(self, *_roles: object) -> None:
        return


class _PhaseGuild:
    def __init__(self, members: Dict[int, _PhaseMember]) -> None:
        self.id = 123
        self._members = members
        self.members = list(members.values())
        self.default_role = object()
        self.roles = []

    def get_member(self, uid: int):
        return self._members.get(int(uid))

    async def fetch_member(self, uid: int):
        return self._members.get(int(uid))

    def get_role(self, _rid: int):
        return None

    def get_channel(self, _cid: int):
        return None


class _PhaseCtx:
    def __init__(self, guild: _PhaseGuild) -> None:
        self.guild = guild

    async def send(self, _msg: str, **kwargs) -> None:
        return


def _phase_attach_members_and_ctx(g: Game) -> _PhaseCtx:
    """Attach fake Discord members consistent with persisted player_roles / living_ids."""
    pids = sorted(g.player_roles.keys())
    if not pids:
        pids = list(range(1, 8))
    members = {
        pid: _PhaseMember(id=pid, display_name=f"P{pid}", roles=[], mention=f"<@{pid}>") for pid in pids
    }
    living_src = list(getattr(g, "_persist_living_ids", []) or [])
    living_set = set(living_src) if living_src else set(pids)
    g.players = [members[pid] for pid in pids]  # type: ignore[assignment]
    g.living_players = [members[pid] for pid in pids if pid in living_set]  # type: ignore[assignment]
    guild = _PhaseGuild(members)
    return _PhaseCtx(guild)


def run_phase_fuzz_steps(steps: List[Any]) -> None:
    """Replay legacy phase_fuzz steps (7x Townie); raises on crash."""
    import persistence as p

    with _PHASE_REPLAY_LOCK:
        old_state_dir = p.STATE_DIR
        import game as gm
        old_games, old_bot = gm.active_games, gm._BOT
        gm.active_games, gm._BOT = {}, None
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                p.STATE_DIR = Path(td)  # type: ignore[assignment]

                members = {
                    pid: _PhaseMember(id=pid, display_name=f"P{pid}", roles=[], mention=f"<@{pid}>")
                    for pid in range(1, 8)
                }
                guild = _PhaseGuild(members)
                ctx = _PhaseCtx(guild)
                g = Game(guild_id=guild.id)
                g.in_progress = True
                gm.active_games[guild.id] = g
                g.players = list(members.values())  # type: ignore[assignment]
                g.living_players = list(members.values())  # type: ignore[assignment]
                for pid in range(1, 8):
                    g.player_roles[pid] = "Townie"
                    g.role_states.setdefault(pid, {})

                for op in steps:
                    if op == "start_night":
                        asyncio.run(g.start_night(ctx))  # type: ignore[arg-type]
                    elif op == "start_day":
                        asyncio.run(g.start_day(ctx))  # type: ignore[arg-type]
                    elif op == "persist_roundtrip":
                        g2 = Game.from_persisted(g.to_persisted())
                        g = g2
                        g.in_progress = True
                        ctx = _phase_attach_members_and_ctx(g)
                        gm.active_games[guild.id] = g
                    else:
                        raise AssertionError(f"Unknown phase step: {op!r}")
        finally:
            p.STATE_DIR = old_state_dir  # type: ignore[assignment]
            gm.active_games, gm._BOT = old_games, old_bot


def run_phase_fuzz_payload(payload: Dict[str, Any]) -> None:
    """Replay phase_fuzz with the same initial persisted snapshot as the fuzzer (roles, counts)."""
    import persistence as p

    initial = payload.get("initial")
    steps = payload.get("steps", [])
    if not isinstance(initial, dict):
        raise ValueError("phase repro missing initial persisted dict")
    if not isinstance(steps, list):
        raise TypeError("steps must be a list")

    with _PHASE_REPLAY_LOCK:
        old_state_dir = p.STATE_DIR
        import game as gm
        old_games, old_bot = gm.active_games, gm._BOT
        gm.active_games, gm._BOT = {}, None
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                p.STATE_DIR = Path(td)  # type: ignore[assignment]
                g = Game.from_persisted(deepcopy(initial))
                ctx = _phase_attach_members_and_ctx(g)
                g.in_progress = True
                gm.active_games[ctx.guild.id] = g

                for op in steps:
                    if op == "start_night":
                        asyncio.run(g.start_night(ctx))  # type: ignore[arg-type]
                    elif op == "start_day":
                        asyncio.run(g.start_day(ctx))  # type: ignore[arg-type]
                    elif op == "persist_roundtrip":
                        g2 = Game.from_persisted(g.to_persisted())
                        g = g2
                        g.in_progress = True
                        ctx = _phase_attach_members_and_ctx(g)
                        gm.active_games[ctx.guild.id] = g
                    else:
                        raise AssertionError(f"Unknown phase step: {op!r}")
        finally:
            p.STATE_DIR = old_state_dir  # type: ignore[assignment]
            gm.active_games, gm._BOT = old_games, old_bot


def run_phase_repro_payload(payload: Dict[str, Any]) -> None:
    """Dispatch: new repros with `initial`, else legacy steps-only replay."""
    if isinstance(payload.get("initial"), dict):
        run_phase_fuzz_payload(payload)
    else:
        run_phase_fuzz_steps(list(payload.get("steps", [])))


# --- Rehydrate fakes (match tests/test_rehydrate_repros.py) ---


@dataclass
class _ReRole:
    id: int
    name: str = "Alive"


@dataclass
class _ReMember:
    id: int
    display_name: str
    roles: list[object] = field(default_factory=list)

    async def add_roles(self, *_roles: object) -> None:
        return


class _ReGuild:
    def __init__(self, members: Dict[int, _ReMember]) -> None:
        self._members = members
        self._alive_role = _ReRole(111, "Alive")

    def get_member(self, uid: int) -> Optional[_ReMember]:
        return self._members.get(int(uid))

    async def fetch_member(self, uid: int) -> _ReMember:
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


def _rehydrate_member_ids(payload: Dict[str, Any]) -> List[int]:
    ids: set[int] = set()
    for key in ("player_ids", "living_ids"):
        lst = payload.get(key)
        if isinstance(lst, list):
            for x in lst:
                try:
                    ids.add(int(x))
                except (TypeError, ValueError):
                    continue
    pr = payload.get("player_roles")
    if isinstance(pr, dict):
        for k in pr:
            try:
                ids.add(int(k))
            except (TypeError, ValueError):
                continue
    if not ids:
        return [1, 2, 3]
    return sorted(ids)


def run_rehydrate_fuzz_payload(payload: Dict[str, Any]) -> None:
    g = Game.from_persisted(payload)  # type: ignore[arg-type]
    members = {i: _ReMember(i, f"P{i}") for i in _rehydrate_member_ids(payload)}
    guild = _ReGuild(members)
    asyncio.run(g.rehydrate_members(guild))  # type: ignore[arg-type]
    g.in_progress = True
    g.alive_role_id = 111
    asyncio.run(g.sync_living_players(guild))  # type: ignore[arg-type]


# --- Tribunal fakes (match scripts/tribunal_fuzz.py) ---


@dataclass
class _TrMember:
    id: int
    display_name: str
    roles: list[object]
    mention: str

    async def send(self, _msg: str, **kwargs) -> None:
        return

    async def add_roles(self, *_roles: object) -> None:
        return

    async def remove_roles(self, *_roles: object) -> None:
        return


class _TrChannel:
    async def set_permissions(self, *_args: object, **_kwargs: object) -> None:
        return


class _TrGuild:
    def __init__(self, members: Dict[int, _TrMember]) -> None:
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
        return _TrChannel()


def assert_tribunal_sanity(g: Game) -> None:
    assert isinstance(getattr(g, "votes_today", 0), int)
    assert getattr(g, "votes_today", 0) >= 0
    assert getattr(g, "tribunal_defendant_id", None) is None or isinstance(getattr(g, "tribunal_defendant_id", None), int)
    assert isinstance(getattr(g, "tribunal_muted", False), bool)
    assert isinstance(getattr(g, "vote_in_progress", False), bool)


def attach_fake_players_from_persisted(g: Game) -> None:
    pids = sorted(g.player_roles.keys())
    members = {pid: _TrMember(id=pid, display_name=f"P{pid}", roles=[], mention=f"<@{pid}>") for pid in pids}
    living_src = list(getattr(g, "_persist_living_ids", []) or [])
    if living_src:
        living_set = set(living_src)
    else:
        living_set = set(pids)
    g.players = [members[pid] for pid in pids]  # type: ignore[assignment]
    g.living_players = [members[pid] for pid in pids if pid in living_set]  # type: ignore[assignment]


TribunalStep = Union[str, Dict[str, Any]]


def _replay_one_tribunal_step(g: Game, step: TribunalStep) -> Game:
    if isinstance(step, str):
        # Legacy repros: cannot replay random outcomes; no-op (may desync).
        return g
    op = step.get("op")
    if op == "mark_vote_in_progress":
        g.vote_in_progress = bool(step["vote_in_progress"])
    elif op == "set_defendant":
        g.tribunal_defendant_id = step.get("defendant_id")
    elif op == "mute_toggle":
        g.tribunal_muted = bool(step["tribunal_muted"])
    elif op == "clear_all":
        g.vote_in_progress = False
        g.tribunal_muted = False
        g.tribunal_defendant_id = None
    elif op == "persist_roundtrip":
        blob = step.get("persisted")
        if not isinstance(blob, dict):
            raise TypeError("persist_roundtrip step missing persisted dict")
        g2 = Game.from_persisted(deepcopy(blob))
        attach_fake_players_from_persisted(g2)
        g2.in_progress = True
        return g2
    else:
        raise AssertionError(f"Unknown tribunal step: {step!r}")
    return g


def run_tribunal_fuzz_payload(payload: Dict[str, Any]) -> None:
    initial = payload.get("initial")
    steps = payload.get("steps", [])
    if not isinstance(initial, dict):
        raise ValueError("tribunal repro missing initial persisted dict")
    if not isinstance(steps, list):
        raise TypeError("steps must be a list")

    g = Game.from_persisted(deepcopy(initial))
    attach_fake_players_from_persisted(g)
    g.in_progress = True

    for step in steps:
        g = _replay_one_tribunal_step(g, step)
        assert_tribunal_sanity(g)


def run_state_fuzz_payload(payload: Dict[str, Any]) -> None:
    Game.from_persisted(payload)
