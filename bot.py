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
from persistence import load_stats
from checks import only_during_night_gameplay as only_during_night_gameplay_factory, enforce_allowed_guild
from errors import on_app_command_tree_error, on_command_error as on_command_error_handler
from game import Game, active_games, bind_bot, get_game_by_player_id, get_game_for_guild
from engine.night import run_night_pipeline
from database import Database
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


def _acquire_single_instance_lock() -> None:
    """
    Best-effort local single-instance guard.

    Prevents accidentally running both source + dist_runtime copies at once on the same machine.
    Opt out with MAFIABOT_ALLOW_MULTI=1.
    """
    if os.environ.get("MAFIABOT_ALLOW_MULTI", "").strip().lower() in ("1", "true", "yes", "on"):
        _dbg("H7", "bot.py:single_instance", "single-instance lock bypassed", {})
        return

    # Use a machine-wide lock location so "source" and "dist_runtime" builds collide.
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(Path.home())
    lock_path = Path(base) / "Mafiabot" / "bot.instance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # Check for stale lock.
            existing_txt = ""
            existing_pid: Optional[int] = None
            try:
                existing_txt = lock_path.read_text(encoding="utf-8")[:2000]
                existing = json.loads(existing_txt) if existing_txt else {}
                if isinstance(existing, dict) and "pid" in existing:
                    existing_pid = int(existing["pid"])
            except Exception:
                existing_pid = None

            alive = False
            if existing_pid:
                try:
                    os.kill(existing_pid, 0)
                    alive = True
                except Exception:
                    alive = False

            if alive:
                _dbg(
                    "H7",
                    "bot.py:single_instance",
                    "lock exists; other instance appears alive",
                    {"lock_path": str(lock_path), "existing_pid": existing_pid, "existing": existing_txt[:500]},
                )
                raise RuntimeError(
                    f"Another Mafia Bot instance appears to be running (pid={existing_pid}). "
                    f"Lock file exists: {lock_path}"
                )

            # Stale lock: remove and retry.
            try:
                lock_path.unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                # Python < 3.8 compat (not expected here, but safe).
                try:
                    if lock_path.exists():
                        lock_path.unlink()
                except Exception:
                    pass
            _dbg("H7", "bot.py:single_instance", "stale lock removed; retrying", {"lock_path": str(lock_path), "existing_pid": existing_pid})
            continue
        except OSError as e:
            # If we can't enforce the guard, log and continue.
            _dbg("H7", "bot.py:single_instance", "lock create failed; continuing", {"err": repr(e), "errno": getattr(e, "errno", None), "lock_path": str(lock_path)})
            return

    try:
        info = {
            "pid": int(os.getpid()),
            "inst": _debug_instance,
            "cwd": os.getcwd(),
            "argv": list(getattr(sys, "argv", [])),
            "ts_ms": int(time.time() * 1000),
        }
        os.write(fd, json.dumps(info, sort_keys=True).encode("utf-8"))
        _dbg("H7", "bot.py:single_instance", "lock acquired", {"lock_path": str(lock_path), "info": info})
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


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


