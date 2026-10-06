from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Collection, Optional, TYPE_CHECKING

import discord
from discord.ext import commands

from config import PLAYER_PRIVATE_CHANNEL_IDS
from guild_resolve import resolve_game_guild  # re-exported for tests and callers

if TYPE_CHECKING:
    from game import Game


def night_autocomplete_living_slots_ok(
    game: Any,
    user_id: int,
    *,
    allowed_roles: Collection[str],
    extra_ok: Optional[Callable[[Any, int], bool]] = None,
) -> bool:
    """
    Slash autocomplete guard: only the command's role+phase may see living slot/name choices.
    Keeps hybrid /target dropdowns from leaking the roster to other players (audit: slash info leak).
    """
    if game is None or not bool(getattr(game, "in_progress", False)):
        return False
    if getattr(game, "phase", None) != "night":
        return False
    from night_action_guards import night_actions_frozen

    if night_actions_frozen(game):
        return False
    roles_map = getattr(game, "player_roles", None) or {}
    role = roles_map.get(user_id)
    allowed = set(allowed_roles)
    if role not in allowed:
        return False
    living = getattr(game, "living_players", None) or []
    living_ids: set[int] = set()
    for m in living:
        mid = getattr(m, "id", None)
        if mid is not None:
            living_ids.add(int(mid))
    in_living = int(user_id) in living_ids
    if not in_living:
        # Dead Guardian Angel may still resolve `ward` (slash autocomplete should not leak slots).
        if role == "Guardian Angel" and int(game.role_states.get(user_id, {}).get("ga_ward_charges", 0)) > 0:
            return True
        return False
    if extra_ok is not None and not extra_ok(game, user_id):
        return False
    return True


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
                if getattr(ctx, "interaction", None) and not ctx.interaction.response.is_done():
                    await ctx.defer(ephemeral=True)
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
                from gameplay.controller import channel_is_private
                try:
                    if not ctx.guild.chunked:
                        await ctx.guild.chunk(cache=True)
                    private = ctx.guild.chunked and channel_is_private(ctx.channel, ctx.guild, ctx.author.id)
                except discord.HTTPException:
                    private = False
                if not private:
                    return await ctx.send("This channel is not private enough for role details. DM me or use /actions.", ephemeral=True)

            if not game or not game.in_progress or game.phase != "night":
                try:
                    await ctx.send("🛑 **Commands are disabled.** It may not be nighttime, or you may be dead.")
                except discord.HTTPException:
                    pass
                return

            from night_action_guards import night_actions_frozen, actor_has_guilt_pending
            if night_actions_frozen(game):
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

            if actor_has_guilt_pending(game, ctx.author.id):
                return await ctx.send("You are overcome with guilt.")
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
