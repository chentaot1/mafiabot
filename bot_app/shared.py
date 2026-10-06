"""Shared helpers for GM, tribunal, and player commands."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO

from bot_app.imports import *  # noqa: F403
from bot_app.instance import ALLOWED_GUILD_ID, bot, only_during_night_gameplay

_logger = logging.getLogger(__name__)


async def safe_send(
    ctx: commands.Context, content: str = "", **kwargs
) -> Optional[discord.Message]:
    """Send a command reply; return the Message or None on Discord HTTP failure."""
    try:
        return await ctx.send(content, **kwargs)
    except discord.HTTPException as e:
        _logger.debug("safe_send failed channel_id=%s: %s", getattr(ctx.channel, "id", None), e)
        return None


async def safe_reply(ctx: commands.Context, content: str = "", **kwargs) -> bool:
    """Send a command reply; return False on Discord HTTP failure."""
    return await safe_send(ctx, content, **kwargs) is not None


async def deny_player_command(ctx: commands.Context, message: str, *, dm_only: bool = False) -> None:
    """User-facing denial for invalid player commands."""
    if dm_only and not isinstance(ctx.channel, discord.DMChannel):
        try:
            await ctx.author.send(message)
        except discord.HTTPException as e:
            _logger.debug("deny_player_command DM failed user_id=%s: %s", ctx.author.id, e)
        await safe_reply(ctx, "🛑 Check your DMs — that command only works in a private message to the bot.")
        return
    await safe_reply(ctx, message)


async def safe_interaction_ephemeral(interaction: discord.Interaction, content: str) -> bool:
    """Reply ephemerally to a slash interaction; return False on HTTP failure."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content, ephemeral=True)
        else:
            await interaction.followup.send(content, ephemeral=True)
        return True
    except discord.HTTPException as e:
        _logger.debug(
            "safe_interaction_ephemeral failed interaction_id=%s: %s",
            getattr(interaction, "id", None),
            e,
        )
        return False


async def safe_channel_send(
    channel: discord.abc.Messageable,
    content: str,
    **kwargs,
) -> bool:
    """Send to a channel/DM; return False on Discord HTTP failure."""
    try:
        await channel.send(content, **kwargs)
        return True
    except discord.HTTPException as e:
        _logger.debug(
            "safe_channel_send failed channel_id=%s: %s",
            getattr(channel, "id", None),
            e,
        )
        return False


def _extra_mole_investigate_ok(game: Any, uid: int) -> bool:
    if game.player_roles.get(uid) == "Mole":
        return int(game.role_states.get(uid, {}).get("uses_remaining", 0)) > 0
    return True


def _extra_vig_shoot_ok(game: Any, uid: int) -> bool:
    st = game.role_states.get(uid, {})
    return (
        int(st.get("shots_remaining", 0)) > 0
        and not bool(st.get("will_die_of_guilt"))
        and not bool(st.get("guilty_tomorrow"))
    )


def _extra_framer_nights_ok(game: Any, _uid: int) -> bool:
    return int(getattr(game, "day_number", 99)) <= 2


def _extra_gravedigger_uses_ok(game: Any, uid: int) -> bool:
    return int(game.role_states.get(uid, {}).get("uses_remaining", 0)) > 0


_AUTOCOMPLETE_SYNC_TTL_SEC = 4.0
_autocomplete_last_sync_mono: Dict[int, float] = {}

_GUILD_UNAVAILABLE_MSG = (
    "🛑 **Server unavailable.** Cannot verify living players; try again shortly."
)


async def _guild_for_context(ctx: commands.Context, game: Game) -> Optional[discord.Guild]:
    if ctx.guild is not None:
        return ctx.guild
    return await resolve_game_guild(bot, int(game.guild_id))


async def _sync_living_ids_for_action(ctx: commands.Context, game: Game) -> Optional[List[int]]:
    """Sync living players from the canonical guild; None if guild is unavailable (HP09)."""
    guild_for_sync = await _guild_for_context(ctx, game)
    if guild_for_sync is None:
        return None
    await game.sync_living_players(guild_for_sync)
    return await game.get_living_ids(guild_for_sync)


async def _living_ids_for_context(ctx: commands.Context, game: Game) -> Optional[List[int]]:
    """Guild-scoped living list for day/guild commands (whisper, etc.)."""
    return await _sync_living_ids_for_action(ctx, game)


