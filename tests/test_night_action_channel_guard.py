from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import checks


@dataclass
class _FakeChannel:
    id: int
    overwrites = {}
    def permissions_for(self, member):
        from types import SimpleNamespace
        return SimpleNamespace(view_channel=member.id==111)


@dataclass
class _FakeGuild:
    id: int
    chunked = True
    @property
    def default_role(self):
        from types import SimpleNamespace
        return SimpleNamespace(id=0)
    @property
    def members(self):
        return [_FakeAuthor(111)]
    def get_member(self, uid):
        return _FakeAuthor(uid)


@dataclass
class _FakeAuthor:
    id: int


class _FakeCtx:
    def __init__(self, *, author_id: int, guild_id: Optional[int], channel_id: int) -> None:
        self.author = _FakeAuthor(author_id)
        self.guild = _FakeGuild(guild_id) if guild_id is not None else None
        self.channel = _FakeChannel(channel_id)
        self.channel.guild = self.guild
        self.sent: list[str] = []

    async def send(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.sent.append(str(msg))


class _FakeBot:
    def get_guild(self, _gid: int):
        return None


class _FakeGame:
    def __init__(self, *, guild_id: int, in_progress: bool = True, phase: str = "night", resolving: bool = False) -> None:
        self.guild_id = guild_id
        self.in_progress = in_progress
        self.phase = phase
        self.resolving = resolving
        # DM path relies on cached living_players; include the author as living.
        self.living_players = [_FakeAuthor(111)]

    async def sync_living_players(self, _guild) -> None:
        return None

    async def get_living_ids(self, _guild) -> list[int]:
        return [111]


async def _run_wrapper(
    *,
    ctx: _FakeCtx,
    game: Optional[_FakeGame],
    mapping: dict[int, int],
) -> tuple[bool, list[str]]:
    old_map = dict(checks.PLAYER_PRIVATE_CHANNEL_IDS)
    checks.PLAYER_PRIVATE_CHANNEL_IDS.clear()
    checks.PLAYER_PRIVATE_CHANNEL_IDS.update(mapping)

    called = False

    async def handler(_ctx: Any) -> None:
        nonlocal called
        called = True

    def get_game_by_player_id(_uid: int) -> Optional[_FakeGame]:
        return game

    try:
        dec = checks.only_during_night_gameplay(bot=_FakeBot(), get_game_by_player_id=get_game_by_player_id)
        wrapped = dec(handler)
        await wrapped(ctx)
        return called, list(ctx.sent)
    finally:
        checks.PLAYER_PRIVATE_CHANNEL_IDS.clear()
        checks.PLAYER_PRIVATE_CHANNEL_IDS.update(old_map)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_guard_rejects_when_no_game_found() -> None:
    ctx = _FakeCtx(author_id=111, guild_id=123, channel_id=999)
    called, sent = _run(_run_wrapper(ctx=ctx, game=None, mapping={111: 999}))
    assert called is False
    assert any("No active game found" in m for m in sent)

def test_guard_rejects_dm() -> None:
    ctx = _FakeCtx(author_id=111, guild_id=None, channel_id=999)
    game = _FakeGame(guild_id=123)
    called, sent = _run(_run_wrapper(ctx=ctx, game=game, mapping={111: 999}))
    assert called is True
    assert sent == []

def test_guard_rejects_missing_mapping() -> None:
    ctx = _FakeCtx(author_id=111, guild_id=123, channel_id=999)
    game = _FakeGame(guild_id=123)
    called, sent = _run(_run_wrapper(ctx=ctx, game=game, mapping={}))
    assert called is False
    assert any("isn't configured" in m for m in sent)

def test_guard_rejects_wrong_channel() -> None:
    ctx = _FakeCtx(author_id=111, guild_id=123, channel_id=555)
    game = _FakeGame(guild_id=123)
    called, sent = _run(_run_wrapper(ctx=ctx, game=game, mapping={111: 999}))
    assert called is False
    assert any("Use your private channel" in m for m in sent)

def test_guard_allows_correct_channel() -> None:
    ctx = _FakeCtx(author_id=111, guild_id=123, channel_id=999)
    game = _FakeGame(guild_id=123)
    called, sent = _run(_run_wrapper(ctx=ctx, game=game, mapping={111: 999}))
    assert called is True
    assert sent == []

def test_guard_rejects_wrong_guild() -> None:
    ctx = _FakeCtx(author_id=111, guild_id=999, channel_id=999)
    game = _FakeGame(guild_id=123)
    called, sent = _run(_run_wrapper(ctx=ctx, game=game, mapping={111: 999}))
    assert called is False
    assert any("different server" in m for m in sent)
