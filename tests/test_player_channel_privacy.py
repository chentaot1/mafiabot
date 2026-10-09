"""Legacy text prompts require the same privacy proof as modern controls."""
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import discord
import pytest

import player_channels
from config import GAME_OVERSEER_ROLE_ID
from gameplay.controller import channel_is_private
from test_gameplay import Channel, world


def mapped_channel(world, monkeypatch):
    _, guild, _, channels, _ = world
    destination = channels[10]
    guild.me = S(id=999, bot=True)
    destination.readers = {1, 999}
    monkeypatch.setattr(player_channels, "PLAYER_PRIVATE_CHANNEL_IDS", {1: 10})
    monkeypatch.setattr(player_channels.discord, "TextChannel", Channel)
    return guild, destination


@pytest.mark.asyncio
@pytest.mark.parametrize("exposure", ["everyone", "other_player", "unknown_role", "uncached_member", "uncached_role", "owner_missing", "owner_cannot_read", "wrong_guild"])
async def test_text_prompt_refuses_unverified_mapped_channel(world, monkeypatch, exposure):
    guild, destination = mapped_channel(world, monkeypatch)
    if exposure == "everyone":
        destination.readers.add(0)
    elif exposure == "other_player":
        destination.readers.add(2)
    elif exposure == "unknown_role":
        role = discord.Role(guild=guild, state=None, data={"id": "55", "name": "Visitor", "permissions": "0"})
        destination.overwrites[role] = discord.PermissionOverwrite(view_channel=True)
    elif exposure.startswith("uncached"):
        target = discord.Object(id=55, type=discord.Member if exposure == "uncached_member" else discord.Role)
        destination.overwrites[target] = discord.PermissionOverwrite(view_channel=True)
    elif exposure == "owner_missing":
        original = guild.get_member
        monkeypatch.setattr(guild, "get_member", lambda uid: None if uid == 1 else original(uid))
    elif exposure == "owner_cannot_read":
        destination.readers.remove(1)
    else:
        destination.guild = S(id=999)
    assert not await player_channels.send_to_player_private_channel(guild, 1, "SECRET role and action")
    assert not destination.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["incomplete", "failure", "complete"])
async def test_text_prompt_requires_complete_member_cache_before_send(world, monkeypatch, status):
    guild, destination = mapped_channel(world, monkeypatch)
    guild.chunked = False
    async def chunk(*, cache):
        assert cache is True
        if status == "failure":
            raise discord.HTTPException(S(status=503, reason="offline test"), "chunk unavailable")
        if status == "complete":
            guild.chunked = True
    guild.chunk = AsyncMock(side_effect=chunk)
    sent = await player_channels.send_to_player_private_channel(guild, 1, "Private prompt")
    assert sent is (status == "complete")
    assert len(destination.sent) == (1 if status == "complete" else 0)
    guild.chunk.assert_awaited_once_with(cache=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("staff", ["none", "administrator", "overseer"])
async def test_text_prompt_allows_verified_channel_and_staff_without_mentions(world, monkeypatch, staff):
    guild, destination = mapped_channel(world, monkeypatch)
    if staff != "none":
        observer = guild.get_member(2)
        destination.readers.add(2)
        if staff == "administrator":
            observer.guild_permissions.administrator = True
        else:
            observer.roles = [S(id=GAME_OVERSEER_ROLE_ID)]
    assert await player_channels.send_to_player_private_channel(guild, 1, "Private prompt @everyone")
    assert len(destination.sent) == 1
    _, kwargs = destination.sent[0]
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_type", [discord.Member, discord.Role])
async def test_uncached_overwrite_principal_falls_back_to_dm_for_modern_controls(world, monkeypatch, target_type):
    game, guild, controller, channels, _ = world
    destination = channels[10]
    destination.readers = {1}
    destination.overwrites[discord.Object(id=55, type=target_type)] = discord.PermissionOverwrite(view_channel=True)
    monkeypatch.setattr("gameplay.controller.PLAYER_PRIVATE_CHANNEL_IDS", {1: 10})
    assert not channel_is_private(destination, guild, 1)
    await controller.send_panel(game, 1)
    assert not destination.sent and len(channels[101].sent) == 1
    assert game.gameplay["panels"]["1"]["channel_id"] == 101