async def _living_slot_choices_for_user(
    user_id: int,
    *,
    allowed_roles: Collection[str],
    extra_ok: Optional[Callable[[Any, int], bool]] = None,
) -> List[discord.app_commands.Choice[int]]:
    game = get_game_by_player_id(user_id)
    if game is None:
        return []
    guild = await resolve_game_guild(bot, int(game.guild_id))
    if guild is not None:
        now = time.monotonic()
        last = _autocomplete_last_sync_mono.get(int(game.guild_id), 0.0)
        if now - last >= _AUTOCOMPLETE_SYNC_TTL_SEC:
            await game.sync_living_players(guild)
            _autocomplete_last_sync_mono[int(game.guild_id)] = now
    if not night_autocomplete_living_slots_ok(game, user_id, allowed_roles=allowed_roles, extra_ok=extra_ok):
        return []
    assert game is not None
    living = game.ordered_living_players()
    choices: List[discord.app_commands.Choice[int]] = []
    for m in living:
        slot = game.player_slots.get(m.id)
        if not slot:
            continue
        # Keep name short; Discord caps label length.
        label = f"#{slot} {m.display_name}"[:100]
        choices.append(discord.app_commands.Choice(name=label, value=int(slot)))
    return choices[:25]


async def _autocomplete_living_slot(
    interaction: discord.Interaction,
    current: str,
    *,
    allowed_roles: Collection[str],
    extra_ok: Optional[Callable[[Any, int], bool]] = None,
) -> List[discord.app_commands.Choice[int]]:
    # `current` is ignored for now; we keep a stable short list.
    _ = current
    return await _living_slot_choices_for_user(interaction.user.id, allowed_roles=allowed_roles, extra_ok=extra_ok)


def _exclude_choice(choices: List[discord.app_commands.Choice[int]], exclude_value: Optional[int]) -> List[discord.app_commands.Choice[int]]:
    if not exclude_value:
        return choices
    return [c for c in choices if c.value != exclude_value]


async def _autocomplete_living_slot_excluding(
    interaction: discord.Interaction,
    current: str,
    *,
    exclude_param: str,
    allowed_roles: Collection[str],
    extra_ok: Optional[Callable[[Any, int], bool]] = None,
) -> List[discord.app_commands.Choice[int]]:
    # Exclude the already-chosen slot from the second-target autocomplete.
    exclude_val = None
    try:
        opts = interaction.data.get("options") or []
        # When hybrid commands are used as slash, options are flat at the top level.
        for o in opts:
            if o.get("name") == exclude_param:
                exclude_val = o.get("value")
                break
    except Exception:
        exclude_val = None
    base = await _living_slot_choices_for_user(interaction.user.id, allowed_roles=allowed_roles, extra_ok=extra_ok)
    try:
        return _exclude_choice(base, int(exclude_val) if exclude_val is not None else None)
    except (TypeError, ValueError):
        return base
