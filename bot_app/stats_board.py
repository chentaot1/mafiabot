"""Pinned server stats board — one embed per guild, refreshed after each game ends."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord

from bot_app.shared import _chunk_lines, _winrate_line
from database import Database

_logger = logging.getLogger(__name__)

DISCORD_FIELD_VALUE_MAX = 1024

_refresh_locks: dict[int, asyncio.Lock] = {}


def _refresh_lock(guild_id: int) -> asyncio.Lock:
    lock = _refresh_locks.get(int(guild_id))
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[int(guild_id)] = lock
    return lock


def _channel_perms_ok(perms: discord.Permissions) -> tuple[bool, str]:
    """Permissions required to post and edit the bot's stats board message."""
    missing: list[str] = []
    if not perms.view_channel:
        missing.append("View Channel")
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.read_message_history:
        missing.append("Read Message History")
    if not perms.embed_links:
        missing.append("Embed Links")
    if missing:
        return False, ", ".join(missing)
    return True, ""


async def _try_delete_prior_board_message(
    *,
    bot: discord.Client,
    guild: discord.Guild,
    db: Database,
) -> None:
    """Remove the previous stats board message when re-running ``!setstatschannel``."""
    cfg = await asyncio.to_thread(db.get_guild_stats_board, guild_id=int(guild.id))
    if not cfg:
        return
    channel = guild.get_channel(int(cfg["channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        return
    me = guild.me
    if me is None:
        return
    try:
        msg = await channel.fetch_message(int(cfg["message_id"]))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    if msg.author.id != me.id:
        return
    try:
        await msg.delete()
    except discord.HTTPException:
        _logger.debug("Could not delete prior stats board message guild_id=%s", guild.id)


async def build_server_stats_board_embed(*, db: Database, guild_id: int) -> discord.Embed:
    """Compact live board for a dedicated stats channel (fits Discord limits comfortably)."""
    summary = await asyncio.to_thread(db.get_server_stats_summary, guild_id=int(guild_id))
    now = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%d %H:%M UTC")

    if not summary.get("games_completed") and not summary.get("player_games"):
        return discord.Embed(
            title="📈 Server stats",
            description="No completed games yet. Stats update automatically when a game ends.",
            color=discord.Color.dark_teal(),
        ).set_footer(text=f"Last check: {now}")

    gc = int(summary["games_completed"])
    desc = (
        f"**{gc}** games · **{int(summary['rostered_players'])}** players tracked\n"
        f"Updated **{now}**"
    )
    embed = discord.Embed(title="📈 Server stats (live)", description=desc, color=discord.Color.dark_teal())

    outcome_lines = [
        f"**{row['outcome']}** {float(row.get('pct', 0)):.1f}% ({int(row['count'])})"
        for row in (summary.get("outcomes") or [])[:10]
    ]
    for i, chunk in enumerate(_chunk_lines(outcome_lines, max_chars=DISCORD_FIELD_VALUE_MAX)):
        embed.add_field(
            name="Outcome winrates" if i == 0 else "\u200b",
            value=chunk,
            inline=True,
        )

    gl = summary.get("game_length") or {}
    len_bits: list[str] = []
    if gl.get("avg_days") is not None:
        len_bits.append(f"Avg **{gl['avg_days']}** days")
    for row in (gl.get("by_outcome") or [])[:4]:
        len_bits.append(f"{row['outcome']}: **{row['avg_days']}**d")
    for i, chunk in enumerate(_chunk_lines(len_bits, max_chars=DISCORD_FIELD_VALUE_MAX)):
        embed.add_field(
            name="Game length" if i == 0 else "\u200b",
            value=chunk,
            inline=True,
        )

    fac_lines = [
        f"**{row['faction']}** {_winrate_line(wins=int(row['wins']), played=int(row['played']))}"
        for row in (summary.get("factions") or [])
    ]
    for i, chunk in enumerate(_chunk_lines(fac_lines, max_chars=DISCORD_FIELD_VALUE_MAX)):
        embed.add_field(
            name="WR by faction (role played)" if i == 0 else "\u200b",
            value=chunk,
            inline=False,
        )

    lobby_lines: list[str] = []
    for row in (summary.get("lobby_sizes") or [])[:6]:
        pc = int(row["player_count"])
        top = row.get("outcomes") or []
        lead = top[0] if top else None
        lead_txt = f" · {lead['outcome']} {float(lead.get('pct', 0)):.0f}%" if lead else ""
        avg_d = row.get("avg_days")
        avg_txt = f" · {avg_d}d avg" if avg_d is not None else ""
        lobby_lines.append(f"**{pc}p** {int(row['games'])} games{avg_txt}{lead_txt}")
    extra = len(summary.get("lobby_sizes") or []) - 6
    if extra > 0:
        lobby_lines.append(f"_+{extra} more sizes — use `/serverstats`_")
    for i, chunk in enumerate(_chunk_lines(lobby_lines, max_chars=DISCORD_FIELD_VALUE_MAX)):
        embed.add_field(
            name="Lobby sizes" if i == 0 else "\u200b",
            value=chunk,
            inline=False,
        )

    top_roles = (summary.get("roles") or [])[:5]
    if top_roles:
        role_lines = [
            f"**{row['role']}** {_winrate_line(wins=int(row['wins']), played=int(row['played']))}"
            for row in top_roles
        ]
        for i, chunk in enumerate(_chunk_lines(role_lines, max_chars=DISCORD_FIELD_VALUE_MAX)):
            embed.add_field(
                name="Most played roles" if i == 0 else "\u200b",
                value=chunk,
                inline=False,
            )

    embed.set_footer(text="/serverstats for full breakdown · GM: !setstatschannel")
    return embed


def assert_board_embed_within_limits(embed: discord.Embed) -> None:
    """Raise AssertionError if embed violates Discord field value limits."""
    if embed.description and len(embed.description) > 4096:
        raise AssertionError(f"description too long: {len(embed.description)}")
    for f in embed.fields:
        if len(f.name) > 256:
            raise AssertionError(f"field name too long: {len(f.name)}")
        if len(f.value) > DISCORD_FIELD_VALUE_MAX:
            raise AssertionError(f"field value too long: {len(f.value)} for {f.name!r}")


async def refresh_guild_stats_board(*, bot: discord.Client, guild_id: int) -> bool:
    """Edit the pinned stats message if configured. Returns True on success."""
    db = getattr(bot, "db", None)
    if db is None:
        return False
    cfg = await asyncio.to_thread(db.get_guild_stats_board, guild_id=int(guild_id))
    if not cfg:
        return False

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return False
    channel = guild.get_channel(int(cfg["channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        return False
    me = guild.me
    if me is None:
        return False
    ok, missing = _channel_perms_ok(channel.permissions_for(me))
    if not ok:
        _logger.warning(
            "Stats board channel missing permissions guild_id=%s channel_id=%s need=%s",
            guild_id,
            channel.id,
            missing,
        )
        return False

    try:
        msg = await channel.fetch_message(int(cfg["message_id"]))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        _logger.warning(
            "Stats board message missing or inaccessible guild_id=%s channel_id=%s message_id=%s",
            guild_id,
            cfg["channel_id"],
            cfg["message_id"],
        )
        return False

    embed = await build_server_stats_board_embed(db=db, guild_id=int(guild_id))
    try:
        await msg.edit(embed=embed)
    except discord.HTTPException:
        _logger.exception("Failed to edit stats board guild_id=%s", guild_id)
        return False

    await asyncio.to_thread(
        db.upsert_guild_stats_board,
        guild_id=int(guild_id),
        channel_id=int(channel.id),
        message_id=int(msg.id),
    )
    return True


def schedule_stats_board_refresh(*, bot: discord.Client, guild_id: int) -> None:
    """Fire-and-forget refresh after SQLite endgame commit (from async win handler)."""

    async def _run() -> None:
        gid = int(guild_id)
        async with _refresh_lock(gid):
            try:
                await refresh_guild_stats_board(bot=bot, guild_id=gid)
            except Exception:
                _logger.exception("stats board refresh failed guild_id=%s", gid)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass


async def setup_stats_board_in_channel(
    *,
    bot: discord.Client,
    guild: discord.Guild,
    channel: discord.TextChannel,
) -> tuple[bool, str]:
    """Post (or replace) the live stats embed in ``channel`` and persist ids."""
    db = getattr(bot, "db", None)
    if db is None:
        return False, "SQLite DB not initialized."

    me = guild.me
    if me is None:
        return False, "Bot member not available."
    ok, missing = _channel_perms_ok(channel.permissions_for(me))
    if not ok:
        return False, f"Bot needs: {missing}."

    await _try_delete_prior_board_message(bot=bot, guild=guild, db=db)

    embed = await build_server_stats_board_embed(db=db, guild_id=guild.id)
    msg = await channel.send(embed=embed)
    try:
        await msg.pin()
    except discord.HTTPException:
        pass

    await asyncio.to_thread(
        db.upsert_guild_stats_board,
        guild_id=int(guild.id),
        channel_id=int(channel.id),
        message_id=int(msg.id),
    )
    return True, f"Live stats board posted in {channel.mention} (message `{msg.id}`)."
