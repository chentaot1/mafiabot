"""Shared guild resolution for game sync paths (HP01/HP09)."""

from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands


async def resolve_game_guild(bot: commands.Bot, guild_id: int) -> Optional[discord.Guild]:
    """Resolve the canonical game guild (cache, then API)."""
    guild = bot.get_guild(int(guild_id))
    if guild is not None:
        return guild
    try:
        return await bot.fetch_guild(int(guild_id))
    except (discord.HTTPException, discord.NotFound):
        return None