async def _build_leaderboard_embed(*, db: Database, guild_id: int, page: str) -> discord.Embed:
    title = "📊 Leaderboard"
    page = str(page)

    async def _to_thread(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    if page == "total":
        rows = await _to_thread(db.top_total_wins, guild_id=guild_id, limit=10)
        subtitle = "Total wins (includes personal wins)"
        lines = [f"**{i}.** <@{r.player_id}> — **{int(r.value)}** wins ({r.wins}/{r.games_played})" for i, r in enumerate(rows, 1)]
    elif page == "winrate":
        rows = await _to_thread(db.top_winrate, guild_id=guild_id, min_games=5, limit=10)
        subtitle = "Winrate (min 5 games)"
        lines = [
            f"**{i}.** <@{r.player_id}> — **{(r.value * 100.0):.1f}%** ({r.wins}/{r.games_played})"
            for i, r in enumerate(rows, 1)
        ]
    elif page in {"town", "mafia", "arsonist"}:
        faction = {"town": "Town", "mafia": "Mafia", "arsonist": "Arsonist"}[page]
        rows = await _to_thread(db.top_faction_wins, guild_id=guild_id, faction=faction, limit=10)
        subtitle = f"{faction} wins"
        lines = [f"**{i}.** <@{r.player_id}> — **{int(r.value)}** {faction} wins" for i, r in enumerate(rows, 1)]
    else:
        rows = await _to_thread(db.top_personal, guild_id=guild_id, key=page, limit=10)
        label = PERSONAL_WIN_LABELS.get(page, page.replace("_", " ").title())
        subtitle = f"Personal: {label}"
        lines = [f"**{i}.** <@{r.player_id}> — **{int(r.value)}**" for i, r in enumerate(rows, 1)]

    embed = discord.Embed(title=title, description="\n".join(lines) if lines else "(no data yet)", color=discord.Color.blurple())
    embed.set_footer(text=subtitle)
    return embed


from stats_personal import PERSONAL_WIN_LABELS, migrate_personal_wins_dict

DEATH_CAUSE_LABELS: Dict[str, str] = {
    "sk_counter_attack": "Serial Killer (counter)",
    "serial_killer": "Serial Killer",
    "mafia": "Mafia",
    "arsonist_ignite": "Arsonist ignite",
    "pirate_plunder": "Pirate plunder",
    "bodyguard": "Bodyguard counter",
    "bodyguard_guard": "Bodyguard (on guard)",
    "vigilante": "Vigilante",
    "guilt": "Vig guilt",
    "scary_grandma": "Scary Grandma alert",
    "deputy_shoot": "Deputy",
    "deputy_friendly_fire": "Deputy (FF)",
    "deputy_friendly_fire_self": "Deputy (self FF)",
    "haunt": "Jester haunt",
    "lynch": "Lynch",
    "night_kill": "Night kill (unspecified)",
    "left": "Left game",
    "shot": "Shot",
}


def _winrate_line(*, wins: int, played: int) -> str:
    pct = (wins / played * 100.0) if played > 0 else 0.0
    return f"**{pct:.1f}%** ({wins}/{played})"


DISCORD_EMBED_MAX_FIELDS = 25
DISCORD_EMBED_FIELD_VALUE_MAX = 1024


def _chunk_lines(lines: list[str], *, max_chars: int = 950) -> list[str]:
    if not lines:
        return ["(none)"]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        add = len(line) + (1 if buf else 0)
        if buf and size + add > max_chars:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _add_chunked_fields(
    embed: discord.Embed,
    *,
    base_name: str,
    lines: list[str],
    max_chars: int = DISCORD_EMBED_FIELD_VALUE_MAX,
) -> None:
    """Add one or more embed fields; stop before Discord's 25-field cap."""
    if not lines:
        embed.add_field(name=base_name, value="(none)", inline=False)
        return
    for i, chunk in enumerate(_chunk_lines(lines, max_chars=max_chars)):
        if len(embed.fields) >= DISCORD_EMBED_MAX_FIELDS - 1:
            embed.add_field(
                name="\u200b",
                value="_…more stats on other `/serverstats` pages_",
                inline=False,
            )
            return
        embed.add_field(name=base_name if i == 0 else "\u200b", value=chunk, inline=False)


def assert_server_stats_embed_within_limits(embed: discord.Embed) -> None:
    """Raise AssertionError if embed violates Discord limits."""
    if embed.description and len(embed.description) > 4096:
        raise AssertionError(f"description too long: {len(embed.description)}")
    if len(embed.fields) > DISCORD_EMBED_MAX_FIELDS:
        raise AssertionError(f"too many fields: {len(embed.fields)}")
    for f in embed.fields:
        if len(f.name) > 256:
            raise AssertionError(f"field name too long: {len(f.name)}")
        if len(f.value) > DISCORD_EMBED_FIELD_VALUE_MAX:
            raise AssertionError(f"field value too long: {len(f.value)} for {f.name!r}")


async def _build_server_stats_embed(*, db: Database, guild_id: int, page: str) -> discord.Embed:
    """Server-wide stats; updates automatically when games end and SQLite commits."""
    page = str(page)
    summary = await asyncio.to_thread(db.get_server_stats_summary, guild_id=int(guild_id))

    if not summary.get("games_completed") and not summary.get("player_games"):
        embed = discord.Embed(
            title="📈 Server stats",
            description="No completed games in the database yet. Stats appear after the first game ends.",
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text="Auto-updated on each game end")
        return embed

    gc = int(summary["games_completed"])
    rp = int(summary["rostered_players"])
    pg = int(summary["player_games"])
    wins = int(summary["wins"])
    losses = int(summary["losses"])
    draws = int(summary["draws"])

    if page == "overview":
        desc = (
            f"**Games completed:** {gc}\n"
            f"**Players with stats:** {rp}\n"
            f"**Player-games recorded:** {pg}\n"
            f"**Record (all players):** {wins}W / {losses}L / {draws}D"
        )
        if gc == 0 and pg > 0:
            desc += (
                "\n\n_Imported JSON player stats only — outcome %, lobby sizes, avg length, "
                "and death causes appear after games **end** and commit to SQLite._"
            )
        embed = discord.Embed(title="📈 Server stats — Overview", description=desc, color=discord.Color.dark_teal())

        outcome_lines = [
            f"**{row['outcome']}** — **{float(row.get('pct', 0)):.1f}%** ({int(row['count'])}/{gc})"
            for row in summary.get("outcomes") or []
        ]
        _add_chunked_fields(embed, base_name="Outcome winrates", lines=outcome_lines)

        gl = summary.get("game_length") or {}
        len_lines: list[str] = []
        if gl.get("avg_days") is not None:
            len_lines = [
                f"**Overall:** avg **{gl['avg_days']}** days "
                f"(min {gl.get('min_days', '?')}, max {gl.get('max_days', '?')}, n={gl.get('games_with_days', 0)})"
            ]
            for row in gl.get("by_outcome") or []:
                len_lines.append(
                    f"**{row['outcome']}** — avg **{row['avg_days']}** days ({int(row['count'])} games)"
                )
        if len_lines:
            _add_chunked_fields(embed, base_name="Avg game length (ended day #)", lines=len_lines)

        lobby_lines: list[str] = []
        for row in summary.get("lobby_sizes") or []:
            pc = int(row["player_count"])
            games_n = int(row["games"])
            avg_d = row.get("avg_days")
            avg_txt = f" · avg **{avg_d}** days" if avg_d is not None else ""
            top_out = row.get("outcomes") or []
            out_bits = ", ".join(
                f"{o['outcome']} {float(o.get('pct', 0)):.0f}%"
                for o in top_out[:4]
            )
            if len(top_out) > 4:
                out_bits += ", …"
            lobby_lines.append(f"**{pc}p** — {games_n} games{avg_txt}" + (f"\n  {out_bits}" if out_bits else ""))
        if lobby_lines:
            _add_chunked_fields(embed, base_name="Lobby size breakdown", lines=lobby_lines)

        fac_lines = [
            f"**{row['faction']}** — {_winrate_line(wins=int(row['wins']), played=int(row['played']))}"
            for row in summary.get("factions") or []
        ]
        _add_chunked_fields(embed, base_name="Winrate by role group (Town / Mafia / Neutral played)", lines=fac_lines)

        role_lines = [
            f"**{row['role']}** — {_winrate_line(wins=int(row['wins']), played=int(row['played']))}"
            for row in (summary.get("roles") or [])[:12]
        ]
        extra = len(summary.get("roles") or []) - 12
        if extra > 0:
            role_lines.append(f"_…and {extra} more roles (see **Roles** page)_")
        _add_chunked_fields(embed, base_name="Top roles by games played", lines=role_lines)
        embed.set_footer(
            text="Neutral Personal wins (Pirate, GA, etc.) on Personal page · Auto-updated on game end · GM: !exportstats"
        )
        return embed

    if page == "factions":
        lines = [
            f"**{row['faction']}** — {_winrate_line(wins=int(row['wins']), played=int(row['played']))}"
            for row in summary.get("factions") or []
        ]
        embed = discord.Embed(
            title="📈 Server stats — Factions",
            description=(
                "Winrate when a player **ended the game on that faction** "
                "(sum of all `player_role_stats` in this server)."
            ),
            color=discord.Color.dark_teal(),
        )
        _add_chunked_fields(embed, base_name="By faction", lines=lines)
        embed.set_footer(text="Town / Mafia / Neutral = role group played · Personal wins tracked separately")
        return embed

    if page == "roles":
        lines = [
            f"**{row['role']}** — {_winrate_line(wins=int(row['wins']), played=int(row['played']))}"
            for row in summary.get("roles") or []
        ]
        embed = discord.Embed(
            title="📈 Server stats — Roles",
            description="Winrate **per role** (games played as that role, any player).",
            color=discord.Color.dark_teal(),
        )
        _add_chunked_fields(embed, base_name="All roles", lines=lines)
        embed.set_footer(text=f"{len(lines)} roles with at least one game")
        return embed

    if page == "deaths":
        lines = []
        for row in summary.get("death_causes") or []:
            tag = str(row["cause"])
            label = DEATH_CAUSE_LABELS.get(tag, tag.replace("_", " ").title())
            lines.append(f"**{label}** — {int(row['count'])}")
        embed = discord.Embed(
            title="📈 Server stats — Death causes",
            description="How players died (from game history, non-survivors).",
            color=discord.Color.dark_teal(),
        )
        if gc == 0 and pg > 0:
            embed.description += (
                "\n\n_No death history yet — import only backfills player aggregates._"
            )
        _add_chunked_fields(embed, base_name="Causes", lines=lines)
        embed.set_footer(text="Tagged on night resolve / lynch / day abilities")
        return embed

    # personal
    lines = []
    for row in summary.get("personal_wins") or []:
        key = str(row["key"])
        label = PERSONAL_WIN_LABELS.get(key, key.replace("_", " ").title())
        lines.append(f"**{label}** — {int(row['count'])}")
    embed = discord.Embed(
        title="📈 Server stats — Personal wins",
        description="Server-wide count of special personal win conditions achieved.",
        color=discord.Color.dark_teal(),
    )
    _add_chunked_fields(embed, base_name="Totals", lines=lines)
    embed.set_footer(text="Same keys as /leaderboard · Faction wins = Town/Mafia/Arsonist only")
    return embed


def build_server_stats_export_bytes(*, guild_id: int, guild_name: Optional[str], summary: dict) -> tuple[bytes, str]:
    """JSON export payload for GM ``!exportstats``."""
    payload = {
        "guild_id": int(guild_id),
        "guild_name": guild_name,
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "summary": summary,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"server_stats_{guild_id}.json"
    return text.encode("utf-8"), fname
