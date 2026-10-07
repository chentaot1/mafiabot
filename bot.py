import discord
from discord.ext import commands
import random
import asyncio
import logging
from typing import List, Dict, Optional, Tuple, Set
import os
from datetime import datetime, timedelta, timezone
import secrets
from pathlib import Path
import json
import time
import sys
from roles import get_role_description
from persistence import load_state
import persistence
from persistence import load_stats
from checks import only_during_night_gameplay as only_during_night_gameplay_factory, enforce_allowed_guild
from errors import on_app_command_tree_error, on_command_error as on_command_error_handler
from game import Game, active_games, bind_bot, get_game_by_player_id, get_game_for_guild
from engine.night import run_night_pipeline
from gameplay import actions as gameplay_actions, state as gameplay_state, trials as gameplay_trials, resolution
from gameplay.controller import Controller
from gameplay.views import private_reply, NO_MENTIONS
from database import Database
from instance_lock import acquire_instance_lock
from async_work import run_blocking, finish_pending
import game_roles
from config import role_starting_charges, chaos_starting_uses, guardian_angel_bind_pool_ids
from config import (
    TRIBUNAL_RESUME_MIN_SECONDS,
    GAME_MASTER_ROLE,
    GAME_OVERSEER_ROLE_ID,
    ALIVE_ROLE_NAME,
    STAND_ROLE_NAME,
    DAY_VOICE_CHANNEL_NAME,
    MAFIA_CHANNEL_NAME,
    GRAVEYARD_TEXT_CHANNEL_NAME,
    GRAVEYARD_VOICE_CHANNEL_NAME,
    PLAYING_ROLE_ID,
    DUEL_DURATION,
    VOTE_DURATION,
    VOTE_LIMIT_PER_DAY,
    ALL_MAFIA_ROLES,
    TOWN_ROLES,
    ROLEBLOCK_IMMUNE_ROLES,
    CONTROL_IMMUNE_ROLES,
    load_allowed_guild_id,
    validate_live_settings,
)

"""
Bot entrypoint.

This bot is locked to a single server (guild). Prefer configuring via `.env`:
`ALLOWED_GUILD_ID=123`
"""

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# region agent log (debug)
_DEBUG_LOG_PATH = Path(__file__).resolve().parent / "debug-d0f4f7.log"
_DEBUG_SESSION_ID = "d0f4f7"
_debug_run_id = os.environ.get("MAFIABOT_DEBUG_RUN_ID", "pre-fix")
_debug_instance = os.environ.get("MAFIABOT_DEBUG_INSTANCE", "").strip() or secrets.token_hex(2)
_debug_tag_enabled = os.environ.get("MAFIABOT_DEBUG_TAG", "1").strip().lower() in ("1", "true", "yes", "on")


def _tag() -> str:
    if not _debug_tag_enabled:
        return ""
    return f" [pid={int(os.getpid())} inst={_debug_instance}]"


_single_instance_lock_handle = None


def _acquire_single_instance_lock() -> None:
    """Hold an OS lock for the process lifetime; a crash releases it safely."""
    global _single_instance_lock_handle
    if os.environ.get("MAFIABOT_ALLOW_MULTI", "").strip().lower() in ("1", "true", "yes", "on"):
        _dbg("H7", "bot.py:single_instance", "single-instance lock bypassed", {})
        return
    if _single_instance_lock_handle is not None:
        return

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(Path.home())
    lock_path = Path(os.environ.get('MAFIABOT_INSTANCE_LOCK_PATH') or (Path(base) / "Mafiabot" / "bot.instance.lock"))
    # Acquire before changing the file. Keep the handle open and never unlink
    # the lock file, so duplicate startups cannot signal or evict the owner.
    _single_instance_lock_handle = acquire_instance_lock(lock_path)
    info = {
        "pid": int(os.getpid()),
        "inst": _debug_instance,
        "cwd": os.getcwd(),
        "argv": list(getattr(sys, "argv", [])),
        "ts_ms": int(time.time() * 1000),
    }
    _single_instance_lock_handle.write(json.dumps(info, sort_keys=True).encode("utf-8"))
    _single_instance_lock_handle.truncate()
    _dbg("H7", "bot.py:single_instance", "lock acquired", {"lock_path": str(lock_path), "info": info})


