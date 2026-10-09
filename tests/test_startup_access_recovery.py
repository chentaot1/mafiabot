"""Unavailable server objects keep startup and access recovery incomplete."""
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import pytest

import game as gm
import persistence
from game import Game
from config import PLAYING_ROLE_ID
from gameplay import access, startup, state as st
from test_gameplay import world, Channel


class Principal:
    def __init__(self, uid):
        self.id = uid
        self.display_name, self.mention, self.bot = f"Player {uid}", f"<@{uid}>", False
        self.roles = []
        self.guild_permissions = S(administrator=False)
        self.permissions = S(administrator=False)
        self.add_roles, self.remove_roles = AsyncMock(), AsyncMock()
        self.send = AsyncMock()


def configure(world):
    game, guild, _, channels, _ = world
    members = [Principal(uid) for uid in (1, 2, 3, 4)]
    game.players, game.living_players = members[:], members[:]
    game.player_roles[1] = "Mobster"
    game.lockdown_role_id, game.mafia_tc_id = 30, 11
    game.grave_tc_id, game.grave_vc_id = 12, 13
    roles = {uid: Principal(uid) for uid in (0, game.alive_role_id, PLAYING_ROLE_ID, 30, 42)}
    guild.get_role = roles.get
    guild.get_member = lambda uid: next((member for member in members if member.id == uid), None)
    guild.me, guild.default_role, guild.members = Principal(999), roles[0], members
    for cid in (12, 13):
        channels[cid] = Channel(cid, guild)
    for channel in channels.values():
        channel.edit = AsyncMock()
    game.gameplay["startup"] = {"match": game.game_key, "complete": False,
        "announced": False, "mafia_announced": False, "completed_players": []}
    persistence.save_state(game.guild_id, game.to_persisted())
    return game, guild, channels, members, roles


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("missing", ["alive", "playing", "lockdown", "mafia", "guild"])
async def test_startup_waits_for_required_access_then_resumes_assignment(world, missing, restart):
    game, guild, channels, members, roles = configure(world)
    expected = dict(game.player_roles)
    if missing in {"alive", "playing", "lockdown"}:
        rid = {"alive": game.alive_role_id, "playing": PLAYING_ROLE_ID, "lockdown": 30}[missing]
        unavailable = roles.pop(rid)
    elif missing == "mafia":
        unavailable = channels.pop(game.mafia_tc_id)
    with pytest.raises(OSError):
        await startup.deliver(game, None if missing == "guild" else guild, client=S(db=None))
    assert not game.gameplay["startup"]["complete"]
    assert game.gameplay["startup"]["completed_players"] == []
    for member in members:
        member.send.assert_not_awaited()
        member.add_roles.assert_not_awaited()
    with pytest.raises(st.Rejected):
        st.require_current(game)
    if missing in {"alive", "playing", "lockdown"}:
        roles[rid] = unavailable
    elif missing == "mafia":
        channels[game.mafia_tc_id] = unavailable
    if restart:
        game = Game.from_persisted(persistence.load_state(game.guild_id))
        await game.rehydrate_members(guild)
        gm.active_games[game.guild_id] = game
    await startup.deliver(game, guild, client=S(db=None))
    assert game.gameplay["startup"]["complete"]
    assert game.player_roles == expected
    assert game.gameplay["startup"]["completed_players"] == [1, 2, 3, 4]
    for member in members:
        assert member.send.await_count >= 1
    channels[11].set_permissions.assert_awaited_once_with(members[0], view_channel=True, send_messages=True)
    sends = [member.send.await_count for member in members]
    await startup.deliver(game, guild, client=S(db=None))
    assert [member.send.await_count for member in members] == sends


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["alive", "playing", "lockdown", "mafia", "grave-text", "grave-voice"])
async def test_recovery_does_not_skip_required_roles_or_private_channels(world, missing):
    game, guild, channels, members, roles = configure(world)
    game.gameplay["startup"]["complete"] = True
    game._recovering_permissions = True
    if missing in {"alive", "playing", "lockdown"}:
        rid = {"alive": game.alive_role_id, "playing": PLAYING_ROLE_ID, "lockdown": 30}[missing]
        unavailable = roles.pop(rid)
    else:
        cid = {"mafia": 11, "grave-text": 12, "grave-voice": 13}[missing]
        unavailable = channels.pop(cid)
    with pytest.raises(OSError):
        await access.reconcile(game, guild, lambda: None)
    assert game._recovering_permissions
    for member in members:
        member.add_roles.assert_not_awaited()
    for channel in channels.values():
        channel.edit.assert_not_awaited()
    if missing in {"alive", "playing", "lockdown"}:
        roles[rid] = unavailable
    else:
        channels[cid] = unavailable
    await access.reconcile(game, guild, lambda: None)
    for member in members:
        assert member.add_roles.await_count == 3
    assert members[0] in channels[11].edit.call_args.kwargs["overwrites"]
    for cid in (11, 12, 13):
        channels[cid].edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_mafia_channel_is_not_acknowledged_as_posted(world):
    game, guild, channels, _, _ = configure(world)
    game.gameplay["startup"]["complete"] = True
    mafia = channels.pop(11)
    with pytest.raises(OSError):
        await startup.announce(game, guild)
    assert not game.gameplay["startup"]["mafia_announced"]
    assert not game.gameplay["startup"]["announced"]
    assert not channels[10].sent
    channels[11] = mafia
    await startup.announce(game, guild)
    assert game.gameplay["startup"]["announced"]
    assert len(mafia.sent) == len(channels[10].sent) == 1
