from __future__ import annotations

from functools import wraps
from typing import Callable, Optional, TYPE_CHECKING

import discord
from discord.ext import commands

from config import PLAYER_PRIVATE_CHANNEL_IDS

if TYPE_CHECKING:
    from game import Game


def only_during_night_gameplay(
    *,
    bot: commands.Bot,
    get_game_by_player_id: Callable[[int], Optional["Game"]],
) -> Callable:
    """
    Decorator for player night-action commands.
    Attaches `ctx.game` for downstream handlers.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(ctx: commands.Context, *args, **kwargs):
            game = get_game_by_player_id(ctx.author.id)
            if not game:
                try:
                    await ctx.send("No active game found for you.")
                except discord.HTTPException:
                    pass
                return

            # Privacy surfaces:
            # - Prefix commands are allowed in DMs (classic experience).
            # - In-server usage is allowed, but only inside the configured per-player private channel.
            if ctx.guild is not None:
                if int(ctx.guild.id) != int(game.guild_id):
                    try:
                        await ctx.send("🛑 This action belongs to a different server's game.")
                    except discord.HTTPException:
                        pass
                    return

                expected_channel_id = PLAYER_PRIVATE_CHANNEL_IDS.get(int(ctx.author.id))
                if not expected_channel_id:
                    # Backwards-compat hardening: some deployments accidentally store the mapping
                    # as channel_id -> user_id. Support both shapes.
                    for k, v in PLAYER_PRIVATE_CHANNEL_IDS.items():
                        if int(v) == int(ctx.author.id):
                            expected_channel_id = int(k)
                            break

                if not expected_channel_id:
                    try:
                        await ctx.send("🛑 Your private channel isn't configured yet. Ask a GM to set it up.")
                    except discord.HTTPException:
                        pass
                    return

                if ctx.channel.id != int(expected_channel_id):
                    try:
                        await ctx.send("🛑 Use your private channel for night actions (or DM me the command).")
                    except discord.HTTPException:
                        pass
                    return

            if not game or not game.in_progress or game.phase != "night":
                try:
                    await ctx.send("🛑 **Commands are disabled.** It may not be nighttime, or you may be dead.")
                except discord.HTTPException:
                    pass
                return

            if getattr(game, "resolving", False):
                try:
                    await ctx.send("🛑 **Night is resolving.** Please wait a moment and try again.")
                except discord.HTTPException:
                    pass
                return

            if ctx.guild:
                await game.sync_living_players(ctx.guild)
                living_ids = await game.get_living_ids(ctx.guild)
            else:
                # In DMs, rely on cached living list (no guild/member fetch).
                living_ids = [p.id for p in game.living_players]

            if ctx.author.id not in living_ids:
                try:
                    await ctx.send("🛑 **Commands are disabled.** It may not be nighttime, or you may be dead.")
                except discord.HTTPException:
                    pass
                return

            ctx.game = game
            return await func(ctx, *args, **kwargs)

        return wrapper

    return decorator


async def enforce_allowed_guild(ctx: commands.Context, *, allowed_guild_id: int) -> bool:
    # Allow DMs (night actions). Guild commands are restricted.
    if ctx.guild is None:
        return True
    if ctx.guild.id != allowed_guild_id:
        try:
            await ctx.send("🛑 This bot is locked to a different server.")
        except discord.HTTPException:
            pass
        return False
    return True