async def _dm_outbox_pump_loop() -> None:
    """Drain SQLite-backed DM queue (B6.1)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            db = getattr(bot, "db", None)
            if db:
                db.requeue_stale_dm_outbox_sending(stale_after_seconds=300)
                rows = db.claim_dm_outbox_batch(limit=25)
                for row in rows:
                    mid = int(row["id"])
                    uid = int(row["target_user_id"])
                    content = str(row["content"])
                    try:
                        user = bot.get_user(uid) or await bot.fetch_user(uid)
                        await user.send(content)
                        db.mark_dm_outbox_sent(mid)
                    except discord.HTTPException as e:
                        delay = 120
                        if e.status == 429:
                            delay = min(600, int(getattr(e, "retry_after", 60) or 60) + 5)
                        db.retry_dm_outbox_later(mid, error=str(e), delay_seconds=delay)
                    except Exception as e:
                        db.retry_dm_outbox_later(mid, error=str(e), delay_seconds=90)
        except Exception:
            logging.exception("dm_outbox pump iteration failed")
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

    def __init__(self, *, current_text: str) -> None:
        super().__init__()
        self.will.default = (current_text or "")[:1800]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        game = get_game_by_player_id(interaction.user.id)
        if not game or not game.in_progress:
            return await interaction.response.send_message("No active game found for you.", ephemeral=True)
        state = game.role_states.setdefault(interaction.user.id, {})
        state["will"] = str(self.will.value or "")[:1800]
        await game.persist_flush()
        await interaction.response.send_message("Saved your will.", ephemeral=True)


class WillView(discord.ui.View):
    def __init__(self, *, owner_id: int, current_text: str) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.current_text = current_text

    @discord.ui.button(label="Edit Will", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This isn't your will editor.", ephemeral=True)
        # Pull latest will text at click time (avoid overwriting with a stale prefill).
        game = get_game_by_player_id(interaction.user.id)
        latest = ""
        if game and game.in_progress:
            latest = str(game.role_states.get(interaction.user.id, {}).get("will", "") or "")
        await interaction.response.send_modal(WillModal(current_text=latest))


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
            synced = await bot.tree.sync(guild=discord.Object(id=ALLOWED_GUILD_ID))
            _dbg("H2", "bot.py:on_ready:guild_sync", "guild sync ok", {"count": len(synced)})
        except Exception:
            _dbg("H2", "bot.py:on_ready:guild_sync", "guild sync failed", {"allowed_guild_id": int(ALLOWED_GUILD_ID)})
            logging.exception("Failed to sync app commands for allowed guild.")
        try:
            synced = await bot.tree.sync()
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
            db_path = str((Path(__file__).resolve().parent / "state" / "mafiabot.db"))
            bot.db = Database(db_path)  # type: ignore[attr-defined]
            bot.db.initialize()  # type: ignore[attr-defined]
        except Exception:
            logging.exception("Failed to initialize SQLite DB (leaderboards disabled).")

    bot._gateway_had_ready = True  # type: ignore[attr-defined]
    bot._gateway_disconnect_at = None  # type: ignore[attr-defined]
    _ensure_gateway_watchdog_task()

    if not getattr(bot, "_mafia_dm_outbox_started", False):
        bot._mafia_dm_outbox_started = True  # type: ignore[attr-defined]
        bot.loop.create_task(_dm_outbox_pump_loop())

    # Attempt to restore persisted game state for the allowed guild.
    guild = bot.get_guild(ALLOWED_GUILD_ID)
    if guild:
        data = load_state(ALLOWED_GUILD_ID)
        _dbg(
            "H5",
            "bot.py:on_ready:restore:pre",
            "restore check",
            {"allowed_guild_present": True, "has_persisted_state": bool(data)},
        )
        if data:
            try:
                game = Game.from_persisted(data)
                await game.rehydrate_members(guild)
                active_games[ALLOWED_GUILD_ID] = game
                logging.info(f"Restored persisted game state for guild {ALLOWED_GUILD_ID}.")


                # Best-effort repair: ensure Playing/Lockdown roles are applied consistently after restart.
                playing_role = guild.get_role(PLAYING_ROLE_ID)
                lockdown_role = guild.get_role(game.lockdown_role_id) if getattr(game, "lockdown_role_id", None) else None
                if game.in_progress and playing_role:
                    for p in list(game.players):
                        try:
                            if playing_role not in p.roles:
                                await p.add_roles(playing_role)
                        except discord.HTTPException as e:
                            logging.warning(
                                "Playing role repair failed guild_id=%s member_id=%s role_id=%s: %s",
                                guild.id,
                                getattr(p, "id", None),
                                getattr(playing_role, "id", None),
                                e,
                            )
                        await asyncio.sleep(0.05)
                if game.in_progress and lockdown_role:
                    for p in list(game.players):
                        try:
                            if any(r.id == GAME_OVERSEER_ROLE_ID for r in p.roles) or p.guild_permissions.administrator:
                                continue
                            if lockdown_role not in p.roles:
                                await p.add_roles(lockdown_role)
                        except discord.HTTPException as e:
                            logging.warning(
                                "Lockdown role repair failed guild_id=%s member_id=%s role_id=%s: %s",
                                guild.id,
                                getattr(p, "id", None),
                                getattr(lockdown_role, "id", None),
                                e,
                            )
                        await asyncio.sleep(0.05)

                resume_defense = False
                t_deadline = getattr(game, "tribunal_defense_deadline_utc", None)
                t_sub = getattr(game, "tribunal_subphase", None)
                if (
                    game.in_progress
                    and game.phase == "day"
                    and game.vote_in_progress
                    and t_sub == "defense"
                    and t_deadline
                    and getattr(game, "tribunal_defendant_id", None)
                ):
                    dtp = _parse_iso_utc(t_deadline)
                    if dtp:
                        rem_sec = (dtp - datetime.now(timezone.utc)).total_seconds()
                        if TRIBUNAL_RESUME_MIN_SECONDS <= rem_sec <= 7200:
                            resume_defense = True
                            bot.loop.create_task(_resume_tribunal_defense_after_restart(guild, rem_sec))

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

                    game.tribunal_muted = False
                    game.tribunal_defendant_id = None
                    game.tribunal_defense_deadline_utc = None
                    game.tribunal_judgment_deadline_utc = None
                    game.tribunal_judgment_message_id = None
                    game.tribunal_subphase = None
                    game.tribunal_verdict_committed = False
                    game.vote_in_progress = False
                    try:
                        await game.persist_flush()
                    except Exception:
                        pass
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
                logging.exception("Failed to restore persisted state; starting fresh.")
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
    return await enforce_allowed_guild(ctx, allowed_guild_id=ALLOWED_GUILD_ID)


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
    _dbg(
        "H6",
        "bot.py:join_game_command:entry",
        "!join invoked",
        {
            "pid": int(os.getpid()),
            "guild_id": int(ctx.guild.id) if ctx.guild else None,
            "channel_id": int(ctx.channel.id) if getattr(ctx, "channel", None) else None,
            "author_id": int(ctx.author.id),
            "message_id": int(getattr(getattr(ctx, "message", None), "id", 0) or 0),
        },
    )
    if get_game_by_player_id(ctx.author.id):
        return await ctx.send(
            "🛑 You are already in an active game in a server! Please finish that game before joining a new one." + _tag()
        )

    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if game.in_progress:
        return await ctx.send("A game is already in progress!" + _tag())
    if ctx.author.id in [p.id for p in game.players]:
        return await ctx.send("You are already on the waiting list!" + _tag())

    game.players.append(ctx.author)
    # Persist lobby so restarts don't lose the waiting list.
    await game.persist_flush()
    await ctx.send(f"{ctx.author.mention} joined! Total players: {len(game.players)}." + _tag())


@bot.command(name="leave")
@commands.guild_only()
async def leave_game_command(ctx: commands.Context) -> None:
    """
    Leave the join queue (lobby) if a game hasn't started yet.
    """
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if game.in_progress:
        return await ctx.send("The game has already started; leaving the queue is not available." + _tag())

    if ctx.author.id not in [p.id for p in game.players]:
        return await ctx.send("You are not in the join queue. Use `!join` to enter." + _tag())

    game.players = [p for p in game.players if p.id != ctx.author.id]
    # Keep lobby persisted so restarts don't resurrect removed players.
    await game.persist_flush()
    await ctx.send(f"{ctx.author.mention} left the queue. Total players: {len(game.players)}." + _tag())


@bot.command(name='players')
@commands.guild_only()
async def show_players_command(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.players:
        return await ctx.send("No one has joined yet. Use `!join` to enter.")

    player_mentions = [p.mention for p in game.players]
    living_mentions = [p.mention for p in game.living_players]

    response = f"**Waiting Players ({len(game.players)}):**\n" + ", ".join(player_mentions)
    if game.living_players:
        response += f"\n\n**Living Players ({len(game.living_players)}):**\n" + ", ".join(living_mentions)
    await ctx.send(response)


@bot.command(name='startgame')
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def startgame(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if game.in_progress:
        return await ctx.send("A game is already in progress!")

    valid_players = []
    for p in game.players:
        member = await game.get_member_safe(ctx.guild, p.id)
        if member:
            valid_players.append(member)

    if len(valid_players) != len(game.players):
        await ctx.send(f"⚠️ Removed {len(game.players) - len(valid_players)} player(s) who left before the game started.")

    game.players = valid_players
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
                game.game_channel_id = day_tc.id
                if day_tc.id != ctx.channel.id:
                    await ctx.send(f"✅ Game channels ready. Use {day_tc.mention} for day chat and announcements.")
            else:
                game.game_channel_id = ctx.channel.id
        except discord.HTTPException:
            game.game_channel_id = ctx.channel.id
    else:
        game.game_channel_id = ctx.channel.id

    if player_count <= 6:
        town_weights = [("Doctor", 8), ("Sheriff", 8), ("Investigator", 6), ("Lookout", 7), ("Tracker", 7), ("Escort", 7), ("Vigilante", 6), ("Retributionist", 3)]
        mafia_support_weights = [("Gravedigger", 8), ("Consort", 7), ("Framer", 6)]
        neutral_pool = ["Jester", "Executioner", "Survivor"]
    else:
        town_weights = [("Doctor", 8), ("Sheriff", 8), ("Investigator", 6), ("Lookout", 7), ("Tracker", 7), ("Escort", 7), ("Bodyguard", 6), ("Vigilante", 6), ("Scary Grandma", 5), ("Transporter", 4), ("Mayor", 3), ("Retributionist", 3)]
        mafia_support_weights = [("Gatekeeper", 8), ("Consort", 8), ("Framer", 7), ("Gravedigger", 6), ("Hypnotist", 5), ("Mole", 5), ("Tailor", 4)]
        neutral_pool = ["Jester", "Executioner", "Survivor", "Witch", "Pirate", "Arsonist", "Chaos"]

        # Make Witch slightly rarer than other neutrals (small nudge, not a hard ban).
        # This preserves the overall neutral mix while trimming Witch frequency.
        if "Witch" in neutral_pool and random.random() < 0.50:
            neutral_pool.remove("Witch")

    num_mafia, num_neutral = ((1, 1) if player_count <= 6 else (2, 1) if player_count <= 9 else (3, 2) if player_count <= 12 else (4, 2))
    num_town = player_count - num_mafia - num_neutral

    if (player_count - num_mafia) <= 1 and "Executioner" in neutral_pool:
        neutral_pool.remove("Executioner")

    def get_weighted_roles(pool, count):
        selected, names, weights = [], [r for r, _ in pool], [w for _, w in pool]
        for _ in range(count):
            if not names:
                break
            chosen = random.choices(names, weights=weights, k=1)[0]
            selected.append(chosen)
            idx = names.index(chosen)
            names.pop(idx)
            weights.pop(idx)
        return selected

    random.shuffle(neutral_pool)
    chosen_neutrals, killing_count, disruptive_count = [], 0, 0
    for r in neutral_pool:
        if len(chosen_neutrals) == num_neutral:
            break
        if r in ["Arsonist", "Pirate"]:
            if killing_count < 1:
                killing_count += 1
                chosen_neutrals.append(r)
        elif r in ["Witch", "Executioner"]:
            if disruptive_count < 1:
                disruptive_count += 1
                chosen_neutrals.append(r)
        else:
            chosen_neutrals.append(r)

    roles_for_this_game = chosen_neutrals + get_weighted_roles(town_weights, num_town) + (["Mobster"] + get_weighted_roles(mafia_support_weights, num_mafia - 1) if num_mafia > 0 else [])

    if len(roles_for_this_game) != player_count:
        return await ctx.send(f"⚠️ Role list mismatch ({len(roles_for_this_game)} roles for {player_count} players). Game not started.")

    # Hard guardrail: this ruleset assumes no duplicate roles.
    dupes = sorted({r for r in roles_for_this_game if roles_for_this_game.count(r) > 1})
    if dupes:
        return await ctx.send(f"🛑 Duplicate roles generated (not allowed): {', '.join(dupes)}. Game not started.")

    random.shuffle(game.players)
    game.living_players = game.players.copy()
    # Stable targeting numbers: these should NOT shift when the living list order changes.
    game.player_slots = {p.id: i + 1 for i, p in enumerate(game.players)}
    random.shuffle(roles_for_this_game)

    game.player_roles = {p.id: r for p, r in zip(game.players, roles_for_this_game)}
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

    alive_role = ctx.guild.get_role(game.alive_role_id) if game.alive_role_id else None
    playing_role = ctx.guild.get_role(PLAYING_ROLE_ID)
    lockdown_role = ctx.guild.get_role(game.lockdown_role_id) if getattr(game, "lockdown_role_id", None) else None

    for p_id, role in game.player_roles.items():
        player = await game.get_member_safe(ctx.guild, p_id)
        if not player:
            continue

        state: Dict = {}
        if role == "Vigilante":    state = {"shots_remaining": 1, "will_die_of_guilt": False, "guilty_tomorrow": False}
        elif role == "Gravedigger": state = {"uses_remaining": 1}
        elif role == "Survivor":   state = {"vests_remaining": 2}
        elif role == "Mayor":      state = {"is_revealed": False}
        elif role == "Doctor":     state = {"self_heals_remaining": 1}
        elif role == "Bodyguard":  state = {"uses_remaining": 1, "self_protects_remaining": 1}
        elif role == "Witch":      state = {"has_learned_role": False, "night1_shield_used": False}
        elif role == "Gatekeeper": state = {"uses_remaining": 2}
        elif role == "Scary Grandma": state = {"alerts_remaining": 2}
        elif role == "Mole":       state = {"uses_remaining": 1}
        elif role == "Tailor":     state = {"uses_remaining": 1}
        elif role == "Pirate":     state = {"wins": 0}
        elif role == "Retributionist": state = {"uses_remaining": 2, "used_corpses": []}
        elif role == "Chaos":      state = {"uses_remaining": 2}
        elif role == "Executioner":
            # ToS-like: target starts as a Town role; exclude Mayor (and self).
            targets = [
                p.id for p in game.players
                if p.id != p_id and game.player_roles.get(p.id) in TOWN_ROLES and game.player_roles.get(p.id) != "Mayor"
            ]
            if targets:
                state = {"exe_target": random.choice(targets)}

        if state:
            game.role_states[p_id] = state

        # Snapshot role_start for honest history/role leaderboards (survives promotions/conversions).
        game.role_states.setdefault(p_id, {})["role_start"] = role

        if alive_role:
            try:
                await player.add_roles(alive_role)
            except discord.HTTPException:
                pass
        # Everyone in the game gets Playing (including GM/admin).
        if playing_role:
            try:
                await player.add_roles(playing_role)
            except discord.HTTPException:
                pass

        # Only non-staff get the lockdown role (so staff can still play without losing access).
        if lockdown_role:
            if any(r.id == GAME_OVERSEER_ROLE_ID for r in player.roles) or player.guild_permissions.administrator:
                pass
            else:
                try:
                    await player.add_roles(lockdown_role)
                except discord.HTTPException:
                    pass

    # Persist after roles/role-states/Playing/Alive have been applied (restart-safe) before durable DM enqueue.
    await game.persist_flush()

    db = getattr(bot, "db", None)
    for p_id, role in game.player_roles.items():
        player = await game.get_member_safe(ctx.guild, p_id)
        if not player:
            continue
        state = game.role_states.get(p_id, {}) or {}
        if db:
            db.enqueue_dm_outbox(
                guild_id=ctx.guild.id,
                kind="role_deal",
                dedupe_key=f"mafia_role_deal:{ctx.guild.id}:{game.game_key}:{p_id}",
                target_user_id=p_id,
                content=(
                    f"--- GAME STARTED ---\nYour role is: **{role}**\n"
                    f"Use `!myrole` at any time to see your role's description and abilities."
                ),
            )
            if role == "Executioner" and state.get("exe_target"):
                target_user = ctx.guild.get_member(state["exe_target"])
                if target_user:
                    db.enqueue_dm_outbox(
                        guild_id=ctx.guild.id,
                        kind="exe_target",
                        dedupe_key=f"mafia_exe_target:{ctx.guild.id}:{game.game_key}:{p_id}",
                        target_user_id=p_id,
                        content=(
                            f"Your target is **{target_user.display_name}**. "
                            f"You must convince the Town to lynch them to win."
                        ),
                    )
        else:
            try:
                await player.send(
                    f"--- GAME STARTED ---\nYour role is: **{role}**\n"
                    f"Use `!myrole` at any time to see your role's description and abilities."
                )
                if role == "Executioner" and state.get("exe_target"):
                    target_user = ctx.guild.get_member(state["exe_target"])
                    if target_user:
                        await player.send(
                            f"Your target is **{target_user.display_name}**. You must convince the Town to lynch them to win."
                        )
            except discord.HTTPException:
                await ctx.send(f"⚠️ Could not DM {player.mention} — they may have DMs disabled.")

    await ctx.send(f"**Game Started!** Roles have been assigned secretly. It is now **Day 1**.")
    logging.info(f"Game started on guild {ctx.guild.id} with {player_count} players.")

    mafia_tc = ctx.guild.get_channel(game.mafia_tc_id)
    if mafia_tc:
        for p in game.players:
            if game.player_roles.get(p.id) in ALL_MAFIA_ROLES:
                try:
                    await mafia_tc.set_permissions(p, view_channel=True, send_messages=True)
                except discord.HTTPException:
                    pass
        await mafia_tc.send("Welcome, Mafiosi. This is your private channel.")


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
    if not game.in_progress or game.phase != "night":
        return await ctx.send("This can only be used during the night phase.")

    # Prevent resolving while Pirate duels are still running.
    pending_duels = [
        a for a in game.night_actions.values()
        if a.get("type") == "plunder" and not a.get("duel_finished", False)
    ]
    if pending_duels:
        return await ctx.send("⚔️ A Pirate duel is still in progress. Please wait for it to finish before resolving the night.")

    if game.resolving:
        return await ctx.send("Resolution is already in progress.")
    game.resolving = True
    try:
        await ctx.send("The sun begins to rise...")
        if not game.in_progress:
            return

        # Expand special actions (Retributionist) into normal engine actions.
        # Chaos is resolved inside engine/night.py as the single source of truth.
        for actor_id, action in list(game.night_actions.items()):
            a_type = action.get("type")
            if a_type == "reanimate":
                corpse_role = action.get("corpse_role")
                corpse_pid = action.get("corpse_player_id")
                if not corpse_role or corpse_pid is None:
                    continue
                # Map corpse role to an engine action.
                if corpse_role == "Doctor":
                    game.night_actions[actor_id] = {"type": "heal", "target": action.get("target"), "actor": actor_id, "_from_retri": corpse_pid}
                elif corpse_role in {"Sheriff", "Investigator"}:
                    game.night_actions[actor_id] = {"type": "investigate", "target": action.get("target"), "role": corpse_role, "actor": actor_id, "_from_retri": corpse_pid}
                elif corpse_role == "Lookout":
                    game.night_actions[actor_id] = {"type": "watch", "target": action.get("target"), "actor": actor_id, "_from_retri": corpse_pid}
                elif corpse_role == "Tracker":
                    game.night_actions[actor_id] = {"type": "track", "target": action.get("target"), "actor": actor_id, "_from_retri": corpse_pid}
                elif corpse_role == "Escort":
                    game.night_actions[actor_id] = {"type": "roleblock", "target": action.get("target"), "actor": actor_id, "_from_retri": corpse_pid}
                elif corpse_role == "Transporter":
                    game.night_actions[actor_id] = {"type": "transport", "targets": action.get("targets", []), "actor": actor_id, "_from_retri": corpse_pid}
                elif corpse_role == "Vigilante":
                    game.night_actions[actor_id] = {"type": "shoot", "target": action.get("target"), "actor": actor_id, "_from_retri": corpse_pid}
                elif corpse_role == "Bodyguard":
                    game.night_actions[actor_id] = {"type": "ret_protect", "target": action.get("target"), "actor": actor_id, "_from_retri": corpse_pid}

        visit_log, blocked, healed_by_map, protected_by_map, deaths = await run_night_pipeline(game, ctx.guild)

        # Consume Chaos / Retributionist uses only if not blocked AND the expanded action actually applied.
        # (Important for revealed-Mayor heal rule: skipped heals should not burn uses/corpses.)
        for actor_id, action in list(game.night_actions.items()):
            if actor_id in blocked:
                continue

            a_type = action.get("type")

            def _heal_applied() -> bool:
                tgt = action.get("target")
                if tgt is None:
                    return False
                return healed_by_map.get(tgt) == actor_id

            applied = True
            if a_type == "heal":
                applied = _heal_applied()
            elif a_type == "roleblock":
                # Consider the roleblock "applied" only if the target actually ended up blocked.
                tgt = action.get("target")
                applied = (tgt is not None) and (tgt in blocked)

            if not applied:
                continue

            corpse_pid = action.get("_from_retri")
            if corpse_pid is not None and game.player_roles.get(actor_id) == "Retributionist":
                s = game.role_states.setdefault(actor_id, {})
                s["uses_remaining"] = max(0, int(s.get("uses_remaining", 0)) - 1)
                used = s.setdefault("used_corpses", [])
                if corpse_pid not in used:
                    used.append(corpse_pid)
                for entry in game.graveyard:
                    if entry.get("player_id") == corpse_pid:
                        entry["used_by_retri"] = True
                        break

        # deaths from run_night_pipeline() already includes night kills.
        deaths = set(deaths)
        night_kill_deaths = set(deaths)

        # Night feedback already sent inside run_night_pipeline().

        await game.sync_living_players(ctx.guild)
        living_ids = await game.get_living_ids(ctx.guild)

        # Vigilante guilt timing: convert pending guilt markers BEFORE tallying guilt deaths for this resolve.
        for _p_id, s in list(game.role_states.items()):
            if s.get("guilty_tomorrow"):
                s["will_die_of_guilt"] = True
                s["guilty_tomorrow"] = False

        guilty_vigs = [
            p_id
            for p_id, s in game.role_states.items()
            if s.get("will_die_of_guilt") and p_id in living_ids and p_id not in night_kill_deaths
        ]
        deaths.update(guilty_vigs)

        # Jester haunt fallback (ToS-like): if a lynched Jester didn't pick, haunt a random eligible voter.
        for _j_id, s in list(game.role_states.items()):
            if not s.get("can_haunt"):
                continue
            if "haunt_target" in s:
                continue
            eligible = [vid for vid in s.get("guilty_voters", []) if vid in living_ids]
            if eligible:
                s["haunt_target"] = random.choice(eligible)
                s["can_haunt"] = False

        jester_haunts = [s["haunt_target"] for s in game.role_states.values() if "haunt_target" in s]
        deaths.update(jester_haunts)

        for _p_id, s in list(game.role_states.items()):
            s.pop("haunt_target", None)

        if not deaths:
            await ctx.send("The night was surprisingly peaceful. No one has died.")
        else:
            for p_id in set(deaths):
                player = await game.get_member_safe(ctx.guild, p_id)
                if p_id in jester_haunts:
                    cause = "haunt"
                    custom = f"👻 The Jester's spirit has claimed its revenge! **<@{p_id}>** was found dead."
                elif p_id in guilty_vigs:
                    cause = "guilt"
                    custom = f"Overcome with guilt, <@{p_id}> took their own life."
                else:
                    cause = "night_kill"
                    custom = None

                if player:
                    await game.process_death(ctx, player, cause, custom_message=custom)
                else:
                    await game.process_death_by_id(ctx, ctx.guild, p_id, cause, custom_message=custom)

        if await game.check_win_conditions():
            return
        await game.start_day(ctx)
    finally:
        game.resolving = False
        # Don't resurrect persisted state after `reset()` has deleted it.
        if game.in_progress and not getattr(game, "ending", False):
            await game.persist_flush()


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

    embed = discord.Embed(title=f"🌙 Night {game.day_number} Status", color=discord.Color.blue())
    embed.add_field(name="✅ Acted",       value="\n".join(acted)       or "None yet.", inline=False)
    embed.add_field(name="⏳ Waiting For", value="\n".join(waiting_for) or "All in!",   inline=False)

    try:
        await ctx.author.send(embed=embed)
        await ctx.message.delete()
        await ctx.send("Night status sent to your DMs.", delete_after=5)
    except discord.HTTPException:
        await ctx.send("I can't DM you! Check your privacy settings.", delete_after=10)


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
    judgment_msg = await channel.send(embed=j_embed)
    try:
        await judgment_msg.add_reaction("✅")
        await judgment_msg.add_reaction("❌")
    except (discord.Forbidden, discord.HTTPException):
        pass

    game.tribunal_judgment_message_id = judgment_msg.id
    await game.persist_flush()

    await asyncio.sleep(30)

    if not game.is_active("day") or not game.vote_in_progress or game.day_number != current_day:
        return

    try:
        judgment_msg = await channel.fetch_message(judgment_msg.id)
    except discord.NotFound:
        await channel.send("The judgment message was deleted — cancelling the trial.")
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

    for reaction in judgment_msg.reactions:
        if str(reaction.emoji) not in {"✅", "❌"}:
            continue
        async for user in reaction.users():
            if user.id != bot.user.id and user.id in living_ids and user.id != defendant.id:
                user_reacts.setdefault(user.id, set()).add(str(reaction.emoji))

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


async def _resume_tribunal_defense_after_restart(guild: discord.Guild, remaining: float) -> None:
    """Sleep remaining defense time then continue tribunal (B4 resume path)."""
    await asyncio.sleep(max(0.0, remaining))
    game = active_games.get(ALLOWED_GUILD_ID)
    if not game or not game.vote_in_progress:
        return
    if getattr(game, "tribunal_subphase", None) != "defense":
        return
    ch = bot.get_channel(game.game_channel_id)
    if not isinstance(ch, discord.TextChannel):
        return
    did = getattr(game, "tribunal_defendant_id", None)
    if not did:
        return
    defendant = await game.get_member_safe(guild, int(did))
    if not defendant:
        return
    alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None
    stand_role = guild.get_role(game.stand_role_id) if game.stand_role_id else None
    day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    current_day = game.day_number
    try:
        await ch.send("⚖️ **Trial resumed** after bot restart — continuing where the defense phase left off.")
    except discord.HTTPException:
        pass
    await _complete_tribunal_after_defense(ch, game, defendant, current_day, alive_role, stand_role, day_vc)


@bot.command()
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def vote(ctx: commands.Context, target_number: Optional[int] = None) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress or game.phase != "day":
        return

    is_gm = any(r.id == GAME_OVERSEER_ROLE_ID for r in ctx.author.roles)
    if target_number:
        # Non-GMs get a simple hint; don't do any lookups.
        if not is_gm:
            return await ctx.send("*(Use the Game Overseer's `!vote` command to run the Tribunal.)*")
        target = await game.get_target_from_input(ctx, target_number)
        if target:
            await ctx.send("*(Use the Tribunal UI to cast votes.)*")
        return

    if not is_gm:
        return await ctx.send("Only the Game Overseer can initiate the Tribunal!")

    if game.votes_today >= VOTE_LIMIT_PER_DAY:
        return await ctx.send("🛑 **The town is exhausted.** No more trials today.")
    if game.vote_in_progress:
        return await ctx.send("A vote is already underway!")

    alive_role = ctx.guild.get_role(game.alive_role_id) if game.alive_role_id else None
    stand_role = ctx.guild.get_role(game.stand_role_id) if game.stand_role_id else None
    day_vc = ctx.guild.get_channel(game.day_vc_id) if game.day_vc_id else None

    current_day = game.day_number

    try:
        game.vote_in_progress = True

        await game.sync_living_players(ctx.guild)
        living_ids = await game.get_living_ids(ctx.guild)
        emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟","🇦","🇧","🇨","🇩","🇪"]
        ordered_living = game.ordered_living_players()
        nominees = {emojis[i]: p for i, p in enumerate(ordered_living) if i < len(emojis)}

        embed = discord.Embed(
            title="⚖️ NOMINATION PHASE ⚖️",
            description=f"Vote to put someone on trial. ({VOTE_DURATION} seconds)",
            color=discord.Color.dark_gold()
        )
        embed.add_field(
            name="Living Players",
            value="\n".join(
                [
                    f"{e} — #{game.player_slots.get(p.id, '?')} {p.mention} ({p.display_name})"
                    for e, p in nominees.items()
                ]
            ),
            inline=False
        )

        poll = await ctx.send(embed=embed)
        try:
            for e in nominees:
                await poll.add_reaction(e)
        except (discord.Forbidden, discord.HTTPException):
            return await ctx.send("🛑 I couldn't add reactions here. Check my permissions (Add Reactions) and try again.")

        await asyncio.sleep(VOTE_DURATION)

        if not game.is_active("day") or not game.vote_in_progress or game.day_number != current_day:
            return

        try:
            poll = await ctx.channel.fetch_message(poll.id)
        except discord.NotFound:
            return

        # Refresh living list before tally (admins may slay, users may leave).
        await game.sync_living_players(ctx.guild)
        living_ids = await game.get_living_ids(ctx.guild)

        ordered_living2 = game.ordered_living_players()
        votes = {p: 0 for p in ordered_living2}
        user_nomination_votes: Dict[int, Optional[discord.Member]] = {}
        user_nomination_multi: Set[int] = set()
        voters_map = {p: [] for p in ordered_living2}

        for reaction in poll.reactions:
            if reaction.emoji not in nominees:
                continue
            target = nominees[reaction.emoji]
            async for user in reaction.users():
                if user.id != bot.user.id and user.id in living_ids and user.id != target.id:
                    if user.id in user_nomination_multi:
                        continue
                    if user.id not in user_nomination_votes:
                        user_nomination_votes[user.id] = target
                    else:
                        # Reacted to multiple nominees -> invalid/abstain for nomination.
                        user_nomination_multi.add(user.id)
                        user_nomination_votes[user.id] = None

        for uid, target in user_nomination_votes.items():
            if target and target.id in living_ids and target in votes:
                weight = 2 if game.role_states.get(uid, {}).get("is_revealed") else 1
                votes[target] += weight
                m = await game.get_member_safe(ctx.guild, uid)
                if m:
                    voters_map[target].append(m)

        max_votes = max(votes.values()) if votes else 0
        if max_votes == 0:
            return await ctx.send("The town remains silent. No one is put on trial.")

        top = [p for p, v in votes.items() if v == max_votes]
        if len(top) > 1:
            return await ctx.send("The nomination resulted in a tie! No one takes the stand.")

        defendant = top[0]
        game.votes_today += 1
        await game.persist_flush()

        await ctx.send(f"🚨 **{defendant.mention} has been voted to the stand!** 🚨\nThe town is now muted. You have **45 seconds** to defend yourself.")
        game.tribunal_defendant_id = defendant.id
        if day_vc and alive_role:
            try:
                await day_vc.set_permissions(alive_role, speak=False)
            except discord.HTTPException:
                pass
            game.tribunal_muted = True
            await game.persist_flush()

        if stand_role:
            try:
                await defendant.add_roles(stand_role)
            except discord.HTTPException:
                pass

        defense_end = (datetime.now(timezone.utc) + timedelta(seconds=45)).replace(microsecond=0)
        game.tribunal_defense_deadline_utc = defense_end.isoformat()
        game.tribunal_subphase = "defense"
        game.tribunal_verdict_committed = False
        await game.persist_flush()

        await asyncio.sleep(45)
        if not game.is_active("day") or not game.vote_in_progress or game.day_number != current_day:
            return

        await _complete_tribunal_after_defense(ctx.channel, game, defendant, current_day, alive_role, stand_role, day_vc)

    finally:
        # Always clear the in-progress flag to avoid getting "stuck" if phases change mid-trial.
        game.vote_in_progress = False
        game.tribunal_muted = False
        game.tribunal_defendant_id = None
        game.tribunal_defense_deadline_utc = None
        game.tribunal_judgment_deadline_utc = None
        game.tribunal_judgment_message_id = None
        game.tribunal_subphase = None
        game.tribunal_verdict_committed = False
        # Best-effort persistence so a restart doesn't bypass vote limits.
        try:
            if game.in_progress and not getattr(game, "ending", False):
                await game.persist_flush()
        except Exception:
            pass
        if game.is_active("day") and game.day_number == current_day:
            if day_vc and alive_role:
                try:
                    await day_vc.set_permissions(alive_role, speak=True)
                except discord.HTTPException:
                    pass


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

    state = game.role_states.setdefault(ctx.author.id, {})
    if text is not None and text.strip().lower() == "clear":
        state["will"] = ""
        await game.persist_flush()
        return await ctx.author.send("Cleared your will.")

    current = str(state.get("will", "") or "")
    display = current.strip() or "(empty)"
    await ctx.author.send(
        "**Your Last Will:**\n"
        f"```{display[:1800]}```\n"
        "Use the button below to edit it."
    , view=WillView(owner_id=ctx.author.id, current_text=current))


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
async def importstats(ctx: commands.Context) -> None:
    """
    One-time importer: migrate existing JSON stats to SQLite.
    This does NOT delete the JSON stats file.
    """
    db = getattr(bot, "db", None)
    if not db:
        return await ctx.send("SQLite DB not initialized; cannot import.")
    data = load_stats(ctx.guild.id) or {}
    n = 0
    try:
        n = db.import_player_stats_from_json(guild_id=ctx.guild.id, stats_data=data)
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

    game.role_states.setdefault(ctx.author.id, {})["is_revealed"] = True
    await game.persist_flush()
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
        game.role_states[ctx.author.id]["can_haunt"] = False
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

    game.role_states.setdefault(ctx.author.id, {})["haunt_target"] = target_id
    game.role_states[ctx.author.id]["can_haunt"] = False
    await game.persist_flush()
    await ctx.send(f"You have chosen to haunt **{target.display_name}**. Your soul may now rest.")

# ==========================================
# PLAYER NIGHT ACTION COMMANDS
# ==========================================
@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def kill(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Mobster":
        return
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    await game.set_night_action(ctx, {"type": "kill", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Targeted **{target.display_name}**.")

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def heal(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Doctor":
        return
    target = await game.get_target_from_input(ctx, target_number, allow_self=True)
    if not target:
        return
    if game.role_states.get(target.id, {}).get("is_revealed"):
        return await ctx.send("Cannot heal a revealed Mayor!")
    if target.id == ctx.author.id and game.role_states.get(ctx.author.id, {}).get("self_heals_remaining", 0) <= 0:
        return await ctx.send("You have already used your self-heal!")

    await game.set_night_action(ctx, {"type": "heal", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Healing **{target.display_name}**.")


@heal.autocomplete("target_number")
async def heal_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def roleblock(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) not in ["Escort", "Consort"]:
        return
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    await game.set_night_action(ctx, {"type": "roleblock", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Targeted **{target.display_name}**.")


@roleblock.autocomplete("target_number")
async def roleblock_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def investigate(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    role = game.player_roles.get(ctx.author.id)
    if role not in ["Sheriff", "Investigator", "Mole"]:
        return
    if role == "Mole" and game.role_states.get(ctx.author.id, {}).get("uses_remaining", 0) <= 0:
        return await ctx.send("You have no investigations left.")

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    await game.set_night_action(ctx, {"type": "investigate", "target": target.id, "role": role, "actor": ctx.author.id})
    await ctx.send(f"Investigating **{target.display_name}**.")


@investigate.autocomplete("target_number")
async def investigate_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def shoot(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Vigilante":
        return
    if game.role_states.get(ctx.author.id, {}).get("shots_remaining", 0) <= 0:
        return await ctx.send("No bullets left!")
    if game.role_states.get(ctx.author.id, {}).get("will_die_of_guilt"):
        return await ctx.send("You are overcome with guilt.")

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    await game.set_night_action(ctx, {"type": "shoot", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Aimed at **{target.display_name}**.")


@shoot.autocomplete("target_number")
async def shoot_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def frame(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Framer":
        return
    if game.day_number > 2:
        return await ctx.send("You can only frame on Nights 1 and 2.")

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    await game.set_night_action(ctx, {"type": "frame", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Framing **{target.display_name}**.")


@frame.autocomplete("target_number")
async def frame_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def hide(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Gravedigger":
        return
    if game.role_states.get(ctx.author.id, {}).get("uses_remaining", 0) <= 0:
        return await ctx.send("You have no uses remaining!")

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    await game.set_night_action(ctx, {"type": "hide", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Concealing **{target.display_name}**.")

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def transport(ctx: commands.Context, target1_num: int, target2_num: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Transporter":
        return
    t1 = await game.get_target_from_input(ctx, target1_num, allow_self=True)
    t2 = await game.get_target_from_input(ctx, target2_num, allow_self=True)
    if not t1 or not t2:
        return
    if t1 == t2:
        return await ctx.send("You must choose two different people.")

    await game.set_night_action(ctx, {"type": "transport", "targets": [t1.id, t2.id], "actor": ctx.author.id})
    await ctx.send(f"Swapping **{t1.display_name}** and **{t2.display_name}**.")


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
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Bodyguard":
        return
    target = await game.get_target_from_input(ctx, target_number, allow_self=True)
    if not target:
        return

    state = game.role_states.get(ctx.author.id, {})
    if target.id == ctx.author.id:
        if state.get("self_protects_remaining", 0) <= 0:
            return await ctx.send("You have already used your self-protection!")
        await game.set_night_action(ctx, {"type": "bg_vest", "target": target.id, "actor": ctx.author.id})
        return await ctx.send("Using a bulletproof vest tonight. 🦺")
    else:
        if state.get("uses_remaining", 0) <= 0:
            return await ctx.send("You have already used your protection on someone else!")

    await game.set_night_action(ctx, {"type": "protect", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Protecting **{target.display_name}** tonight.")


@protect.autocomplete("target_number")
async def protect_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def watch(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Lookout":
        return
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return

    await game.set_night_action(ctx, {"type": "watch", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Watching **{target.display_name}** tonight.")


@watch.autocomplete("target_number")
async def watch_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def track(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Tracker":
        return
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return

    await game.set_night_action(ctx, {"type": "track", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Tracking **{target.display_name}** tonight.")


@track.autocomplete("target_number")
async def track_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current)

@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def control(ctx: commands.Context, target1_num: int, target2_num: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Witch":
        return
    t1 = await game.get_target_from_input(ctx, target1_num)
    t2 = await game.get_target_from_input(ctx, target2_num)
    if not t1 or not t2:
        return

    await game.set_night_action(ctx, {"type": "control", "targets": [t1.id, t2.id], "actor": ctx.author.id})
    await ctx.send(f"Attempting to force **{t1.display_name}** to target **{t2.display_name}**.")


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
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Arsonist":
        return
    target = await game.get_target_from_input(ctx, target_number, allow_self=False)
    if not target:
        return

    await game.set_night_action(ctx, {"type": "douse", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Dousing **{target.display_name}**.")


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
        if entry.get("used_by_retri") or entry.get("is_hidden"):
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
        r = entry.get("real_role")
        if r in {"Doctor", "Sheriff", "Investigator", "Lookout", "Tracker", "Escort", "Transporter", "Bodyguard", "Vigilante"}:
            usable.append(entry)

    if corpse_number < 1 or corpse_number > len(usable):
        return await ctx.send("Invalid corpse number. Use `!corpses` first.")
    corpse = usable[corpse_number - 1]
    corpse_role = corpse["real_role"]

    t1 = await game.get_target_from_input(ctx, target1_num, allow_self=True)
    if not t1:
        return
    if corpse_role == "Doctor":
        if game.role_states.get(t1.id, {}).get("is_revealed") and game.player_roles.get(t1.id) == "Mayor":
            return await ctx.send("Cannot heal a revealed Mayor!")
    t2 = None
    if corpse_role == "Transporter":
        if target2_num is None:
            return await ctx.send("Transporter corpse requires two targets: `!reanimate <corpse> <t1> <t2>`.")
        t2 = await game.get_target_from_input(ctx, target2_num, allow_self=True)
        if not t2:
            return
        if t1.id == t2.id:
            return await ctx.send("You must choose two different people.")

    action: Dict = {"type": "reanimate", "actor": ctx.author.id, "corpse_player_id": corpse["player_id"], "corpse_role": corpse_role, "target": t1.id}
    if t2 is not None:
        action["targets"] = [t1.id, t2.id]
    await game.set_night_action(ctx, action)
    await ctx.send("You begin your ritual over the graveyard...")


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def chaos(ctx: commands.Context, target1_num: int, target2_num: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Chaos":
        return
    state = game.role_states.get(ctx.author.id, {})
    if state.get("uses_remaining", 0) <= 0:
        return await ctx.send("You have no uses remaining.")
    t1 = await game.get_target_from_input(ctx, target1_num, allow_self=True)
    t2 = await game.get_target_from_input(ctx, target2_num, allow_self=True)
    if not t1 or not t2:
        return
    if t1.id == t2.id:
        return await ctx.send("You must choose two different people.")
    await game.set_night_action(ctx, {"type": "chaos", "actor": ctx.author.id, "targets": [t1.id, t2.id]})
    await ctx.send("Reality bends around your choices...")

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def hypnotize(ctx: commands.Context, target_number: int, message_type: str) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Hypnotist":
        return

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return

    message_type = message_type.lower()
    valid_types = ["healed", "roleblocked", "transported", "controlled", "attacked"]
    if message_type not in valid_types:
        return await ctx.send(f"❌ Invalid message type. Use: {', '.join(valid_types)}")

    await game.set_night_action(ctx, {"type": "hypnotize", "target": target.id, "msg_type": message_type, "actor": ctx.author.id})
    await ctx.send(f"Sending fake '{message_type}' message to **{target.display_name}**.")

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def tailor(ctx: commands.Context, target_number: int, *, fake_role: str) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Tailor":
        return
    if game.role_states.get(ctx.author.id, {}).get("uses_remaining", 0) <= 0:
        return await ctx.send("You have no uses remaining!")

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return

    fake_role = discord.utils.escape_mentions(discord.utils.escape_markdown(fake_role[:20]))
    await game.set_night_action(ctx, {"type": "tailor", "target": target.id, "fake_role": fake_role, "actor": ctx.author.id})
    await ctx.send(f"Altering **{target.display_name}**'s role to '{fake_role}'.")

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def plunder(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Pirate":
        return
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return

    duel_token = random.randint(1, 2_000_000_000)
    await game.set_night_action(ctx, {"type": "plunder", "target": target.id, "actor": ctx.author.id, "duel_won": False, "duel_finished": False, "duel_token": duel_token})
    CHOICES = {"🪨": "rock", "📄": "paper", "✂️": "scissors"}

    async def finish_duel_if_current() -> None:
        act = game.night_actions.get(ctx.author.id)
        if act and act.get("type") == "plunder" and act.get("duel_token") == duel_token:
            act["duel_won"] = False
            act["duel_finished"] = True
            if game.in_progress and not getattr(game, "ending", False):
                await game.persist_flush()

    async def get_choice(player: discord.Member, prompt: str, is_target: bool = False) -> str:
        try:
            msg = await player.send(prompt)
            for emoji in CHOICES:
                await msg.add_reaction(emoji)
            check = lambda r, u: u.id == player.id and str(r.emoji) in CHOICES and r.message.id == msg.id
            reaction, _ = await bot.wait_for("reaction_add", timeout=DUEL_DURATION, check=check)
            return CHOICES[str(reaction.emoji)]
        except (asyncio.TimeoutError, discord.Forbidden):
            return "TIMEOUT_TARGET" if is_target else "TIMEOUT_PIRATE"

    try:
        results = await asyncio.gather(
            get_choice(ctx.author, "⚔️ Choose your weapon for the plunder! (30s)"),
            get_choice(target, "⚔️ You are being plundered! Choose your weapon. (30s)", is_target=True),
            return_exceptions=True
        )
    finally:
        # If the coroutine is cancelled/crashes mid-duel, ensure the night can still resolve.
        await finish_duel_if_current()

    if not game.in_progress:
        await finish_duel_if_current()
        return

    game_chan = bot.get_channel(game.game_channel_id)
    if not game_chan:
        await finish_duel_if_current()
        return

    await game.sync_living_players(game_chan.guild)
    living_ids = await game.get_living_ids(game_chan.guild)
    if target.id not in living_ids:
        await finish_duel_if_current()
        return await ctx.send("The duel was cancelled — your target died or left during the night.")

    pirate_choice = results[0] if not isinstance(results[0], Exception) else "TIMEOUT_PIRATE"
    target_choice = results[1] if not isinstance(results[1], Exception) else "TIMEOUT_TARGET"

    # Timeout handling: symmetric randomness (avoid double-timeout bias).
    if pirate_choice == "TIMEOUT_PIRATE":
        pirate_choice = random.choice(list(CHOICES.values()))
    if target_choice == "TIMEOUT_TARGET":
        target_choice = random.choice(list(CHOICES.values()))

    winner = None
    if (pirate_choice, target_choice) in [("rock", "scissors"), ("scissors", "paper"), ("paper", "rock")]:
        winner = ctx.author
    elif pirate_choice != target_choice:
        winner = target

    result_msg = f"Pirate chose **{pirate_choice}**, Target chose **{target_choice}**. "
    if winner == ctx.author:
        result_msg += "The Pirate wins the duel! 🏴‍☠️"
        act = game.night_actions.get(ctx.author.id)
        if act and act.get("type") == "plunder" and act.get("duel_token") == duel_token:
            act["duel_won"] = True
            if game.in_progress and not getattr(game, "ending", False):
                await game.persist_flush()
    elif winner == target:
        result_msg += "The Target wins the duel!"
    else:
        result_msg += "It's a draw!"

    act = game.night_actions.get(ctx.author.id)
    if act and act.get("type") == "plunder" and act.get("duel_token") == duel_token:
        act["duel_finished"] = True
        if game.in_progress and not getattr(game, "ending", False):
            await game.persist_flush()

    for p in [ctx.author, target]:
        try:
            await p.send(result_msg)
        except discord.Forbidden:
            pass

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def guard(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Gatekeeper":
        return
    if game.role_states.get(ctx.author.id, {}).get("uses_remaining", 0) <= 0:
        return await ctx.send("You have no guard uses remaining!")
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    await game.set_night_action(ctx, {"type": "guard", "target": target.id, "actor": ctx.author.id})
    await ctx.send(f"Guarding **{target.display_name}**'s location tonight.")

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def vest(ctx: commands.Context) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Survivor":
        return
    if game.role_states.get(ctx.author.id, {}).get("vests_remaining", 0) <= 0:
        return await ctx.send("You have no vests remaining!")
    await game.set_night_action(ctx, {"type": "vest", "target": ctx.author.id, "actor": ctx.author.id})
    await ctx.send("Using a protective vest tonight. 🦺")

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def alert(ctx: commands.Context) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Scary Grandma":
        return
    if game.role_states.get(ctx.author.id, {}).get("alerts_remaining", 0) <= 0:
        return await ctx.send("You have no alerts remaining!")
    await game.set_night_action(ctx, {"type": "alert", "actor": ctx.author.id})
    await ctx.send("You are on alert tonight. Any visitors will be shot. 🔫")

@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def ignite(ctx: commands.Context) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Arsonist":
        return
    await game.set_night_action(ctx, {"type": "ignite", "actor": ctx.author.id})
    await ctx.send("Igniting all doused players tonight. 🔥")


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def clean(ctx: commands.Context) -> None:
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Arsonist":
        return
    await game.set_night_action(ctx, {"type": "clean", "actor": ctx.author.id})
    await ctx.send("Cleaning gasoline off yourself tonight. 🧼")

# ==========================================
# RUN BOT
# ==========================================
async def _connect_forever() -> None:
    """B1: supervised gateway session with backoff on fatal disconnect."""
    _reset_gateway_watchdog_session()
    token = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable not set. Add it to your .env or environment variables before running."
        )
    backoff_min = float(os.getenv("MAFIABOT_RECONNECT_BACKOFF_MIN_SEC", "5"))
    backoff_max = float(os.getenv("MAFIABOT_RECONNECT_BACKOFF_MAX_SEC", "300"))
    backoff = backoff_min
    while True:
        try:
            await bot.start(token, reconnect=True)
            logging.info("Gateway session ended normally.")
            break
        except discord.LoginFailure:
            logging.critical("Discord token rejected — fix credentials.")
            raise SystemExit(1)
        except KeyboardInterrupt:
            logging.info("Shutdown requested.")
            try:
                if not bot.is_closed():
                    await bot.close()
            except Exception:
                pass
            break
        except Exception:
            logging.exception("Disconnected or fatal error — retrying in %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, backoff_max)
            try:
                if not bot.is_closed():
                    await bot.close()
            except Exception:
                logging.debug("bot.close() during reconnect backoff failed", exc_info=True)


def _require_discord_token() -> None:
    if os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN"):
        return
    raise RuntimeError(
        "DISCORD_TOKEN environment variable not set. Add it to your .env or environment variables before running."
    )


_require_discord_token()


if __name__ == "__main__":
    validate_live_settings()
    _acquire_single_instance_lock()
    try:
        asyncio.run(_connect_forever())
    except KeyboardInterrupt:
        logging.info("Interrupted.")