def _dbg(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": _DEBUG_SESSION_ID,
            "runId": _debug_run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        # Never crash the bot due to debug logging.
        pass


def _safe_guild_ids() -> List[int]:
    try:
        return [int(g.id) for g in getattr(bot, "guilds", [])]
    except Exception:
        return []


def _safe_tree_counts() -> dict:
    out = {"global": None, "allowed_guild": None}
    try:
        out["global"] = len(bot.tree.get_commands())
    except Exception:
        out["global"] = None
    try:
        out["allowed_guild"] = len(bot.tree.get_commands(guild=discord.Object(id=ALLOWED_GUILD_ID)))
    except Exception:
        out["allowed_guild"] = None
    return out

# endregion

# --- BOT SETUP ---
ALLOWED_GUILD_ID: int = load_allowed_guild_id()
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.dm_messages = True
bot = commands.Bot(command_prefix='!', intents=intents)
bind_bot(bot)
bot.gameplay_controller = Controller(bot)


@bot.tree.error
async def _tree_error_handler(interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
    await on_app_command_tree_error(interaction, error)


async def _mafia_tree_interaction_check(interaction: discord.Interaction) -> bool:
    """B6.3: central slash/UI gate — single-guild bot."""
    try:
        if interaction.guild_id is not None and interaction.guild_id != ALLOWED_GUILD_ID:
            msg = "This bot is locked to one configured server."
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
            return False
    except Exception:
        logging.exception("interaction_check failed")
        return interaction.guild_id is not None
    return True


bot.tree.interaction_check = _mafia_tree_interaction_check


# --- Gateway stuck watchdog (B6.2, parity with StudyBot single-loop policy) ---
def _reset_gateway_watchdog_session() -> None:
    t = getattr(bot, "_gateway_watchdog_task", None)
    if t is not None and not t.done():
        t.cancel()
    bot._gateway_watchdog_task = None  # type: ignore[attr-defined]
    bot._gateway_had_ready = False  # type: ignore[attr-defined]
    bot._gateway_disconnect_at = None  # type: ignore[attr-defined]
    bot._gateway_session_started_at = time.monotonic()  # type: ignore[attr-defined]
    bot._gateway_last_stuck_log_at = 0.0  # type: ignore[attr-defined]


def _ensure_gateway_watchdog_task() -> None:
    t = getattr(bot, "_gateway_watchdog_task", None)
    if t is not None and not t.done():
        return
    bot._gateway_watchdog_task = asyncio.create_task(_gateway_stuck_watchdog(), name="gateway_stuck_watchdog")  # type: ignore[attr-defined]


async def _gateway_stuck_watchdog() -> None:
    disconnect_sec = float(os.getenv("GATEWAY_STUCK_DISCONNECT_SEC", "900"))
    initial_sec = float(os.getenv("GATEWAY_STUCK_INITIAL_CONNECT_SEC", "1200"))
    interval = max(5.0, float(os.getenv("GATEWAY_STUCK_POLL_SEC", "30")))
    log_every = float(os.getenv("GATEWAY_STUCK_LOG_EVERY_SEC", "300"))
    try:
        while not bot.is_closed():
            await asyncio.sleep(interval)
            if bot.is_closed():
                break
            now = time.monotonic()
            if bot.is_ready():
                bot._gateway_disconnect_at = None  # type: ignore[attr-defined]
                continue

            had_ready = getattr(bot, "_gateway_had_ready", False)
            if had_ready and getattr(bot, "_gateway_disconnect_at", None) is None:
                bot._gateway_disconnect_at = now  # type: ignore[attr-defined]

            disc_at = getattr(bot, "_gateway_disconnect_at", None)
            if had_ready and disc_at is not None:
                stale = now - float(disc_at)
                if stale >= disconnect_sec:
                    logging.error(
                        "Gateway stuck disconnected for %.0fs (>= %.0fs) — closing client for full reconnect.",
                        stale,
                        disconnect_sec,
                    )
                    bot._gateway_restart_requested = True
                    try:
                        await bot.close()
                    except Exception:
                        logging.debug("gateway watchdog close() failed", exc_info=True)
                    return
                if (
                    log_every > 0
                    and stale >= 60
                    and (now - float(getattr(bot, "_gateway_last_stuck_log_at", 0.0))) >= log_every
                ):
                    bot._gateway_last_stuck_log_at = now  # type: ignore[attr-defined]
                    logging.warning(
                        "Gateway still disconnected (%.0fs elapsed, %.0fs until forced reconnect).",
                        stale,
                        max(0.0, disconnect_sec - stale),
                    )

            if not had_ready:
                boot_stale = now - float(getattr(bot, "_gateway_session_started_at", now))
                if boot_stale >= initial_sec:
                    logging.error(
                        "Gateway never reached READY after %.0fs (>= %.0fs) — closing client.",
                        boot_stale,
                        initial_sec,
                    )
                    bot._gateway_restart_requested = True
                    try:
                        await bot.close()
                    except Exception:
                        logging.debug("gateway watchdog close() failed", exc_info=True)
                    return
                if (
                    log_every > 0
                    and boot_stale >= 60
                    and (now - float(getattr(bot, "_gateway_last_stuck_log_at", 0.0))) >= log_every
                ):
                    bot._gateway_last_stuck_log_at = now  # type: ignore[attr-defined]
                    logging.warning(
                        "Still waiting for first READY (%.0fs elapsed, %.0fs until forced reconnect).",
                        boot_stale,
                        max(0.0, initial_sec - boot_stale),
                    )
    except asyncio.CancelledError:
        logging.debug("Gateway stuck watchdog cancelled")
        raise


def _outbox_match_key(row):
    if row.get('match_key'):
        return row['match_key']
    key = str(row.get('dedupe_key') or '')
    # Compatibility with the existing mafia_kind:guild:match:user format.
    if key.startswith('mafia_') and key.count(':') >= 3:
        return key.rsplit(':', 1)[0].split(':', 2)[2]
    return None


async def _dm_outbox_pump_loop() -> None:
    """Drain queued messages off the event loop and validate their match at delivery."""
    from gameplay.lifecycle import message_lock
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            db = getattr(bot, 'db', None)
            if db:
                await run_blocking(db.requeue_stale_dm_outbox_sending, stale_after_seconds=300)
                rows = await run_blocking(db.claim_dm_outbox_batch, limit=25)
                for row in rows:
                    mid, uid, gid = int(row['id']), int(row['target_user_id']), int(row['guild_id'])
                    try:
                        user = bot.get_user(uid) or await bot.fetch_user(uid)
                        async with message_lock(gid):
                            match = _outbox_match_key(row)
                            current = active_games.get(gid)
                            if match and current is None:
                                # READY may still be recovering the saved game. Check
                                # its identity before even sending a Game Over notice.
                                data = await run_blocking(load_state, gid)
                                if data and data.get('in_progress'):
                                    if data.get('game_key') != match:
                                        await run_blocking(db.mark_dm_outbox_superseded, mid)
                                        continue
                                    if row['kind'] != 'game_over':
                                        await run_blocking(db.defer_dm_outbox, mid)
                                        continue
                            obsolete = bool(match and (
                                (current is not None and current.game_key not in (None, match)) or
                                (row['kind'] != 'game_over' and (current is None or not current.in_progress
                                    or current.ending or current.game_key != match))))
                            if obsolete:
                                await run_blocking(db.mark_dm_outbox_superseded, mid)
                                continue
                            await finish_pending(user.send(str(row['content'])))
                            await run_blocking(db.mark_dm_outbox_sent, mid)
                    except discord.HTTPException as error:
                        delay = min(600, int(getattr(error, 'retry_after', 60) or 60) + 5) if error.status == 429 else 120
                        await run_blocking(db.retry_dm_outbox_later, mid, error=type(error).__name__, delay_seconds=delay)
                    except Exception as error:
                        await run_blocking(db.retry_dm_outbox_later, mid, error=type(error).__name__, delay_seconds=90)
        except Exception:
            logging.exception('dm_outbox pump iteration failed')
        await asyncio.sleep(12)


# ==========================================
# GATEWAY / SESSION OBSERVABILITY
# ==========================================
# discord.py reconnects the websocket automatically for many failures; these hooks help ops logs.
@bot.event
async def on_disconnect() -> None:
    logging.warning("Discord gateway disconnected — client will attempt to reconnect.")
    if getattr(bot, "_gateway_had_ready", False):
        bot._gateway_disconnect_at = time.monotonic()  # type: ignore[attr-defined]


@bot.event
async def on_resumed() -> None:
    logging.info("Discord gateway resumed (same session where supported).")


# ==========================================
# HYBRID COMMAND HELPERS (slash + prefix)
# ==========================================
async def _get_allowed_guild() -> Optional[discord.Guild]:
    # In DMs, interaction.guild is None; we still need a guild context for living lists.
    return bot.get_guild(ALLOWED_GUILD_ID)


async def _living_slot_choices_for_user(user_id: int) -> List[discord.app_commands.Choice[int]]:
    game = get_game_by_player_id(user_id)
    if not game or not game.in_progress:
        return []
    # Autocomplete callbacks fire on every keystroke; avoid network fetches.
    # Use the cached living list; command handlers will do a fresh sync/validation.
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


async def _autocomplete_living_slot(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice[int]]:
    # `current` is ignored for now; we keep a stable short list.
    return await _living_slot_choices_for_user(interaction.user.id)


def _exclude_choice(choices: List[discord.app_commands.Choice[int]], exclude_value: Optional[int]) -> List[discord.app_commands.Choice[int]]:
    if not exclude_value:
        return choices
    return [c for c in choices if c.value != exclude_value]


async def _autocomplete_living_slot_excluding(interaction: discord.Interaction, current: str, *, exclude_param: str) -> List[discord.app_commands.Choice[int]]:
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
    base = await _living_slot_choices_for_user(interaction.user.id)
    try:
        return _exclude_choice(base, int(exclude_val) if exclude_val is not None else None)
    except (TypeError, ValueError):
        return base

# ==========================================
# LAST WILL (DM MODAL)
# ==========================================
class WillModal(discord.ui.Modal, title="Edit your Last Will"):
    will = discord.ui.TextInput(
        label="Last Will",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1800,
        placeholder="Type your will here...",
    )

    def __init__(self, *, game: Game, owner_id: int, current_text: str) -> None:
        super().__init__()
        self.game, self.owner_id, self.match_key = game, owner_id, game.game_key
        self.will.default = (current_text or "")[:1800]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        game = self.game
        def save():
            gameplay_state.require_current(game)
            if interaction.user.id != self.owner_id or game.game_key != self.match_key:
                raise gameplay_state.Rejected("This editor belongs to an earlier game. Open !will again.")
            if game.resolving:
                raise gameplay_state.Rejected("Night is resolving. Please wait.")
            if self.owner_id not in {p.id for p in game.living_players}:
                raise gameplay_state.Rejected("Only living players can edit their wills.")
            game.role_states.setdefault(interaction.user.id, {})["will"] = str(self.will.value or "")[:1800]
        try:
            await gameplay_state.commit(game, save)
            await private_reply(interaction, "Saved your will.")
        except gameplay_state.Rejected as error:
            await private_reply(interaction, str(error))
        except OSError:
            await private_reply(interaction, "Your will could not be saved. Please try again.")


class WillView(discord.ui.View):
    def __init__(self, *, game: Game, owner_id: int, current_text: str) -> None:
        super().__init__(timeout=300)
        self.game, self.match_key = game, game.game_key
        self.owner_id = owner_id
        self.current_text = current_text

    @discord.ui.button(label="Edit Will", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This isn't your will editor.", ephemeral=True)
        # Pull latest will text at click time (avoid overwriting with a stale prefill).
        game = self.game
        try:
            gameplay_state.require_current(game)
            if game.game_key != self.match_key:
                raise gameplay_state.Rejected("This editor belongs to an earlier game. Open !will again.")
            if self.owner_id not in {p.id for p in game.living_players}:
                raise gameplay_state.Rejected('Only living players can edit their wills.')
        except gameplay_state.Rejected as error:
            return await private_reply(interaction, str(error))
        latest = str(game.role_states.get(interaction.user.id, {}).get("will", "") or "")
        await interaction.response.send_modal(WillModal(game=game, owner_id=self.owner_id, current_text=latest))


# ==========================================
# LEADERBOARD UI (SLASH)
# ==========================================
class LeaderboardView(discord.ui.View):
    def __init__(self, *, invoker_id: int, guild_id: int, db: Database) -> None:
        super().__init__(timeout=300)
        self.invoker_id = int(invoker_id)
        self.guild_id = int(guild_id)
        self.db = db
        self.message: Optional[discord.Message] = None
        self.select = LeaderboardSelect(view=self)
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            try:
                await interaction.response.send_message("Only the command invoker can use these controls.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class LeaderboardSelect(discord.ui.Select):
    def __init__(self, *, view: LeaderboardView) -> None:
        self._lb_view = view
        options = [
            discord.SelectOption(label="Total wins (incl. personal)", value="total", description="Top total wins"),
            discord.SelectOption(label="Winrate (min 5 games)", value="winrate", description="Wins / games"),
            discord.SelectOption(label="Town wins", value="town", description="Final Town wins"),
            discord.SelectOption(label="Mafia wins", value="mafia", description="Final Mafia wins"),
            discord.SelectOption(label="Arsonist wins", value="arsonist", description="Final Arsonist wins"),
            discord.SelectOption(label="Pirate (personal)", value="pirate_win", description="2 plunders achieved"),
            discord.SelectOption(label="Executioner (personal)", value="exe_win", description="Target lynched"),
            discord.SelectOption(label="Jester (personal)", value="jester_win", description="Lynched"),
            discord.SelectOption(label="Survivor (personal)", value="survivor_survived", description="Survived to end"),
            discord.SelectOption(label="Chaos (personal)", value="chaos_survived", description="Survived to end"),
            discord.SelectOption(label="Witch (personal)", value="witch_town_loses", description="Alive when Town loses"),
            discord.SelectOption(label="Arsonist (personal)", value="arsonist_win", description="Won as Arsonist"),
        ]
        super().__init__(placeholder="Choose leaderboard…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        # Acknowledge quickly to avoid "interaction failed" under slow disk/locked DB.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return
        key = str(self.values[0])
        v = self._lb_view
        embed = await _build_leaderboard_embed(db=v.db, guild_id=v.guild_id, page=key)
        try:
            await interaction.edit_original_response(embed=embed, view=v)
        except discord.HTTPException:
            pass


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
        # Personal win pages are stored in player_personal_stats.
        rows = await _to_thread(db.top_personal, guild_id=guild_id, key=page, limit=10)
        subtitle = f"Personal: {page}"
        lines = [f"**{i}.** <@{r.player_id}> — **{int(r.value)}**" for i, r in enumerate(rows, 1)]

    embed = discord.Embed(title=title, description="\n".join(lines) if lines else "(no data yet)", color=discord.Color.blurple())
    embed.set_footer(text=subtitle)
    return embed

# --- LIFECYCLE ---
@bot.event
async def on_ready() -> None:
    # Count ready cycles: first is cold connect; later ones are usually gateway reconnects.
    bot._mafia_ready_count = int(getattr(bot, "_mafia_ready_count", 0)) + 1  # type: ignore[attr-defined]
    _is_reconnect = bot._mafia_ready_count > 1  # type: ignore[attr-defined]

    # Smoke-test hint: DB lives under `state/mafiabot.db`.
    _dbg(
        "H1",
        "bot.py:on_ready:entry",
        "on_ready entry",
        {
            "ready_count": int(bot._mafia_ready_count),  # type: ignore[attr-defined]
            "is_reconnect": bool(_is_reconnect),
            "pid": int(os.getpid()),
            "inst": _debug_instance,
            "allowed_guild_id": int(ALLOWED_GUILD_ID),
            "connected_guild_ids": _safe_guild_ids(),
            "intents": {
                "message_content": bool(getattr(bot.intents, "message_content", False)),
                "members": bool(getattr(bot.intents, "members", False)),
                "dm_messages": bool(getattr(bot.intents, "dm_messages", False)),
            },
            "prefix_command_count": len(list(getattr(bot, "commands", []))),
            "tree_counts_pre_sync": _safe_tree_counts(),
        },
    )
    try:
        guilds = [f"{g.name} ({g.id})" for g in bot.guilds]
    except Exception:
        guilds = []
    logging.info(
        "Logged in as %s (id=%s). Connected guilds: %s [ready #%s%s]",
        bot.user,
        getattr(bot.user, "id", "unknown"),
        ", ".join(guilds) or "none",
        int(bot._mafia_ready_count),  # type: ignore[attr-defined]
        "; reconnect" if _is_reconnect else "",
    )

    # Best-effort: ensure hybrid/slash commands are synced.
    # Guild sync is fast; global sync enables DM usage but may take time to propagate.
    # Skip full sync on gateway reconnects to avoid Discord rate limits / unnecessary churn.
    _force_sync = os.environ.get("MAFIA_FORCE_COMMAND_SYNC", "").strip().lower() in ("1", "true", "yes", "on")
    if int(bot._mafia_ready_count) == 1 or _force_sync:  # type: ignore[attr-defined]
        try:
            if hasattr(bot.tree,'copy_global_to'):
                bot.tree.copy_global_to(guild=discord.Object(id=ALLOWED_GUILD_ID))
            synced = await bot.tree.sync(guild=discord.Object(id=ALLOWED_GUILD_ID))
            _dbg("H2", "bot.py:on_ready:guild_sync", "guild sync ok", {"count": len(synced)})
        except Exception:
            _dbg("H2", "bot.py:on_ready:guild_sync", "guild sync failed", {"allowed_guild_id": int(ALLOWED_GUILD_ID)})
            logging.exception("Failed to sync app commands for allowed guild.")
        try:
            synced = [] if os.environ.get('MAFIABOT_TEST_PROFILE') == '1' else await bot.tree.sync()
            _dbg("H3", "bot.py:on_ready:global_sync", "global sync ok", {"count": len(synced)})
        except Exception:
            _dbg("H3", "bot.py:on_ready:global_sync", "global sync failed", {})
            logging.exception("Failed to sync global app commands.")

        _dbg("H4", "bot.py:on_ready:post_sync", "post-sync command counts", {"tree_counts_post_sync": _safe_tree_counts()})
    else:
        logging.info(
            "Skipping slash command tree sync (ready session #%s). Set MAFIA_FORCE_COMMAND_SYNC=1 to force.",
            int(bot._mafia_ready_count),  # type: ignore[attr-defined]
        )

    # Initialize SQLite DB (leaderboards/history). Non-fatal if it fails.
    if not getattr(bot, "db", None):
        try:
            db_path = str(persistence.STATE_DIR / "mafiabot.db")
            bot.db = Database(db_path)  # type: ignore[attr-defined]
            await run_blocking(bot.db.initialize)  # type: ignore[attr-defined]
        except Exception:
            logging.exception("Failed to initialize SQLite DB (leaderboards disabled).")
            bot.db = None

    bot._gateway_had_ready = True  # type: ignore[attr-defined]
    bot._gateway_disconnect_at = None  # type: ignore[attr-defined]
    _ensure_gateway_watchdog_task()

    if not getattr(bot, "_mafia_dm_outbox_started", False):
        bot._mafia_dm_outbox_started = True  # type: ignore[attr-defined]
        bot._mafia_dm_outbox_task = asyncio.create_task(_dm_outbox_pump_loop())

    guild = bot.get_guild(ALLOWED_GUILD_ID)
    try:
        await _restore_saved_game(guild)
    except (OSError, discord.HTTPException):
        logging.exception('Saved game recovery pending; retaining the snapshot.')
        bot.gameplay_controller.start_job((ALLOWED_GUILD_ID, 'restore', 'current'), lambda: _restore_saved_game(guild))


async def _restore_saved_game(guild):
    from gameplay.lifecycle import recovery_lock
    async with recovery_lock(ALLOWED_GUILD_ID):
        await _restore_saved_game_locked(guild)


async def _restore_saved_game_locked(guild):
    # Attempt to restore persisted game state for the allowed guild.
    if guild:
        # READY can fire again while trial/duel/resolution tasks are running.
        # Those tasks and new commands must keep the same Game object.
        if (ALLOWED_GUILD_ID in active_games and not active_games[ALLOWED_GUILD_ID]._rehydrate_pending
                and not active_games[ALLOWED_GUILD_ID]._recovering_permissions
                and not getattr(bot, "_mafia_full_reconnect", False)):
            logging.info("Preserving live game state on READY for guild %s.", ALLOWED_GUILD_ID)
            return
        # Set before the first await to prevent overlapping READY restores.
        bot._mafia_state_restore_started = True
        existing = active_games.get(ALLOWED_GUILD_ID)
        data = existing.to_persisted() if existing else await run_blocking(load_state, ALLOWED_GUILD_ID)
        # A command may have restored the game while the disk read was pending.
        # Always hydrate the canonical object from its own snapshot.
        existing = active_games.get(ALLOWED_GUILD_ID)
        if existing:
            data = existing.to_persisted()
        _dbg(
            "H5",
            "bot.py:on_ready:restore:pre",
            "restore check",
            {"allowed_guild_present": True, "has_persisted_state": bool(data)},
        )
        if data:
            try:
                game = existing or Game.from_persisted(data)
                if not game._rehydrate_pending:
                    game._persist_player_ids = list(data.get('player_ids', []))
                    game._persist_living_ids = list(data.get('living_ids', []))
                game._rehydrate_pending = True
                game._recovering_permissions = game.in_progress
                active_games[ALLOWED_GUILD_ID] = game
                await game.ensure_rehydrated(guild)
                # Do not publish a half-rehydrated game or replace one that
                # commands created while member fetching was in progress.
                if active_games.get(ALLOWED_GUILD_ID) is not game or game.ending:
                    return
                logging.info(f"Restored persisted game state for guild {ALLOWED_GUILD_ID}.")


                if game.in_progress:
                    from gameplay.access import reconcile
                    async with game._startup_lock:
                        def guard():
                            if active_games.get(game.guild_id) is not game or game.ending:
                                raise gameplay_state.Rejected('This recovery was cancelled.')
                        await reconcile(game, guild, guard)
                        guard()
                        game._recovering_permissions = False

                bot._mafia_full_reconnect = False
                if game.in_progress:
                    await bot.gameplay_controller.recover(game)
                if game.gameplay.get("trial"):
                    return
                # Old reaction trials contain no durable ballot session. Clean them up
                # rather than reconstructing a verdict from incomplete recovery data.
                resume_defense = False
                t_deadline = getattr(game, "tribunal_defense_deadline_utc", None)
                # Best-effort repair: unstick day VC permissions unless we're mid-defense (resume will continue).
                if game.in_progress and game.phase == "day" and game.day_vc_id and game.alive_role_id:
                    if not (game.vote_in_progress and getattr(game, "tribunal_subphase", None) == "defense"):
                        day_vc = guild.get_channel(game.day_vc_id)
                        alive_role = guild.get_role(game.alive_role_id)
                        if day_vc and alive_role:
                            try:
                                await day_vc.set_permissions(alive_role, connect=True, speak=True)
                            except discord.HTTPException:
                                pass

                # Crash recovery: abort tribunal if we cannot resume defens (B4 floor / overdue / invalid).
                if (
                    not resume_defense
                    and game.in_progress
                    and game.phase == "day"
                    and (getattr(game, "tribunal_muted", False) or getattr(game, "tribunal_defendant_id", None))
                ):
                    stand_role = guild.get_role(game.stand_role_id) if getattr(game, "stand_role_id", None) else None
                    defendant_id = getattr(game, "tribunal_defendant_id", None)
                    if defendant_id and stand_role:
                        try:
                            m = await game.get_member_safe(guild, int(defendant_id))
                            if m and stand_role in m.roles:
                                await m.remove_roles(stand_role)
                        except discord.HTTPException:
                            pass

                    if game.day_vc_id and game.alive_role_id:
                        day_vc2 = guild.get_channel(game.day_vc_id)
                        alive_role2 = guild.get_role(game.alive_role_id)
                        if day_vc2 and alive_role2:
                            try:
                                await day_vc2.set_permissions(alive_role2, connect=True, speak=True)
                            except discord.HTTPException:
                                pass

                    def clear_legacy_trial():
                        gameplay_state.require_current(game, phase="day")
                        if game.gameplay.get("trial"):
                            raise gameplay_state.Rejected("A new trial has started.")
                        gameplay_trials.clear_flags(game)
                        game.tribunal_verdict_committed = False
                    try:
                        await gameplay_state.commit(game, clear_legacy_trial)
                    except gameplay_state.Rejected:
                        return
                    gc = bot.get_channel(game.game_channel_id) if game.game_channel_id else None
                    if isinstance(gc, discord.TextChannel):
                        try:
                            dtp2 = _parse_iso_utc(t_deadline)
                            rem2 = (dtp2 - datetime.now(timezone.utc)).total_seconds() if dtp2 else -99999.0
                            if rem2 < 0:
                                msg = "⚠️ **Trial reset:** bot restarted — defense phase already overdue on wall-clock."
                            elif rem2 < TRIBUNAL_RESUME_MIN_SECONDS:
                                msg = (
                                    "⚠️ **Trial reset:** bot restarted with insufficient time remaining "
                                    f"to resume safely (<{TRIBUNAL_RESUME_MIN_SECONDS}s)."
                                )
                            else:
                                msg = "⚠️ **Trial reset:** bot restarted during tribunal cleanup — could not resume."
                            await gc.send(msg)
                        except discord.HTTPException as e:
                            logging.warning(
                                "Tribunal restart announcement failed guild_id=%s channel_id=%s: %s",
                                guild.id,
                                getattr(gc, "id", None),
                                e,
                            )
            except Exception:
                raise
    else:
        _dbg(
            "H5",
            "bot.py:on_ready:restore:pre",
            "allowed guild not connected (no restore)",
            {"allowed_guild_present": False, "allowed_guild_id": int(ALLOWED_GUILD_ID)},
        )

_night_decorator = only_during_night_gameplay_factory(
    bot=bot,
    get_game_by_player_id=get_game_by_player_id,
)

# Keep backwards-compatible call style: existing commands use `@only_during_night_gameplay()`.
def only_during_night_gameplay():
    return _night_decorator


# ==========================================
# ERROR HANDLER
# ==========================================
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    await on_command_error_handler(ctx, error)

# region agent log (debug)
@bot.event
async def on_message(message: discord.Message) -> None:
    # Debug prefix-command visibility without logging message contents.
    try:
        if message.author and getattr(message.author, "bot", False):
            return
        content = message.content or ""
        _dbg(
            "H1",
            "bot.py:on_message",
            "message received",
            {
                "guild_id": int(message.guild.id) if message.guild else None,
                "is_dm": message.guild is None,
                "content_len": len(content),
                "has_bang_prefix": content.startswith("!"),
                "allowed_guild_id": int(ALLOWED_GUILD_ID),
            },
        )
    except Exception:
        pass
    await bot.process_commands(message)
# endregion


# ==========================================
# GUILD LOCKING
# ==========================================
@bot.check
async def enforce_allowed_guild_check(ctx: commands.Context) -> bool:
    allowed = await enforce_allowed_guild(ctx, allowed_guild_id=ALLOWED_GUILD_ID)
    if allowed and ctx.guild:
        game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
        await game.ensure_rehydrated(ctx.guild)
    return allowed


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    if guild.id != ALLOWED_GUILD_ID:
        logging.warning(f"Joined unauthorized guild {guild.id}; leaving.")
        try:
            await guild.leave()
        except discord.HTTPException:
            pass


# ==========================================
# SLASH: LEADERBOARD
# ==========================================
@bot.tree.command(name="leaderboard", description="Show leaderboards for this server.")
async def leaderboard_slash(interaction: discord.Interaction) -> None:
    # Explicit allowed-guild enforcement (prefix @bot.check does not apply to app commands).
    if not interaction.guild or interaction.guild.id != ALLOWED_GUILD_ID:
        return await interaction.response.send_message("🛑 This bot is locked to a different server.", ephemeral=True)

    db = getattr(bot, "db", None)
    if not db:
        return await interaction.response.send_message("Leaderboards are not available (DB not initialized).", ephemeral=True)

    # Defer immediately to avoid Discord's ~3s response deadline.
    await interaction.response.defer()
    embed = await _build_leaderboard_embed(db=db, guild_id=interaction.guild.id, page="total")
    view = LeaderboardView(invoker_id=interaction.user.id, guild_id=interaction.guild.id, db=db)
    msg = await interaction.followup.send(embed=embed, view=view, wait=True)
    view.message = msg


# ==========================================
# GM / LOBBY COMMANDS
# ==========================================
@bot.command(name='join')
@commands.guild_only()
async def join_game_command(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    def join():
        if game.in_progress or game.ending:
            raise gameplay_state.Rejected("A game is already in progress!")
        if ctx.author.id in [p.id for p in game.players]:
            raise gameplay_state.Rejected("You are already on the waiting list!")
        game.players.append(ctx.author)
    try:
        await gameplay_state.commit(game, join)
        await ctx.send(f"{ctx.author.mention} joined! Total players: {len(game.players)}." + _tag())
    except gameplay_state.Rejected as error:
        await ctx.send(str(error))


@bot.command(name="leave")
@commands.guild_only()
async def leave_game_command(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    def leave():
        if game.in_progress or game.ending:
            raise gameplay_state.Rejected("The game has already started; leaving the queue is not available.")
        if ctx.author.id not in [p.id for p in game.players]:
            raise gameplay_state.Rejected("You are not in the join queue. Use !join to enter.")
        game.players = [p for p in game.players if p.id != ctx.author.id]
    try:
        await gameplay_state.commit(game, leave)
        await ctx.send(f"{ctx.author.mention} left the queue. Total players: {len(game.players)}." + _tag())
    except gameplay_state.Rejected as error:
        await ctx.send(str(error))


@bot.command(name='players')
@commands.guild_only()
async def show_players_command(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.players:
        return await ctx.send("No one has joined yet. Use `!join` to enter.")

    player_mentions = [f'#{game.player_slots.get(p.id, index)}: {p.mention}' for index, p in enumerate(game.players, 1)]
    living_mentions = [f'#{game.player_slots.get(p.id, index)}: {p.mention}' for index, p in enumerate(game.living_players, 1)]

    response = f"**Waiting Players ({len(game.players)}):**\n" + ", ".join(player_mentions)
    if game.living_players:
        response += f"\n\n**Living Players ({len(game.living_players)}):**\n" + ", ".join(living_mentions)
    from discord_output import chunk_lines
    for chunk in chunk_lines(response.splitlines(), max_chars=1900):
        await ctx.send(chunk, allowed_mentions=NO_MENTIONS)


@bot.command(name='startgame')
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def startgame(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    try:
        await _startgame(ctx, game)
    except gameplay_state.Rejected as error:
        await ctx.send(str(error))


async def _startgame(ctx, game):
    async with game._startup_lock:
        await _startgame_impl(ctx, game)


async def _startgame_impl(ctx, game):
    if game.in_progress or game.ending:
        return await ctx.send("A game is already in progress or being cleaned up!")

    # Keep recovered endgame markers until their statistics are committed;
    # starting a new match must not overwrite the previous match's snapshot.
    from game_recovery import _pending_endgame_meta, disk_recovery_blocked_reason
    if await run_blocking(_pending_endgame_meta, game.guild_id):
        if not await run_blocking(Game.commit_pending_endgame_if_any, game.guild_id):
            return await ctx.send('Previous match statistics are pending. Repair or retry recovery before starting a new game.')
    blocked = await run_blocking(disk_recovery_blocked_reason, game.guild_id)
    if blocked:
        return await ctx.send(blocked)

    expected_roster = tuple(p.id for p in game.players)
    valid_players = []
    for p in game.players:
        member = await game.lookup_member(ctx.guild, p.id)
        if member:
            valid_players.append(member)

    if len(valid_players) != len(game.players):
        await ctx.send(f"⚠️ Removed {len(game.players) - len(valid_players)} player(s) who left before the game started.")

    def refresh_lobby():
        if (active_games.get(game.guild_id) is not game or game.in_progress or game.ending
                or tuple(p.id for p in game.players) != expected_roster):
            raise gameplay_state.Rejected('The lobby changed during startup. Run !startgame again.')
        game.players = valid_players
        return tuple(p.id for p in game.players)
    expected_roster = await gameplay_state.commit(game,refresh_lobby)
    player_count = len(game.players)

    if player_count < 5:
        return await ctx.send("You need at least 5 players to start.")

    failed_dms = []
    for p in game.players:
        try:
            msg = await p.send("Checking DM permissions for game start...")
        except discord.HTTPException:
            failed_dms.append(p.display_name)
            continue

        # Deleting the check message is nice-to-have; don't treat delete failures as DM failures.
        try:
            await msg.delete()
        except discord.HTTPException:
            pass

    if failed_dms:
        return await ctx.send(f"🛑 **Game Start Aborted!** The following players have DMs disabled: {', '.join(failed_dms)}. They must enable DMs from server members to play.")

    try:
        await game.setup_infrastructure(ctx.guild)
    except RuntimeError as e:
        return await ctx.send(f"🛑 **Game Start Aborted!** {e}")
    # Prefer the dedicated day text channel for all announcements, if available.
    if getattr(game, "day_tc_id", None):
        try:
            day_tc = ctx.guild.get_channel(game.day_tc_id)
            me = ctx.guild.me
            if not me and ctx.bot.user:
                me = ctx.guild.get_member(ctx.bot.user.id)

            bot_can_send = False
            players_can_view = False
            if isinstance(day_tc, discord.TextChannel) and me:
                perms_me = day_tc.permissions_for(me)
                perms_default = day_tc.permissions_for(ctx.guild.default_role)
                bot_can_send = perms_me.send_messages and perms_me.view_channel
                players_can_view = perms_default.view_channel

            if isinstance(day_tc, discord.TextChannel) and bot_can_send and players_can_view:
                announcement_channel_id = day_tc.id
                if day_tc.id != ctx.channel.id:
                    await ctx.send(f"✅ Game channels ready. Use {day_tc.mention} for day chat and announcements.")
            else:
                announcement_channel_id = ctx.channel.id
        except discord.HTTPException:
            announcement_channel_id = ctx.channel.id
    else:
        announcement_channel_id = ctx.channel.id

    roles_for_this_game = game_roles.draw_roles_for_startgame(player_count, rng=random)

    random.shuffle(roles_for_this_game)

    def initialize_game():
        if (active_games.get(game.guild_id) is not game or game.in_progress or game.ending
                or tuple(p.id for p in game.players) != expected_roster):
            raise gameplay_state.Rejected('A game is already starting.')
        random.shuffle(game.players)
        game.living_players = game.players.copy()
        # Stable targeting numbers: these should NOT shift when the living list order changes.
        game.player_slots = {p.id: i + 1 for i, p in enumerate(game.players)}
        game.player_roles = {p.id: r for p, r in zip(game.players, roles_for_this_game)}
        game.game_channel_id = announcement_channel_id
        game.in_progress = True
        game.phase = "day"
        game.day_number = 1
        # Persisted idempotency key for history/stat commits.
        # Stored on the Game object (not in role_states) to keep from_persisted() int-key coercion safe.
        game.started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        game.game_key = f"{ctx.guild.id}:{game.started_at}:{secrets.token_hex(8)}"
        # Endgame stats commit must be per-game idempotent; reset between games.
        game.stats_committed = False
        # Clear any stale per-game state from aborted runs.
        game.resolving = False
        game.vote_in_progress = False
        game.votes_today = 0
        game.graveyard = []
        game.night_actions, game.role_states, game.doused_players = {}, {}, set()

        game.gameplay = {"version": 1, "trial": None, "panels": {}, "night_token": None, "deaths": {}, "duels": {}, "resolutions": {},
            "startup": {"match": game.game_key, "complete": False, "announced": False, "completed_players": []}}
        for p_id, role in game.player_roles.items():
            state: Dict = {}
            if role == "Vigilante":    state = {"shots_remaining": 1, "will_die_of_guilt": False, "guilty_tomorrow": False}
            elif role == "Gravedigger": state = {"uses_remaining": 1}
            elif role == "Survivor":   state = {"vests_remaining": role_starting_charges(player_count=player_count)}
            elif role == "Mayor":      state = {"is_revealed": False}
            elif role == "Doctor":     state = {"self_heals_remaining": 1}
            elif role == "Bodyguard":  state = {"uses_remaining": 1, "self_protects_remaining": 1}
            elif role == "Witch":      state = {"has_learned_role": False, "night1_shield_used": False}
            elif role == "Gatekeeper": state = {"uses_remaining": role_starting_charges(player_count=player_count)}
            elif role == "Scary Grandma": state = {"alerts_remaining": role_starting_charges(player_count=player_count)}
            elif role == "Mole":       state = {"uses_remaining": 1}
            elif role == "Tailor":     state = {"uses_remaining": 1}
            elif role == "Pirate":     state = {"wins": 0}
            elif role == "Retributionist": state = {"uses_remaining": role_starting_charges(player_count=player_count), "used_corpses": []}
            elif role == "Chaos":      state = {"uses_remaining": chaos_starting_uses(player_count), "night1_shield_used": False}
            elif role == "Deputy": state = {"deputy_shots_remaining": 1, "deputy_fired_day": 0}
            elif role == "Seer": state = {"seer_pair_history": []}
            elif role == "Serial Killer": state = {"sk_cautious": False, "sk_target_id": None}
            elif role == "Guardian Angel": state = {"ga_ward_charges": 1, "ga_defeated": False}
            elif role == "Executioner":
                # ToS-like: target starts as a Town role; exclude Mayor (and self).
                targets = [
                    p.id for p in game.players
                    if p.id != p_id and game.player_roles.get(p.id) in TOWN_ROLES and game.player_roles.get(p.id) != "Mayor"
                ]
                if targets:
                    state = {"exe_target": random.choice(targets)}
                else:
                    game.player_roles[p_id] = "Jester"
                    state = {"can_haunt": False}

            if state:
                game.role_states[p_id] = state

            # Snapshot role_start for honest history/role leaderboards (survives promotions/conversions).
            game.role_states.setdefault(p_id, {})["role_start"] = role

        for ga_id, role in game.player_roles.items():
            if role == "Guardian Angel":
                pool = guardian_angel_bind_pool_ids([p.id for p in game.players], ga_id)
                if pool:
                    game.role_states[ga_id]["ga_target_id"] = int(random.choice(pool))
    from gameplay.lifecycle import message_lock
    from gameplay import startup
    async with message_lock(game.guild_id):
        await gameplay_state.commit(game, initialize_game)
    try:
        await startup.deliver(game, ctx.guild, client=bot)
        await startup.announce(game, ctx.guild)
    except (OSError, discord.HTTPException):
        logging.warning('Startup delivery pending guild_id=%s', game.guild_id)
        bot.gameplay_controller.start_job((game.guild_id, 'startup', game.game_key),
            lambda: startup.resume(game, ctx.guild, client=bot))
        await ctx.send('The role assignment is saved. Server setup is incomplete and will retry; gameplay opens when it finishes.')
    logging.info('Game assigned on guild %s with %s players.', ctx.guild.id, player_count)


@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def night(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress:
        return await ctx.send("No game is in progress.")
    await game.start_night(ctx)


@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def day(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress:
        return await ctx.send("No game is in progress.")
    if game.phase == "day":
        return await ctx.send("It is already day.")
    await game.start_day(ctx)


@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def resolve(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    try:
        record = game.gameplay.get("resolution")
        if game.phase == "night" and record and record.get("applied") and not record.get("progressed"):
            await resolution.finish(game, ctx)
        else:
            await resolution.run(game, ctx)
    except gameplay_state.Rejected as error:
        await ctx.send(str(error))


@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
async def status(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress or game.phase != "night":
        return await ctx.send("This command can only be used during the night phase.", delete_after=10)

    await game.sync_living_players(ctx.guild)
    living_ids = await game.get_living_ids(ctx.guild)
    if ctx.author.id in living_ids:
        return await ctx.send("🛑 **Anti-Cheat:** You cannot check the status list while you are an active player!", delete_after=10)

    acted, waiting_for = [], []
    action_roles = {
        "Mobster", "Doctor", "Escort", "Consort", "Sheriff", "Investigator", "Framer", "Gravedigger", "Vigilante",
        "Transporter", "Bodyguard", "Lookout", "Tracker", "Witch", "Arsonist", "Hypnotist", "Mole", "Tailor", "Pirate", "Gatekeeper", "Survivor", "Scary Grandma",
        "Retributionist", "Chaos",
        "Serial Killer", "Seer", "Guardian Angel",
    }

    for p_id, role in game.player_roles.items():
        if p_id not in living_ids or role not in action_roles:
            continue
        player = await game.get_member_safe(ctx.guild, p_id)
        if not player:
            continue

        has_acted = p_id in game.night_actions and (role != "Pirate" or game.night_actions[p_id].get("duel_finished", False))
        if role in ["Survivor", "Scary Grandma"] and not has_acted:
            continue
        (acted if has_acted else waiting_for).append(f"- {player.display_name} ({role})")

    from discord_output import section_embeds
    embeds = section_embeds(f'🌙 Night {game.day_number} Status',
        [('✅ Acted', acted or ['None yet.']), ('⏳ Waiting For', waiting_for or ['All in!'])], color=discord.Color.blue())
    try:
        for embed in embeds:
            await ctx.author.send(embed=embed, allowed_mentions=NO_MENTIONS)
    except discord.Forbidden:
        return await ctx.send("I can't DM you! Check your privacy settings.", delete_after=10)
    except discord.HTTPException:
        return await ctx.send('Discord could not deliver the status. Please retry shortly.', delete_after=10)
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    await ctx.send('Night status sent to your DMs.', delete_after=5)


@bot.command(name='slay')
@commands.check_any(commands.has_permissions(administrator=True), commands.has_role(GAME_OVERSEER_ROLE_ID))
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def slay_command(ctx: commands.Context, member: discord.Member, *, message: Optional[str] = None) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    await game.process_death(ctx, member, cause="manual", custom_message=message)
    await game.check_win_conditions()


@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def reset(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    await game.reset(ctx.guild)
    await ctx.send("🔄 **Game has been manually reset.** All state and permissions cleared.")


@bot.command(name="nukereset")
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def nukereset(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    await ctx.send("⚠️ **NUKE RESET** in progress… this may take a bit.")
    try:
        await game.nuke_reset(ctx.guild)
    except Exception:
        logging.exception("Nuke reset failed")
        return await ctx.send("🛑 Nuke reset failed — check logs. You may need to fix permissions or delete channels manually.")
    await ctx.send("☢️ **Nuke reset complete.** Roles, permissions, channels, and saved state were cleaned up.")


# ==========================================
# VOTE / TRIBUNAL
# ==========================================
def _parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        raw = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0)
    except Exception:
        return None


class _ChanCtx:
    """Minimal ctx for ``Game.start_night`` when only ``channel`` / ``guild`` are required."""

    __slots__ = ("channel", "guild", "bot")

    def __init__(self, channel: discord.TextChannel):
        self.channel = channel
        self.guild = channel.guild
        self.bot = bot

    async def send(self, *args, **kwargs):
        return await self.channel.send(*args, **kwargs)


async def _cancel_legacy_tribunal_notice(channel) -> None:
    """Best-effort notice: missing permissions may also prevent cancellation delivery."""
    logging.warning("Legacy Tribunal cancelled because judgment controls were unavailable.")
    try:
        await channel.send("The trial has been cancelled because Discord could not deliver or read the judgment controls.")
    except discord.HTTPException:
        pass


async def _complete_tribunal_after_defense(
    channel: discord.TextChannel,
    game: Game,
    defendant: discord.Member,
    current_day: int,
    alive_role: Optional[discord.Role],
    stand_role: Optional[discord.Role],
    day_vc: Optional[discord.abc.GuildChannel],
) -> None:
    """Post-defense → judgment → verdict (B4); shared by live ``!vote`` and restart resume."""
    guild = channel.guild
    if getattr(game, "tribunal_verdict_committed", False):
        return

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    if defendant.id not in living_ids:
        game.votes_today = max(0, game.votes_today - 1)
        await game.persist_flush()
        await channel.send("The trial has been cancelled — the defendant is no longer alive.")
        return

    if stand_role:
        try:
            await defendant.remove_roles(stand_role)
        except discord.HTTPException:
            pass

    if day_vc and alive_role:
        try:
            await day_vc.set_permissions(alive_role, speak=True)
        except discord.HTTPException:
            pass
        game.tribunal_muted = False

    game.tribunal_defense_deadline_utc = None
    j_end = (datetime.now(timezone.utc) + timedelta(seconds=30)).replace(microsecond=0)
    game.tribunal_judgment_deadline_utc = j_end.isoformat()
    game.tribunal_subphase = "judgment"
    await game.persist_flush()

    j_embed = discord.Embed(
        title=f"⚖️ JUDGMENT: {defendant.display_name.upper()} ⚖️",
        description="✅ — Guilty\n❌ — Innocent\n*(30 seconds)*",
        color=discord.Color.dark_red(),
    )
    try:
        judgment_msg = await channel.send(embed=j_embed)
        await judgment_msg.add_reaction("✅")
        await judgment_msg.add_reaction("❌")
    except (discord.Forbidden, discord.HTTPException):
        await _cancel_legacy_tribunal_notice(channel)
        return

    game.tribunal_judgment_message_id = judgment_msg.id
    await game.persist_flush()

    await asyncio.sleep(30)

    if not game.is_active("day") or not game.vote_in_progress or game.day_number != current_day:
        return

    try:
        judgment_msg = await channel.fetch_message(judgment_msg.id)
    except discord.HTTPException:
        await _cancel_legacy_tribunal_notice(channel)
        return

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    if defendant.id not in living_ids:
        # Trial cancelled after being placed on stand -> refund the trial use
        game.votes_today = max(0, game.votes_today - 1)
        await game.persist_flush()
        await channel.send("The trial has been cancelled — the defendant is no longer alive.")
        return

    guilty_votes, innocent_votes = 0, 0
    guilty_voters: List[discord.Member] = []
    user_reacts: Dict[int, Set[str]] = {}
    mayor_voted = False

    try:
        for reaction in judgment_msg.reactions:
            if str(reaction.emoji) not in {"✅", "❌"}:
                continue
            async for user in reaction.users():
                if user.id != bot.user.id and user.id in living_ids and user.id != defendant.id:
                    user_reacts.setdefault(user.id, set()).add(str(reaction.emoji))
    except discord.HTTPException:
        # Pagination can fail after some voters were read; never use a partial tally.
        await _cancel_legacy_tribunal_notice(channel)
        return

    resolved_judgments: Dict[int, Optional[str]] = {}
    for uid, reacts in user_reacts.items():
        if "✅" in reacts and "❌" in reacts:
            resolved_judgments[uid] = None
        elif "✅" in reacts:
            resolved_judgments[uid] = "✅"
        elif "❌" in reacts:
            resolved_judgments[uid] = "❌"
        else:
            resolved_judgments[uid] = None

    for uid, vote_type in resolved_judgments.items():
        if not vote_type:
            continue
        is_mayor = game.role_states.get(uid, {}).get("is_revealed", False)
        if is_mayor:
            mayor_voted = True
        weight = 2 if is_mayor else 1

        if vote_type == "✅":
            guilty_votes += weight
            m = await game.get_member_safe(guild, uid)
            if m:
                guilty_voters.append(m)
        elif vote_type == "❌":
            innocent_votes += weight

    game.tribunal_verdict_committed = True
    game.tribunal_judgment_deadline_utc = None
    game.tribunal_judgment_message_id = None
    game.tribunal_subphase = None
    await game.persist_flush()

    result_txt = f"The votes are in: **{guilty_votes} Guilty** vs **{innocent_votes} Innocent**."
    if mayor_voted:
        result_txt += "\n*(The Mayor's revealed vote counted double!)*"
    await channel.send(result_txt)

    if guilty_votes > innocent_votes:
        eligible_haunt_ids = [
            uid for uid in living_ids if uid != defendant.id and resolved_judgments.get(uid) != "❌"
        ]
        eligible_haunt_voters: List[discord.Member] = []
        for uid in eligible_haunt_ids:
            m = await game.get_member_safe(guild, uid)
            if m:
                eligible_haunt_voters.append(m)

        await game.process_death(channel, defendant, "lynch", eligible_haunt_voters, custom_message=f"By majority vote, {defendant.mention} has been sentenced to the gallows.")
        if not await game.check_win_conditions():
            await channel.send("The execution is concluded. The sun sets early tonight...")
            await game.start_night(_ChanCtx(channel))
    else:
        await channel.send(f"The town has spared **{defendant.display_name}**. They step down from the stand.")


async def _cleanup_tribunal(game: Game, guild: discord.Guild, current_day: int) -> None:
    """Shared finalizer for live and restarted trials, including failed trials."""
    defendant_id = game.tribunal_defendant_id
    game.vote_in_progress = False
    game.tribunal_muted = False
    game.tribunal_defendant_id = None
    game.tribunal_defense_deadline_utc = None
    game.tribunal_judgment_deadline_utc = None
    game.tribunal_judgment_message_id = None
    game.tribunal_subphase = None
    game.tribunal_verdict_committed = False
    try:
        if game.in_progress and not getattr(game, "ending", False):
            await game.persist_flush()
    except Exception:
        logging.exception("Failed to persist tribunal cleanup for guild %s.", guild.id)

    stand_role = guild.get_role(game.stand_role_id) if game.stand_role_id else None
    if defendant_id and stand_role:
        try:
            defendant = await game.get_member_safe(guild, defendant_id)
            if defendant:
                await defendant.remove_roles(stand_role)
        except discord.HTTPException:
            pass
    if game.is_active("day") and game.day_number == current_day:
        day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
        alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None
        if day_vc and alive_role:
            try:
                await day_vc.set_permissions(alive_role, speak=True)
            except discord.HTTPException:
                pass


async def _resume_tribunal_defense_after_restart(guild: discord.Guild, remaining: float) -> None:
    """Continue this restored trial, then release its vote/permission locks."""
    game = active_games.get(ALLOWED_GUILD_ID)
    if not game or not game.vote_in_progress or game.tribunal_subphase != "defense":
        return
    current_day = game.day_number
    defendant_id = game.tribunal_defendant_id
    deadline = game.tribunal_defense_deadline_utc

    def owns_trial() -> bool:
        return (
            active_games.get(ALLOWED_GUILD_ID) is game
            and game.day_number == current_day
            and game.tribunal_defendant_id == defendant_id
        )

    try:
        await asyncio.sleep(max(0.0, remaining))
        if not owns_trial() or not game.is_active("day") or not game.vote_in_progress:
            return
        if game.tribunal_subphase != "defense" or game.tribunal_defense_deadline_utc != deadline:
            return
        ch = bot.get_channel(game.game_channel_id)
        if not isinstance(ch, discord.TextChannel) or not defendant_id:
            return
        defendant = await game.get_member_safe(guild, int(defendant_id))
        if not defendant:
            return
        alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None
        stand_role = guild.get_role(game.stand_role_id) if game.stand_role_id else None
        day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
        try:
            await ch.send("⚖️ **Trial resumed** after bot restart — continuing where the defense phase left off.")
        except discord.HTTPException:
            pass
        await _complete_tribunal_after_defense(ch, game, defendant, current_day, alive_role, stand_role, day_vc)
    finally:
        # A reset/new match must not be cleaned up by an obsolete resume task.
        if owns_trial():
            await _cleanup_tribunal(game, guild, current_day)


@bot.command()
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def vote(ctx: commands.Context, target_number: Optional[int] = None) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if target_number is not None:
        return await ctx.send("Use the Tribunal controls to cast nominations.")
    try:
        await bot.gameplay_controller.begin_trial(game, [r.id for r in ctx.author.roles], game.game_channel_id or ctx.channel.id,actor_id=ctx.author.id)
    except gameplay_state.Rejected as error:
        await ctx.send(str(error))


# ==========================================
# UTILITY / PLAYER COMMANDS
# ==========================================

@bot.command()
async def myrole(ctx: commands.Context) -> None:
    if not isinstance(ctx.channel, discord.DMChannel):
        return

    game = get_game_by_player_id(ctx.author.id)
    if not game:
        return await ctx.send("The game hasn't started yet! You don't have a role.")

    role = game.player_roles.get(ctx.author.id)
    embed = discord.Embed(
        title=f"Your Role: {role}",
        description=get_role_description(role),
        color=discord.Color.green()
    )

    if role == "Executioner" and game.role_states.get(ctx.author.id):
        target_id = game.role_states[ctx.author.id].get("exe_target")
        target_user = bot.get_user(target_id)
        if not target_user and target_id:
            guild = bot.get_guild(game.guild_id)
            if guild:
                target_user = guild.get_member(target_id)
                if not target_user:
                    try:
                        target_user = await guild.fetch_member(target_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        target_user = None
        if target_user:
            embed.add_field(
                name="Your Target",
                value=f"Convince Town to lynch **{target_user.display_name}**.",
                inline=False
            )

    await ctx.send(embed=embed)


@bot.command()
async def will(ctx: commands.Context, *, text: Optional[str] = None) -> None:
    """
    DM-only Last Will editor.
      - `!will` shows your current will and provides a button to edit via Modal.
      - `!will clear` clears your will.
    """
    # DM-only to keep wills private. If used in a guild channel, delete and instruct via DM, then return.
    if not isinstance(ctx.channel, discord.DMChannel):
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        try:
            await ctx.author.send("📝 Use `!will` here in DMs to view/edit your will.")
        except discord.HTTPException:
            return
        return

    game = get_game_by_player_id(ctx.author.id)
    if not game or not game.in_progress:
        try:
            await ctx.author.send("No active game found for you.")
        except discord.HTTPException:
            pass
        return

    state = game.role_states.get(ctx.author.id, {})
    if text is not None and text.strip().lower() == "clear":
        def clear():
            gameplay_state.require_current(game)
            if game.resolving:
                raise gameplay_state.Rejected("Night is resolving. Please wait.")
            if ctx.author.id not in {p.id for p in game.living_players}:
                raise gameplay_state.Rejected("Only living players can edit their wills.")
            game.role_states.setdefault(ctx.author.id, {})["will"] = ""
        try:
            await gameplay_state.commit(game, clear)
        except gameplay_state.Rejected as error:
            return await ctx.author.send(str(error))
        except OSError:
            return await ctx.author.send('Your will could not be saved. Please try again.')
        return await ctx.author.send("Cleared your will.")

    current = str(state.get("will", "") or "")
    display = current.strip() or "(empty)"
    await ctx.author.send(
        "**Your Last Will:**\n"
        f"```{display[:1800]}```\n"
        "Use the button below to edit it."
    , view=WillView(game=game, owner_id=ctx.author.id, current_text=current))


@bot.command()
@commands.guild_only()
async def stats(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """
    Show personal winrate stats for this guild.
      - `!stats` shows your stats.
      - `!stats @user` is GM-only.
    """
    is_gm = any(r.id == GAME_OVERSEER_ROLE_ID for r in getattr(ctx.author, "roles", []))
    target = member or ctx.author
    if member is not None and not is_gm:
        return await ctx.send("Only the Game Overseer can view other players' stats.")

    # Prefer SQLite (same source as /leaderboard). Fallback to JSON if DB unavailable.
    rec = None
    db = getattr(bot, "db", None)
    if db:
        try:
            rec = await asyncio.to_thread(db.get_player_stats_summary, guild_id=ctx.guild.id, player_id=target.id)
        except Exception:
            rec = None

    if rec is None:
        data = load_stats(ctx.guild.id) or {}
        rec = (data.get("players") or {}).get(str(target.id)) or {}

    def _safe_int(v: object) -> int:
        try:
            return int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    games = _safe_int(rec.get("games_played", 0))
    wins = _safe_int(rec.get("wins", 0))
    losses = _safe_int(rec.get("losses", 0))
    draws = _safe_int(rec.get("draws", 0))
    wr = (wins / games * 100.0) if games > 0 else 0.0

    def _coerce_int_map(d: object) -> Dict[str, int]:
        if not isinstance(d, dict):
            return {}
        out: Dict[str, int] = {}
        for k, v in d.items():
            out[str(k)] = _safe_int(v)
        return out

    def _top_n(d: Dict[str, int], n: int = 5) -> str:
        if not d:
            return "(none)"
        items = sorted(((str(k), _safe_int(v)) for k, v in d.items()), key=lambda kv: (-kv[1], kv[0].lower()))
        return ", ".join(f"{k}({v})" for k, v in items[:n])

    role_played = _coerce_int_map(rec.get("role_played"))
    role_wins = _coerce_int_map(rec.get("role_wins"))
    faction_played = _coerce_int_map(rec.get("faction_played"))
    faction_wins = _coerce_int_map(rec.get("faction_wins"))

    # Back-compat + nicer display: accept legacy role-name keys, but display friendly labels.
    personal_raw = _coerce_int_map(rec.get("personal_wins"))
    old_to_new = {
        "Pirate": "pirate_win",
        "Executioner": "exe_win",
        "Jester": "jester_win",
        "Survivor": "survivor_survived",
        "Chaos": "chaos_survived",
        "Witch": "witch_town_loses",
        "Arsonist": "arsonist_win",
    }
    personal_norm: Dict[str, int] = {}
    # Keep canonical keys + any unknown keys.
    for k, v in personal_raw.items():
        if k in old_to_new:
            continue
        personal_norm[k] = _safe_int(v)
    # Fold old keys into canonical keys.
    for old_k, new_k in old_to_new.items():
        if personal_raw.get(old_k):
            personal_norm[new_k] = personal_norm.get(new_k, 0) + _safe_int(personal_raw.get(old_k))

    pretty_labels = {
        "pirate_win": "Pirate",
        "exe_win": "Executioner",
        "jester_win": "Jester",
        "survivor_survived": "Survivor",
        "chaos_survived": "Chaos",
        "witch_town_loses": "Witch",
        "arsonist_win": "Arsonist",
    }
    personal: Dict[str, int] = {}
    for k, v in personal_norm.items():
        label = pretty_labels.get(k, k)
        personal[label] = personal.get(label, 0) + _safe_int(v)

    embed = discord.Embed(
        title=f"📊 Stats for {target.display_name}",
        description=f"Games: **{games}**\nWins: **{wins}**  Losses: **{losses}**  Draws: **{draws}**\nWR: **{wr:.1f}%**",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Faction played", value=_top_n(faction_played, 3), inline=False)
    embed.add_field(name="Faction wins", value=_top_n(faction_wins, 3), inline=False)
    embed.add_field(name="Top roles played", value=_top_n(role_played, 5), inline=False)
    embed.add_field(name="Top role wins", value=_top_n(role_wins, 5), inline=False)
    if personal:
        embed.add_field(name="Personal wins", value=_top_n(personal, 10), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="importstats")
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def importstats(ctx: commands.Context, force: Optional[str] = None) -> None:
    """
    One-time importer: migrate existing JSON stats to SQLite.
    This does NOT delete the JSON stats file.
    """
    db = getattr(bot, "db", None)
    if not db:
        return await ctx.send("SQLite DB not initialized; cannot import.")
    if force is not None and force.lower() != 'force':
        return await ctx.send('Use !importstats, or !importstats force to explicitly replace newer totals.')
    data = await run_blocking(load_stats, ctx.guild.id) or {}
    n = 0
    try:
        n = await run_blocking(db.import_player_stats_from_json, guild_id=ctx.guild.id, stats_data=data, reject_stale=force is None)
    except ValueError as error:
        return await ctx.send(str(error))
    except Exception:
        logging.exception("Stats import failed.")
        return await ctx.send("🛑 Import failed — check logs.")
    await ctx.send(f"✅ Imported **{n}** player stat record(s) into SQLite.")

@bot.command()
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def reveal(ctx: commands.Context) -> None:
    game = get_game_by_player_id(ctx.author.id)
    if not game or game.phase != "day" or game.player_roles.get(ctx.author.id) != "Mayor":
        return
    guild = bot.get_guild(game.guild_id)
    if guild:
        await game.sync_living_players(guild)
        living_ids = await game.get_living_ids(guild)
        if ctx.author.id not in living_ids:
            return
    if game.role_states.get(ctx.author.id, {}).get("is_revealed"):
        return await ctx.send("You have already revealed yourself!")

    def reveal_mayor():
        gameplay_state.require_current(game, phase="day")
        if ctx.author.id not in {p.id for p in game.living_players} or game.player_roles.get(ctx.author.id) != "Mayor":
            raise gameplay_state.Rejected("You cannot reveal now.")
        if game.role_states.get(ctx.author.id,{}).get('is_revealed'):
            raise gameplay_state.Rejected('You have already revealed yourself.')
        game.role_states.setdefault(ctx.author.id, {})["is_revealed"] = True
    await gameplay_state.commit(game, reveal_mayor)
    game_chan = bot.get_channel(game.game_channel_id)
    if game_chan:
        await game_chan.send(f"👑 **{ctx.author.mention} has revealed as the Mayor! Their vote now counts as two.**")

@bot.command()
async def haunt(ctx: commands.Context, target_number: Optional[int] = None) -> None:
    if not isinstance(ctx.channel, discord.DMChannel):
        return

    game = get_game_by_player_id(ctx.author.id)
    if not game or not game.role_states.get(ctx.author.id, {}).get("can_haunt"):
        return

    stored_voters: List[int] = game.role_states[ctx.author.id].get("guilty_voters", [])
    game_chan = bot.get_channel(game.game_channel_id)
    if not game_chan:
        return
    guild = game_chan.guild

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)

    # Live eligible list: filter out voters who are now dead/left.
    eligible_voters = [vid for vid in stored_voters if vid in living_ids]
    if not eligible_voters:
        def exhausted():
            gameplay_state.require_current(game)
            if game.resolving:
                raise gameplay_state.Rejected('Night is resolving. Please wait.')
            game.role_states[ctx.author.id]['can_haunt'] = False
        await gameplay_state.commit(game,exhausted)
        return await ctx.send("There are no eligible living voters left to haunt.")

    # Allow `!haunt` with no number to show the up-to-date list.
    if target_number is None:
        lines: List[str] = []
        for i, vid in enumerate(eligible_voters, start=1):
            m = await game.get_member_safe(guild, vid)
            lines.append(f"{i}: {m.display_name if m else str(vid)}")
        return await ctx.send("Eligible voters you can haunt:\n" + "\n".join(lines) + "\nUse `!haunt <number>`.")

    if not (1 <= target_number <= len(eligible_voters)):
        return await ctx.send("Invalid number. Use `!haunt` to see the current eligible list.")

    target_id = eligible_voters[target_number - 1]
    target = await game.get_member_safe(guild, target_id)

    if not target or target.id not in living_ids:
        return await ctx.send("That player is already dead. Use `!haunt` to see the current eligible list.")

    result = await gameplay_actions.submit(game, ctx.author.id, "haunt", (target_id,), guild=guild)
    await ctx.send(result.message, allowed_mentions=NO_MENTIONS)

# ==========================================
# PLAYER NIGHT ACTION COMMANDS
# ==========================================

async def _submit_action_command(ctx, ability, slots=(), *, corpse_number=None, **options):
    if getattr(ctx, "interaction", None) and not ctx.interaction.response.is_done():
        await ctx.defer(ephemeral=True)
    game = ctx.game
    expected = gameplay_state.identity(game)
    guild = ctx.guild or bot.get_guild(game.guild_id)
    await game.sync_living_players(guild)
    living = {game.player_slots.get(p.id): p.id for p in game.living_players}
    if any(slot not in living for slot in slots):
        return await ctx.send("Invalid or no longer living target seat.", ephemeral=True)
    if corpse_number is not None:
        corpses = gameplay_actions.usable_corpses(game, ctx.author.id)
        if not 1 <= corpse_number <= len(corpses):
            return await ctx.send("Invalid corpse number. Use !corpses for the current list.", ephemeral=True)
        options['corpse_id'] = corpses[corpse_number-1]['player_id']
    result = await gameplay_actions.submit(game, ctx.author.id, ability, tuple(living[slot] for slot in slots),
        expected=expected, guild=guild, **options)
    await ctx.send(result.message, ephemeral=True, allowed_mentions=NO_MENTIONS)
    if result.accepted:
        bot.gameplay_controller.after_submission(game, ctx.author.id, result.action)


@bot.command(name="actions")
async def actions_command(ctx):
    game = get_game_by_player_id(ctx.author.id)
    if not game:
        return await ctx.send("No active game found for you.")
    try:
        await bot.gameplay_controller.reopen(game, ctx.author.id)
        if ctx.guild:
            await ctx.send("Your controls were sent privately.", allowed_mentions=NO_MENTIONS)
    except gameplay_state.Rejected as error:
        await ctx.send(str(error))
    except discord.HTTPException:
        await ctx.send("I couldn't deliver private controls. Enable DMs or use /actions.")


@bot.tree.command(name="actions", description="Open private role controls, report history, or an outstanding duel")
async def actions_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    game = get_game_by_player_id(interaction.user.id)
    try:
        if not game or (interaction.guild and interaction.guild.id != game.guild_id):
            raise gameplay_state.Rejected("No active game found for you in this server.")
        await private_reply(interaction, "Your private controls", view=bot.gameplay_controller.panel_for(game, interaction.user.id))
    except gameplay_state.Rejected as error:
        await private_reply(interaction, str(error))


@bot.tree.command(name="trial", description="Game Overseer: start the Tribunal")
@discord.app_commands.guild_only()
async def trial_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if interaction.guild.id != ALLOWED_GUILD_ID:
            raise gameplay_state.Rejected("This bot is configured for another server.")
        game = get_game_for_guild(interaction.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
        await bot.gameplay_controller.begin_trial(game, [r.id for r in interaction.user.roles], game.game_channel_id or interaction.channel_id,actor_id=interaction.user.id)
        await private_reply(interaction, "Nominations are open in the game channel.")
    except gameplay_state.Rejected as error:
        await private_reply(interaction, str(error))


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def kill(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "kill", (target_number,))

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def heal(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "heal", (target_number,))


@heal.autocomplete("target_number")
async def heal_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def roleblock(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "roleblock", (target_number,))


@roleblock.autocomplete("target_number")
async def roleblock_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def investigate(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "investigate", (target_number,))


@investigate.autocomplete("target_number")
async def investigate_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
async def shoot(ctx: commands.Context, target_number: int) -> None:
    game = get_game_by_player_id(ctx.author.id)
    if not game:
        return await ctx.send('No active game found.')
    if game.phase == 'night':
        return await _vig_shoot_night(ctx, target_number)
    expected = gameplay_state.identity(game)
    if getattr(ctx, 'interaction', None) is not None:
        await ctx.defer(ephemeral=True)
    from gameplay.deputy import fire
    from gameplay.controller import channel_is_private
    guild = ctx.guild or bot.get_guild(game.guild_id)
    if guild is None or guild.id != game.guild_id:
        return await ctx.send('The game server is unavailable.')
    if ctx.guild is not None:
        if not guild.chunked:
            await guild.chunk(cache=True)
        if not guild.chunked or not channel_is_private(ctx.channel, guild, ctx.author.id):
            return await ctx.send('Use a verified private channel or DM for role actions.')
    target = next((uid for uid, slot in game.player_slots.items() if slot == target_number), None)
    try:
        message, receipts = await fire(game, ctx.author.id, target, guild=guild, expected=expected)
    except gameplay_state.Rejected as error:
        return await ctx.send(str(error))
    controller = getattr(bot, 'gameplay_controller', None)
    if controller:
        controller.after_deputy_shot(game, ctx.author.id, receipts)
        await ctx.send(message, ephemeral=True, allowed_mentions=NO_MENTIONS)
        return
    channel = guild.get_channel(game.game_channel_id)
    if channel:
        for receipt in receipts:
            await game.deliver_death_receipt(channel, guild, receipt)
    await ctx.send(message)
    await game.check_win_conditions()


@only_during_night_gameplay()
async def _vig_shoot_night(ctx, target_number):
    return await _submit_action_command(ctx, 'shoot', (target_number,))


@shoot.autocomplete("target_number")
async def shoot_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def frame(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "frame", (target_number,))


@frame.autocomplete("target_number")
async def frame_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def hide(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "hide", (target_number,))

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def transport(ctx: commands.Context, target1_num: int, target2_num: int) -> None:
    await _submit_action_command(ctx, "transport", (target1_num, target2_num))


@transport.autocomplete("target1_num")
async def transport_t1_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)


@transport.autocomplete("target2_num")
async def transport_t2_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot_excluding(interaction, current, exclude_param="target1_num")

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def protect(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "protect", (target_number,))


@protect.autocomplete("target_number")
async def protect_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def watch(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "watch", (target_number,))


@watch.autocomplete("target_number")
async def watch_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def track(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "track", (target_number,))


@track.autocomplete("target_number")
async def track_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def control(ctx: commands.Context, target1_num: int, target2_num: int) -> None:
    await _submit_action_command(ctx, "control", (target1_num, target2_num))


@control.autocomplete("target1_num")
async def control_t1_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)


@control.autocomplete("target2_num")
async def control_t2_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot_excluding(interaction, current, exclude_param="target1_num")

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def douse(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "douse", (target_number,))


@douse.autocomplete("target_number")
async def douse_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def corpses(ctx: commands.Context) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Retributionist":
        return
    state = game.role_states.get(ctx.author.id, {})
    if state.get("uses_remaining", 0) <= 0:
        return await ctx.send("You have no uses remaining.")
    used_ids: Set[int] = set()
    for x in (state.get("used_corpses") or []):
        try:
            used_ids.add(int(x))
        except (TypeError, ValueError):
            continue
    usable = []
    for entry in game.graveyard:
        if entry.get("used_by_retri"):
            continue
        pid = entry.get("player_id")
        if pid is None:
            continue
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue
        if pid_int in used_ids:
            continue
        if entry.get("is_hidden"):
            continue
        r = entry.get("real_role")
        if r not in {"Doctor", "Sheriff", "Investigator", "Lookout", "Tracker", "Escort", "Transporter", "Bodyguard", "Vigilante"}:
            continue
        usable.append(entry)
    if not usable:
        return await ctx.send("No usable Town corpses are available yet.")
    lines = []
    for i, e in enumerate(usable, start=1):
        pid = e.get("player_id")
        member = await game.get_member_safe(ctx.bot.get_guild(game.guild_id), pid) if ctx.bot.get_guild(game.guild_id) else None
        name = member.display_name if member else str(pid)
        lines.append(f"{i}: {name} ({e.get('real_role')})")
    await ctx.send("**Usable corpses:**\n" + "\n".join(lines))


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def reanimate(ctx: commands.Context, corpse_number: int, target1_num: int, target2_num: Optional[int] = None) -> None:
    await _submit_action_command(ctx, "reanimate", (target1_num, target2_num) if target2_num is not None else (target1_num,), corpse_number=corpse_number)


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def chaos(ctx: commands.Context, target1_num: int, target2_num: int) -> None:
    await _submit_action_command(ctx, "chaos", (target1_num, target2_num))

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def hypnotize(ctx: commands.Context, target_number: int, message_type: str) -> None:
    await _submit_action_command(ctx, "hypnotize", (target_number,), message_type=message_type)

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def tailor(ctx: commands.Context, target_number: int, *, fake_role: str) -> None:
    await _submit_action_command(ctx, "tailor", (target_number,), fake_role=fake_role)

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def plunder(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "plunder", (target_number,))

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def guard(ctx: commands.Context, target_number: int) -> None:
    await _submit_action_command(ctx, "guard", (target_number,))

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def vest(ctx: commands.Context) -> None:
    await _submit_action_command(ctx, "vest")

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def alert(ctx: commands.Context) -> None:
    await _submit_action_command(ctx, "alert")

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def ignite(ctx: commands.Context) -> None:
    await _submit_action_command(ctx, "ignite")


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def clean(ctx: commands.Context) -> None:
    await _submit_action_command(ctx, "clean")

# ==========================================
# RUN BOT
# ==========================================

@bot.hybrid_command(name='doused')
@only_during_night_gameplay()
async def doused(ctx: commands.Context) -> None:
    game = get_game_by_player_id(ctx.author.id)
    if game.player_roles.get(ctx.author.id) != 'Arsonist':
        return await ctx.send('That ability is not available to you.', ephemeral=True)
    from discord_output import chunk_lines
    targets = [f"#{game.player_slots.get(uid, '?')}: {discord.utils.escape_markdown(p.display_name)}"
               for uid in sorted(game.doused_players, key=lambda uid: game.player_slots.get(uid, uid))
               for p in game.players if p.id == uid]
    for text in chunk_lines(['**Doused players:**'] + (targets or ['None.']), max_chars=1900):
        await ctx.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def stab(ctx: commands.Context, target_number: int) -> None:
    return await _submit_action_command(ctx, 'sk_kill', (target_number,))

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
async def ward(ctx: commands.Context, target_number: int) -> None:
    ctx.game = get_game_by_player_id(ctx.author.id)
    if ctx.game is None:
        return await ctx.send('No active game found.')
    if ctx.guild is not None:
        return await ctx.send('Use /actions or DM me to ward your bound player.')
    return await _submit_action_command(ctx, 'ward', (target_number,))

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def gaze(ctx: commands.Context, target_number: int, second_target: int) -> None:
    return await _submit_action_command(ctx, 'gaze', (target_number, second_target))

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def cautious(ctx: commands.Context) -> None:
    return await _submit_action_command(ctx, 'cautious', ())

async def _reopen_client():
    # Stop and drain jobs before clearing SDK caches. Game objects remain canonical.
    await bot.gameplay_controller.stop_all()
    for game in list(active_games.values()):
        async with game._startup_lock:
            pass
        from gameplay.lifecycle import message_lock
        async with message_lock(game.guild_id):
            def pause():
                if not game._rehydrate_pending:
                    game._persist_player_ids = [p.id for p in game.players]
                    game._persist_living_ids = [p.id for p in game.living_players]
                    game._rehydrate_pending = True
            await gameplay_state.commit(game, pause, persist=False, allow_recovery=True)
    tasks = [getattr(bot, name, None) for name in ('_gateway_watchdog_task', '_mafia_dm_outbox_task')]
    tasks = [task for task in tasks if task and task is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await bot.close()
    bot.clear()
    # This application owns the default connector. discord.py clear() resets
    # the session but retains its closed connector, so static_login must build another.
    bot.http.connector = discord.utils.MISSING
    # close() also clears the loop binding; clear() does not restore it.
    await bot._async_setup_hook()
    bot.gameplay_controller = Controller(bot)
    bot._mafia_dm_outbox_started = False
    bot._mafia_full_reconnect = True
    bot._gateway_restart_requested = False
    _reset_gateway_watchdog_session()


async def _connect_forever() -> None:
    """Supervise full sessions; ordinary gateway reconnects remain SDK-managed."""
    _reset_gateway_watchdog_session()
    token = os.getenv('DISCORD_TOKEN') or os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        raise RuntimeError('DISCORD_TOKEN environment variable not set.')
    minimum = max(.1, float(os.getenv('MAFIABOT_RECONNECT_BACKOFF_MIN_SEC', '5')))
    maximum = max(minimum, float(os.getenv('MAFIABOT_RECONNECT_BACKOFF_MAX_SEC', '300')))
    backoff = minimum
    while True:
        try:
            await bot.start(token, reconnect=True)
            if not getattr(bot, '_gateway_restart_requested', False):
                logging.info('Gateway session ended normally.')
                return
        except (discord.LoginFailure, discord.PrivilegedIntentsRequired):
            logging.critical('Discord rejected the bot configuration; repair credentials/intents.')
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception('Gateway session failed; restarting in %.1fs.', backoff)
        await _reopen_client()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, maximum)


def _require_discord_token() -> None:
    if os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN"):
        return
    raise RuntimeError(
        "DISCORD_TOKEN environment variable not set. Add it to your .env or environment variables before running."
    )



def _release_single_instance_lock():
    global _single_instance_lock_handle
    if _single_instance_lock_handle is not None:
        _single_instance_lock_handle.close()
        _single_instance_lock_handle = None


async def _run_session():
    try:
        await _connect_forever()
    finally:
        await bot.gameplay_controller.stop_all()
        tasks = [getattr(bot, name, None) for name in ('_gateway_watchdog_task', '_mafia_dm_outbox_task')]
        tasks = [task for task in tasks if task and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if not bot.is_closed():
            await bot.close()


def main():
    _require_discord_token()
    validate_live_settings()
    _acquire_single_instance_lock()
    try:
        asyncio.run(_run_session())
    except KeyboardInterrupt:
        logging.info("Interrupted.")
    finally:
        _release_single_instance_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
