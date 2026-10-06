"""Per-player private guild text channels (configured in config.PLAYER_PRIVATE_CHANNEL_IDS)."""

from __future__ import annotations

import logging
from typing import Optional

import discord

from config import PLAYER_PRIVATE_CHANNEL_IDS


def private_text_channel_id_for_user(user_id: int) -> Optional[int]:
    uid = int(user_id)
    direct = PLAYER_PRIVATE_CHANNEL_IDS.get(uid)
    if direct is not None:
        return int(direct)
    for channel_id, mapped_user in PLAYER_PRIVATE_CHANNEL_IDS.items():
        if int(mapped_user) == uid:
            return int(channel_id)
    return None


async def send_to_player_private_channel(
    guild: discord.Guild,
    user_id: int,
    content: str,
    *,
    log_context: str = "player private channel",
) -> bool:
    """Best-effort post to the player's mapped channel. Returns True if sent."""
    ch_id = private_text_channel_id_for_user(user_id)
    if ch_id is None:
        return False
    ch = guild.get_channel(ch_id)
    if not isinstance(ch, discord.TextChannel):
        return False
    me = guild.me
    if me is not None:
        perms = ch.permissions_for(me)
        if not (perms.view_channel and perms.send_messages):
            logging.warning(
                "%s: missing perms guild_id=%s channel_id=%s",
                log_context,
                guild.id,
                ch_id,
            )
            return False
    try:
        await ch.send(content, allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.HTTPException as e:
        logging.warning(
            "%s: send failed guild_id=%s channel_id=%s user_id=%s: %s",
            log_context,
            guild.id,
            ch_id,
            user_id,
            e,
        )
        return False
