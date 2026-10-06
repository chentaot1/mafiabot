"""Discord delivery helpers for ToS public log lines (open decision #34)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

import discord

if TYPE_CHECKING:
    from game import Game

logger = logging.getLogger(__name__)


def game_text_channel(game: "Game", guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch_id = getattr(game, "game_channel_id", None)
    if not ch_id:
        return None
    ch = guild.get_channel(int(ch_id))
    return ch if isinstance(ch, discord.TextChannel) else None


async def post_game_channel(
    game: "Game",
    guild: discord.Guild,
    *lines: str,
    allowed_mentions: Optional[discord.AllowedMentions] = discord.AllowedMentions.none(),
) -> bool:
    """Send each non-empty line to the game channel. Returns False if channel missing or any send fails."""
    ch = game_text_channel(game, guild)
    if not ch:
        logger.warning("post_game_channel: game_channel_id unset guild_id=%s", getattr(guild, "id", None))
        return False
    for line in lines:
        text = (line or "").strip()
        if not text:
            continue
        try:
            kwargs: Dict[str, Any] = {"allowed_mentions": allowed_mentions}
            await ch.send(text, **kwargs)
        except discord.HTTPException as e:
            logger.warning("post_game_channel send failed channel_id=%s: %s", ch.id, e)
            return False
    return True


async def post_game_channel_embed(
    game: "Game",
    guild: discord.Guild,
    embed: discord.Embed,
    *,
    allowed_mentions: Optional[discord.AllowedMentions] = discord.AllowedMentions.none(),
) -> tuple[bool, Optional[discord.Message]]:
    """Post an embed to the game channel. Returns (ok, message)."""
    ch = game_text_channel(game, guild)
    if not ch:
        logger.warning("post_game_channel_embed: game_channel_id unset guild_id=%s", getattr(guild, "id", None))
        return False, None
    try:
        msg = await ch.send(embed=embed, allowed_mentions=allowed_mentions)
        return True, msg
    except discord.HTTPException as e:
        logger.warning("post_game_channel_embed send failed channel_id=%s: %s", ch.id, e)
        return False, None


async def dm_member(member: discord.Member, text: str) -> bool:
    try:
        await member.send(text)
        return True
    except discord.HTTPException:
        return False
