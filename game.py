from __future__ import annotations

from copy import deepcopy

import asyncio
import logging
import random
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import discord
from discord.ext import commands

from config import (
    ALIVE_ROLE_NAME,
    ALL_MAFIA_ROLES,
    CONTROL_IMMUNE_ROLES,
    DAY_TEXT_CHANNEL_NAME,
    DAY_VOICE_CHANNEL_NAME,
    GAME_CATEGORY_ID,
    GAME_OVERSEER_ROLE_ID,
    GRAVEYARD_TEXT_CHANNEL_NAME,
    GRAVEYARD_VOICE_CHANNEL_NAME,
    MAFIA_CHANNEL_NAME,
    PLAYING_ROLE_ID,
    ROLEBLOCK_IMMUNE_ROLES,
    SEER_HOSTILE_NEUTRAL_ROLES,
    SEER_NEUTRAL_KILLING_ROLES,
    STAND_ROLE_NAME,
    TOWN_ROLES,
    WITCH_TOWN_LOSES_OUTCOMES,
)
from draw_override_wins import (
    collect_draw_override_winners,
    resolve_draw_override_outcome,
)
from endgame_stats import apply_deltas_to_json_players, compute_player_endgame_deltas, deltas_to_sqlite_payloads
from guardian_angel_wins import (
    build_outcome_flags_for_game,
    guardian_angel_joint_win,
    guardian_angel_personal_win,
)
from personal_win_notify import send_personal_win_dm_if_needed
from game_recovery import (
    clear_pending_endgame_meta,
    commit_and_maybe_delete_game_state,
    commit_pending_endgame_before_state_delete,
    delete_game_state_locked,
    persist_pending_endgame_marker,
)
from persistence import is_stale_ended_state, load_state, save_state
from persistence import load_stats, load_stats_meta, save_stats, save_stats_meta
from persist_schema import game_from_persisted, game_to_persisted
from game_state import (
    NightState,
    TribunalState,
    install_game_state_delegates,
)
from engine import night as night_engine
from player_channels import send_to_player_private_channel
from messages import tos as tos_msg
from messages.delivery import dm_member, post_game_channel
from guild_resolve import resolve_game_guild

ACTION_VERBS: Dict[str, str] = {
    "kill": "kill",
    "sk_kill": "stab",
    "heal": "heal",
    "investigate": "investigate",
    "roleblock": "roleblock",
    "watch": "watch",
    "track": "track",
    "transport": "transport",
    "control": "control",
    "protect": "protect",
    "douse": "douse",
    "ignite": "ignite",
    "clean": "clean",
    "plunder": "plunder",
    "gaze": "gaze upon",
    "ward": "protect",
    "chaos": "spread chaos between",
    "frame": "frame",
    "hide": "hide",
    "guard": "guard",
    "hypnotize": "hypnotize",
    "tailor": "tailor",
    "alert": "alert",
    "vest": "vest",
    "bg_vest": "vest",
    "shoot": "shoot",
    "reanimate": "reanimate",
}

# Night actions whose physical visit can be redirected by Transporter (ack hint only).
_VISIT_REDIRECT_ACK_TYPES = frozenset(
    {
        "kill",
        "sk_kill",
        "heal",
        "investigate",
        "roleblock",
        "watch",
        "track",
        "protect",
        "douse",
        "plunder",
        "gaze",
        "ward",
        "frame",
        "hide",
        "guard",
        "hypnotize",
        "tailor",
        "shoot",
        "reanimate",
    }
)


_BOT: Optional[commands.Bot] = None


class MemberLookupUnavailable(OSError):
    """An API/cache failure is not evidence that a player departed."""


def bind_bot(bot: commands.Bot) -> None:
    global _BOT
    _BOT = bot


def unbind_bot() -> None:
    """Test helper: clear bound bot so later tests can inject mocks."""
    global _BOT
    _BOT = None


def _require_bot() -> commands.Bot:
    if _BOT is None:
        raise RuntimeError("Bot not bound. Call game.bind_bot(bot) during startup.")
    return _BOT


def try_get_bot() -> Optional[commands.Bot]:
    """Return bound bot or None (stats/endgame paths that tolerate missing bot)."""
    return _BOT


def _is_empty_game_placeholder(game: "Game") -> bool:
    return not game.in_progress and not game.player_roles and not game.game_key


# --- MULTI-SERVER STATE ---
active_games: Dict[int, "Game"] = {}


def night_resolve_in_progress(game: "Game") -> bool:
    """True while ``!resolve`` holds ``_night_resolve_guard`` (including post-pipeline tail)."""
    lock = getattr(game, "_night_resolve_guard", None)
    if lock is not None:
        locked = getattr(lock, "locked", None)
        if callable(locked) and locked():
            return True
    return bool(getattr(game, "resolving", False))


class Game:
    def __init__(self, guild_id: int) -> None:
        # --- Core loop (persisted: in_progress, phase, day_number, game_key, started_at) ---
        self.guild_id: int = guild_id
        self.in_progress: bool = False
        self.phase: Optional[str] = None
        self.resolving: bool = False
        self.day_number: int = 0
        self.game_key: Optional[str] = None
        self.started_at: Optional[str] = None

        # --- Roster & roles (persisted: players as ids, player_slots, player_roles, role_states) ---
        self.players: List[discord.Member] = []
        self.living_players: List[discord.Member] = []
        self.player_slots: Dict[int, int] = {}
        self.player_roles: Dict[int, str] = {}
        self.role_states: Dict[int, Dict] = {}
        self.member_display_names: Dict[int, str] = {}

        # --- Night (persisted; composed on self.night, flat attrs via delegated properties) ---
        self.night = NightState()
        self.night_transport_swaps = []
        self._transport_pairs_seen = set()
        self.night_transport_dm_pairs = set()
        self._effective_visit_destinations_cache = None

        # --- Graveyard (persisted: graveyard list) ---
        self.graveyard: List[Dict] = []

        # --- Discord infrastructure ids (persisted) ---
        self.game_channel_id: Optional[int] = None
        self.mafia_tc_id: Optional[int] = None
        self.day_tc_id: Optional[int] = None
        self.day_vc_id: Optional[int] = None
        self.grave_tc_id: Optional[int] = None
        self.grave_vc_id: Optional[int] = None
        self.alive_role_id: Optional[int] = None
        self.stand_role_id: Optional[int] = None
        self.locked_channel_ids: List[int] = []
        self.lockdown_role_id: Optional[int] = None

        # --- Tribunal / day vote (persisted; composed on self.tribunal_state, flat attrs via properties) ---
        self.tribunal_state = TribunalState()

        # --- Endgame / stats (persisted: stats_committed, ending) ---
        self.stats_committed: bool = False
        self.refresh_stats_board: bool = False  # runtime: True only if SQLite endgame row exists
        self.ending: bool = False
        self.bloodless_cycle_streak: int = 0
        self.deaths_this_cycle: int = 0
        self.bloodless_stalemate_pending: bool = False
        self.cleanup_pending: bool = False

        # --- Runtime locks (not persisted) ---
        self._endgame_lock: asyncio.Lock = asyncio.Lock()
        self._night_resolve_guard: asyncio.Lock = asyncio.Lock()
        self._persist_lock: asyncio.Lock = asyncio.Lock()
        self._tribunal_start_lock: asyncio.Lock = asyncio.Lock()
        self._reset_lock: asyncio.Lock = asyncio.Lock()
        self._reset_in_progress: bool = False
        self._rehydrate_pending: bool = False
        self._recovering_permissions: bool = False
        self._check_win_active: bool = False
        self._lobby_lock: asyncio.Lock = asyncio.Lock()
        self._startup_lock = asyncio.Lock()
        self._rehydrate_lock = asyncio.Lock()
        self._persist_player_ids = []
        self._persist_living_ids = []
        self._file_write_lock: threading.Lock = threading.Lock()
        self.state_lock = asyncio.Lock()
        self._phase_transition_lock = asyncio.Lock()
        self.delivery_locks = {}
        self.action_cooldowns = {}
        self.gameplay = {"version": 1, "trial": None, "panels": {}, "night_token": None, "deaths": {}, "duels": {}}

    def _graveyard_player_ids(self) -> Set[int]:
        """Ignore malformed legacy metadata when deriving authoritative dead ids."""
        from persist_schema import coerce_opt_int
        dead = set()
        for entry in self.graveyard or []:
            raw = entry.get('player_id') if isinstance(entry, dict) else None
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                continue
            player_id = coerce_opt_int(raw)
            if player_id is not None and player_id > 0:
                dead.add(player_id)
        return dead

    @staticmethod
    def living_ids_excluding_graveyard(game: "Game") -> List[int]:
        """Living roster ids derived from roles minus graveyard (for stats retry)."""
        dead = game._graveyard_player_ids()
        out: List[int] = []
        for pid in game.player_roles:
            try:
                ipid = int(pid)
            except (TypeError, ValueError):
                continue
            if ipid not in dead:
                out.append(ipid)
        return out

    @staticmethod
    def commit_pending_endgame_if_any(guild_id: int) -> bool:
        """
        Commit a prior game's ``pending_endgame`` stats marker when disk state matches.

        Called before ``!startgame`` mints a new ``game_key`` so failed SQLite commits
        are not discarded.
        """
        from game_recovery import _pending_endgame_meta

        from game_recovery import clear_pending_endgame_meta, sqlite_has_game_key

        pending = _pending_endgame_meta(guild_id)
        if not pending:
            return False
        pk = str(pending.get("game_key") or "").strip()
        if not pk:
            return False
        state_data = load_state(guild_id)
        state_key = str(state_data.get("game_key") or "").strip() if state_data else ""
        if not state_data or state_key != pk:
            sqlite_ok = sqlite_has_game_key(pk)
            if sqlite_ok is True:
                clear_pending_endgame_meta(guild_id)
                return True
            if sqlite_ok is False:
                logging.warning(
                    "Retaining pending_endgame: no game JSON and SQLite missing game_key=%s guild_id=%s",
                    pk,
                    guild_id,
                )
                return False
            if state_key and state_key != pk:
                Game.discard_orphan_pending_endgame_meta(guild_id)
            else:
                logging.warning(
                    "Retaining pending_endgame: no matching game JSON guild_id=%s game_key=%s",
                    guild_id,
                    pk,
                )
            return False
        snap = Game.from_persisted(state_data)
        if bool(getattr(snap, "stats_committed", False)):
            db = None
            try:
                bot = try_get_bot()
                db = getattr(bot, "db", None) if bot is not None else None
            except Exception:
                db = None
            sqlite_ok = True
            if db is not None and pk:
                try:
                    hk = db.has_game_key(pk)
                    sqlite_ok = bool(hk) if isinstance(hk, bool) else False
                except Exception:
                    logging.exception(
                        "SQLite game_key verify failed during commit_pending guild_id=%s",
                        guild_id,
                    )
                    sqlite_ok = False
            if sqlite_ok:
                try:
                    meta = dict(load_stats_meta(guild_id))
                    meta.pop("pending_endgame", None)
                    save_stats_meta(guild_id, meta)
                except Exception:
                    logging.exception(
                        "Failed to clear pending_endgame after stats already committed guild_id=%s",
                        guild_id,
                    )
                return True
            logging.warning(
                "JSON stats_committed=True but SQLite row missing; re-committing "
                "guild_id=%s game_key=%s",
                guild_id,
                pk,
            )
            snap.stats_committed = False
        living_retry: List[int] = []
        raw_living = pending.get("living_ids")
        if isinstance(raw_living, (list, tuple)):
            for x in raw_living:
                try:
                    living_retry.append(int(x))
                except (TypeError, ValueError):
                    continue
        if not living_retry:
            living_retry = Game.living_ids_excluding_graveyard(snap)
        snap._commit_endgame_stats(
            outcome=str(pending["outcome"]),
            living_ids=living_retry,
        )
        return bool(getattr(snap, "stats_committed", False))

    @staticmethod
    def discard_orphan_pending_endgame_meta(guild_id: int) -> bool:
        """Drop ``pending_endgame`` only when it cannot attach and SQLite lacks the game_key."""
        from game_recovery import _pending_endgame_meta, clear_pending_endgame_meta, sqlite_has_game_key

        pending = _pending_endgame_meta(guild_id)
        if not pending:
            return False
        pk = str(pending.get("game_key") or "").strip()
        state_data = load_state(guild_id)
        state_key = str(state_data.get("game_key") or "").strip() if state_data else ""
        if pk and state_key and pk == state_key:
            return False
        if pk and sqlite_has_game_key(pk) is True:
            clear_pending_endgame_meta(guild_id)
            return True
        if not state_data:
            return False
        if pk and state_key and pk != state_key:
            clear_pending_endgame_meta(guild_id)
            return True
        return False

    async def commit_endgame_stats_async(self, *, outcome: str, living_ids: List[int]) -> None:
        """Commit a detached model and drain its worker before reset can clear state."""
        from copy import copy, deepcopy
        from async_work import run_blocking
        async with self.state_lock:
            working = copy(self)
            for field in ('player_roles', 'role_states', 'graveyard', 'night', 'tribunal_state', 'gameplay'):
                setattr(working, field, deepcopy(getattr(self, field)))
            working.players = list(self.players)
            working.living_players = list(self.living_players)
            try:
                await run_blocking(working._commit_endgame_stats, outcome=outcome, living_ids=list(living_ids))
            finally:
                # The worker is finished even when cancellation is propagated.
                self.stats_committed = working.stats_committed
                self.refresh_stats_board = working.refresh_stats_board
                self.game_key, self.started_at = working.game_key, working.started_at

    def _commit_endgame_stats(self, *, outcome: str, living_ids: List[int]) -> None:
        """
        Update per-guild persistent player stats (SQLite when DB is attached).

        Win rules: see endgame_stats.compute_player_endgame_deltas.
        """
        participants = list(self.player_roles.keys())
        if not participants:
            return
        if bool(getattr(self, "stats_committed", False)):
            return

        with self._file_write_lock:
            self._commit_endgame_stats_locked(outcome=outcome, living_ids=living_ids)

    def _commit_endgame_stats_locked(self, *, outcome: str, living_ids: List[int]) -> None:
        participants = list(self.player_roles.keys())
        if not participants:
            return
        self.refresh_stats_board = False
        meta = load_stats_meta(self.guild_id)

        try:
            bot = _require_bot()
        except RuntimeError:
            bot = None
        db = getattr(bot, "db", None) if bot is not None else None

        if db is not None and not self.game_key:
            nonce = secrets.token_hex(8)
            self.started_at = self.started_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            self.game_key = f"{self.guild_id}:{self.started_at}:{nonce}"
            try:
                save_state(self.guild_id, self.to_persisted())
            except Exception:
                logging.exception(
                    "Failed to persist game_key before endgame stats commit guild_id=%s",
                    self.guild_id,
                )

        game_key_str = str(self.game_key) if self.game_key else ""
        meta_pending = meta.get("pending_endgame")
        if isinstance(meta_pending, dict):
            pk = str(meta_pending.get("game_key") or "").strip()
            if pk and game_key_str and pk == game_key_str:
                try:
                    pending_outcome = str(meta_pending.get("outcome") or "")
                    if pending_outcome:
                        outcome = pending_outcome
                    raw_living = meta_pending.get("living_ids")
                    if isinstance(raw_living, (list, tuple)) and raw_living:
                        living_ids = [int(x) for x in raw_living]
                except (TypeError, ValueError):
                    pass
            elif pk and game_key_str and pk != game_key_str:
                logging.warning(
                    "Ignoring stale pending_endgame (guild_id=%s pending_key=%s current_key=%s)",
                    self.guild_id,
                    pk,
                    game_key_str,
                )
                try:
                    cleared = dict(meta)
                    cleared.pop("pending_endgame", None)
                    save_stats_meta(self.guild_id, cleared)
                    meta = cleared
                except Exception:
                    logging.exception(
                        "Failed to clear stale pending_endgame guild_id=%s",
                        self.guild_id,
                    )

        sql_done = False
        if db is not None and game_key_str:
            try:
                hk = db.has_game_key(game_key_str)
                if isinstance(hk, bool):
                    sql_done = hk
            except Exception:
                logging.exception(
                    "SQLite game_key lookup failed guild_id=%s game_key=%s",
                    self.guild_id,
                    game_key_str,
                )

        if sql_done:
            self.stats_committed = True
            self.refresh_stats_board = True
            if meta.get("pending_endgame"):
                cleared = dict(meta)
                cleared.pop("pending_endgame", None)
                try:
                    save_stats_meta(self.guild_id, cleared)
                except Exception:
                    logging.exception(
                        "Failed to clear pending_endgame marker guild_id=%s",
                        self.guild_id,
                    )
            return

        outcome_norm = str(outcome)
        deltas = compute_player_endgame_deltas(
            player_roles=self.player_roles,
            role_states=self.role_states,
            living_ids=set(living_ids),
            outcome_norm=outcome_norm,
        )

        sqlite_committed = sql_done
        if db is not None and not sql_done:
            ended_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            game_rows, player_stats_deltas, role_stats_deltas, personal_win_deltas = deltas_to_sqlite_payloads(
                deltas, ended_at=ended_at
            )

            last_sql_err: Optional[BaseException] = None
            for attempt in range(5):
                try:
                    is_first, _game_id = db.commit_endgame_atomic(
                        guild_id=self.guild_id,
                        game_key=str(self.game_key),
                        started_at=self.started_at,
                        ended_at=ended_at,
                        outcome=outcome_norm,
                        player_count=len(participants),
                        ended_day_number=int(self.day_number),
                        ended_phase=str(self.phase) if self.phase else None,
                        game_player_rows=game_rows,
                        player_stats_deltas=player_stats_deltas,
                        role_stats_deltas=role_stats_deltas,
                        personal_win_deltas=personal_win_deltas,
                    )
                    last_sql_err = None
                    if is_first:
                        sqlite_committed = True
                    else:
                        try:
                            sqlite_committed = bool(db.has_game_key(game_key_str))
                        except Exception:
                            logging.exception(
                                "SQLite game_key lookup after is_first=False guild_id=%s",
                                self.guild_id,
                            )
                            sqlite_committed = False
                        if not sqlite_committed:
                            logging.error(
                                "Endgame SQLite commit returned is_first=False but game_key "
                                "missing (guild_id=%s game_key=%s)",
                                self.guild_id,
                                game_key_str,
                            )
                    break
                except Exception as e:
                    last_sql_err = e
                    time.sleep(min(0.2, 0.04 * (2**attempt)))
            if last_sql_err is not None:
                logging.error(
                    "SQLite endgame commit failed; wrote pending_endgame marker "
                    "(guild_id=%s game_key=%s).",
                    self.guild_id,
                    self.game_key,
                    exc_info=last_sql_err,
                )
                if game_key_str:
                    persist_pending_endgame_marker(
                        self.guild_id,
                        pending={
                            "game_key": game_key_str,
                            "outcome": outcome_norm,
                            "living_ids": [int(x) for x in living_ids],
                        },
                        meta=meta,
                    )
                return

            if not sqlite_committed:
                logging.error(
                    "Endgame SQLite commit did not persist game_key (guild_id=%s game_key=%s)",
                    self.guild_id,
                    game_key_str,
                )
                if game_key_str:
                    persist_pending_endgame_marker(
                        self.guild_id,
                        pending={
                            "game_key": game_key_str,
                            "outcome": outcome_norm,
                            "living_ids": [int(x) for x in living_ids],
                        },
                        meta=meta,
                    )
                return

            self.stats_committed = True
            self.refresh_stats_board = True
            if meta.get("pending_endgame"):
                try:
                    clear_pending_endgame_meta(self.guild_id)
                except Exception:
                    logging.exception(
                        "Failed to clear pending_endgame marker guild_id=%s",
                        self.guild_id,
                    )
            try:
                bot = try_get_bot()
                db = getattr(bot, "db", None) if bot is not None else None
                if db is not None:
                    from stats_mirror_repair import repair_guild_json_mirror_from_sqlite

                    repair_guild_json_mirror_from_sqlite(
                        db,
                        guild_id=int(self.guild_id),
                        game_key=game_key_str or None,
                    )
            except Exception:
                logging.exception(
                    "JSON stats mirror refresh after endgame commit failed guild_id=%s",
                    self.guild_id,
                )
            return

        # No SQLite (headless tests): JSON player aggregates only.
        data = load_stats(self.guild_id) or {}
        players = data.setdefault("players", {})
        apply_deltas_to_json_players(players, deltas)
        if game_key_str:
            data.setdefault("_meta", {})["last_json_game_key"] = game_key_str

        last_json_err: Optional[BaseException] = None
        for attempt in range(5):
            try:
                save_stats(self.guild_id, data)
                last_json_err = None
                break
            except Exception as e:
                last_json_err = e
                time.sleep(min(0.2, 0.04 * (2**attempt)))
        if last_json_err is not None:
            logging.exception(
                "JSON stats save failed without SQLite (guild_id=%s game_key=%s).",
                self.guild_id,
                getattr(self, "game_key", None),
            )
            return

        self.stats_committed = True
        self.refresh_stats_board = False
        try:
            file_meta = data.get("_meta")
            if isinstance(file_meta, dict) and "pending_endgame" in file_meta:
                cleared = dict(file_meta)
                cleared.pop("pending_endgame", None)
                save_stats_meta(self.guild_id, cleared)
        except Exception:
            logging.exception("Failed to clear pending_endgame marker guild_id=%s", self.guild_id)

    def to_persisted(self) -> Dict:
        return game_to_persisted(self)

    @staticmethod
    def from_persisted(data: Dict) -> "Game":
        return game_from_persisted(data)

    async def lookup_member(self, guild, user_id):
        """Only an authoritative 404 means departure; transient errors abort reconciliation."""
        uid = int(user_id)
        member = guild.get_member(uid)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(uid)
        except discord.NotFound:
            return None
        except (discord.Forbidden, discord.HTTPException) as error:
            raise MemberLookupUnavailable('Cannot verify server membership; retry shortly.') from error

    async def rehydrate_members(self, guild: discord.Guild) -> None:
        from types import SimpleNamespace
        from gameplay.state import commit
        from gameplay.death import apply_death
        player_ids = list(getattr(self, '_persist_player_ids', []))
        dead_ids = self._graveyard_player_ids()
        living_ids = [pid for pid in getattr(self, '_persist_living_ids', []) if pid not in dead_ids]
        found = {pid: await self.lookup_member(guild, pid) for pid in player_ids}
        departed = [pid for pid in living_ids if found.get(pid) is None]
        placeholders = {pid: SimpleNamespace(id=pid, display_name=f'Player {pid}', mention=f'<@{pid}>', roles=[])
                        for pid in player_ids if found[pid] is None}
        expected = self.game_key
        def hydrate():
            if self.game_key != expected or self.ending:
                return
            self.players = [found[pid] or placeholders[pid] for pid in player_ids]
            self.living_players = [found.get(pid) or placeholders[pid] for pid in living_ids if pid in found]
            for pid in departed:
                if self.in_progress:
                    apply_death(self, pid, 'left')
            self._rehydrate_pending = False
            self._persist_player_ids = []
            self._persist_living_ids = []
        await commit(self, hydrate, persist=bool(departed) and self.in_progress, allow_recovery=True)
        if departed and self.in_progress and not self.ending and not self._recovering_permissions:
            for pid in departed:
                receipt = self.gameplay['deaths'].get(str(pid))
                if receipt:
                    await self.deliver_death_receipt(None, guild, receipt)

    async def _sweep_executioner_conversion_for(
        self,
        guild: "discord.Guild",
        *,
        dead_player_id: int,
        cause: str,
    ) -> None:
        """Audit C2 — shared Executioner-target sweep.

        If a living Executioner's exe_target just died, convert the Exe
        to Jester on any non-lynch death, or mark exe_won on lynch.
        Used by process_death, process_death_by_id, and the
        rehydrate_members leaver-as-death path so the conversion fires
        regardless of which death surface produced the death.
        """
        from personal_win_notify import send_personal_win_dm_if_needed

        living_ids = await self.get_living_ids(guild)
        for p_id, state in list(self.role_states.items()):
            if p_id not in living_ids:
                continue
            if self.player_roles.get(p_id) != "Executioner":
                continue
            if state.get("exe_target") != int(dead_player_id):
                continue
            if cause == "lynch":
                self.role_states.setdefault(p_id, {})["exe_won"] = True
                await send_personal_win_dm_if_needed(self, guild, int(p_id))
                continue
            self.player_roles[p_id] = "Jester"
            st = self.role_states.setdefault(p_id, {})
            st.pop("exe_target", None)
            st["night1_shield_used"] = False
            exe_player = await self.get_member_safe(guild, p_id)
            if exe_player is not None:
                await dm_member(exe_player, tos_msg.exe_convert_jester())

    async def persist_flush(self):
        if not self.in_progress and not self.player_roles:
            return
        owner = active_games.get(self.guild_id)
        if owner is not None and owner is not self:
            return
        from persistence import save_state_async
        await save_state_async(self.guild_id, self.to_persisted())

    def persist_now(self):
        save_state(self.guild_id, self.to_persisted())

    def is_active(self, phase: Optional[str] = None) -> bool:
        if not self.in_progress:
            return False
        if phase is not None and self.phase != phase:
            return False
        return True

    async def get_member_safe(self, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        # Be tolerant of corrupted persisted state where IDs may not be ints.
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return None

        member = guild.get_member(uid)
        if not member:
            try:
                member = await guild.fetch_member(uid)
            except (TypeError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return member

    async def ensure_rehydrated(self, guild) -> None:
        async with self._rehydrate_lock:
            if self._rehydrate_pending:
                await self.rehydrate_members(guild)

    async def sync_living_players(self, guild):
        if guild is None:
            raise MemberLookupUnavailable('The game server is unavailable.')
        await self.ensure_rehydrated(guild)
        from gameplay.state import commit
        from gameplay.death import apply_death
        expected = (self.game_key, self.day_number)
        dead_ids = self._graveyard_player_ids()
        candidates = [p for p in self.living_players if p.id not in dead_ids]
        members = {p.id: await self.lookup_member(guild, p.id) for p in candidates}
        departed = [uid for uid, member in members.items() if member is None]
        def refresh():
            if expected != (self.game_key, self.day_number) or self.ending:
                return
            for uid in departed:
                if self.in_progress:
                    apply_death(self, uid, 'left')
            current_dead = self._graveyard_player_ids()
            self.living_players = sorted([members.get(p.id) or p for p in self.living_players if p.id not in current_dead],
                key=lambda m: (self.player_slots.get(m.id, 10**9), m.display_name.lower()))
        await commit(self, refresh, persist=bool(departed) and self.in_progress)
        for uid in departed:
            receipt = self.gameplay['deaths'].get(str(uid))
            if receipt:
                await self.deliver_death_receipt(None, guild, receipt)

    def _engine_player_ids(self) -> Set[int]:
        ids: Set[int] = set()
        for raw in self.player_roles:
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                continue
        for raw in self.player_slots:
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                continue
        return ids

    async def reconcile_member_discord_roles(
        self, guild: discord.Guild, member: discord.Member
    ) -> None:
        """Align Discord game roles with engine living/graveyard (e.g. leaver rejoin)."""
        if not self.in_progress or member.id not in self._engine_player_ids():
            return

        living_ids = {p.id for p in self.living_players}
        alive_role = guild.get_role(self.alive_role_id) if self.alive_role_id else None
        stand_role = guild.get_role(self.stand_role_id) if self.stand_role_id else None
        playing_role = guild.get_role(PLAYING_ROLE_ID)

        if member.id not in living_ids:
            to_remove = [
                r
                for r in (alive_role, stand_role, playing_role)
                if r is not None and r in member.roles
            ]
            if to_remove:
                try:
                    await member.remove_roles(*to_remove)
                except discord.HTTPException:
                    logging.warning(
                        "reconcile_member_discord_roles: could not remove roles user_id=%s",
                        member.id,
                        exc_info=True,
                    )
            if self.player_roles.get(member.id) in ALL_MAFIA_ROLES:
                mafia_tc = guild.get_channel(self.mafia_tc_id) if self.mafia_tc_id else None
                if mafia_tc is not None:
                    try:
                        await mafia_tc.set_permissions(member, overwrite=None)
                    except discord.HTTPException:
                        pass
                    except Exception:
                        pass
            return

        if alive_role is not None and alive_role not in member.roles:
            try:
                await member.add_roles(alive_role)
            except discord.HTTPException:
                logging.warning(
                    "reconcile_member_discord_roles: could not add alive_role user_id=%s",
                    member.id,
                    exc_info=True,
                )

    async def get_living_ids(self, guild: discord.Guild) -> List[int]:
        return [p.id for p in self.living_players]

    def ordered_living_players(self) -> List[discord.Member]:
        return sorted(self.living_players, key=lambda m: (self.player_slots.get(m.id, 10**9), m.display_name.lower()))

    async def get_target_from_input(
        self, ctx: commands.Context, target_number: int, *, allow_self: bool = False
    ) -> Optional[discord.Member]:
        guild = ctx.guild or ctx.bot.get_guild(self.guild_id)
        if not guild:
            try:
                await ctx.send("❌ Could not find the server for this game.")
            except discord.HTTPException:
                pass
            return None

        await self.sync_living_players(guild)
        living_ids = await self.get_living_ids(guild)
        living_members = self.ordered_living_players()
        if not living_ids:
            try:
                await ctx.send("❌ There are no living players.")
            except discord.HTTPException:
                pass
            return None

        slot_to_member: Dict[int, discord.Member] = {}
        for m in living_members:
            slot = self.player_slots.get(m.id)
            if slot is None:
                continue
            # If duplicates exist (shouldn't), prefer the first stable ordering result.
            slot_to_member.setdefault(slot, m)

        if target_number not in slot_to_member:
            valid_slots = sorted(slot_to_member.keys())
            try:
                slots_txt = ", ".join(str(s) for s in valid_slots) if valid_slots else "(none)"
                await ctx.send(f"❌ Invalid slot. Valid slots: {slots_txt}.")
            except discord.HTTPException:
                pass
            return None

        target = slot_to_member[target_number]
        target = await self.get_member_safe(guild, target.id) or target

        if target.id not in living_ids:
            try:
                await ctx.send("❌ That player is not alive.")
            except discord.HTTPException:
                pass
            return None

        if (not allow_self) and target.id == ctx.author.id:
            try:
                await ctx.send("❌ You cannot target yourself.")
            except discord.HTTPException:
                pass
            return None

        return target

    async def setup_infrastructure(self, guild: discord.Guild) -> None:
        from gameplay.state import commit, Rejected
        expected = (self.game_key, self.day_number, self.in_progress)
        references = {}
        try:
            alive_role = discord.utils.get(guild.roles, name=ALIVE_ROLE_NAME)
            if not alive_role:
                alive_role = await guild.create_role(name=ALIVE_ROLE_NAME, color=discord.Color.green())
            references['alive_role_id'] = alive_role.id

            stand_role = discord.utils.get(guild.roles, name=STAND_ROLE_NAME)
            if not stand_role:
                stand_role = await guild.create_role(name=STAND_ROLE_NAME, color=discord.Color.red())
            references['stand_role_id'] = stand_role.id

            category = guild.get_channel(GAME_CATEGORY_ID)
            if category and not isinstance(category, discord.CategoryChannel):
                category = None
            if not category:
                category = discord.utils.get(guild.categories, name="Mafia Game")
            if not category:
                category = await guild.create_category("Mafia Game")

            playing_role = guild.get_role(PLAYING_ROLE_ID)
            overseer_role = guild.get_role(GAME_OVERSEER_ROLE_ID)

            from gameplay.access import private_overwrites as build_private_overwrites
            private_overwrites = build_private_overwrites(guild, alive_role)
            grave_overwrites = dict(private_overwrites)
            mafia_overwrites = dict(private_overwrites)

            for name, attr, creator in [
                (
                    DAY_TEXT_CHANNEL_NAME,
                    "day_tc_id",
                    lambda: guild.create_text_channel(DAY_TEXT_CHANNEL_NAME, category=category),
                ),
                (
                    MAFIA_CHANNEL_NAME,
                    "mafia_tc_id",
                    lambda: guild.create_text_channel(
                        MAFIA_CHANNEL_NAME, category=category, overwrites=mafia_overwrites
                    ),
                ),
                (
                    GRAVEYARD_TEXT_CHANNEL_NAME,
                    "grave_tc_id",
                    lambda: guild.create_text_channel(
                        GRAVEYARD_TEXT_CHANNEL_NAME, category=category, overwrites=grave_overwrites
                    ),
                ),
                (
                    DAY_VOICE_CHANNEL_NAME,
                    "day_vc_id",
                    lambda: guild.create_voice_channel(DAY_VOICE_CHANNEL_NAME, category=category),
                ),
                (
                    GRAVEYARD_VOICE_CHANNEL_NAME,
                    "grave_vc_id",
                    lambda: guild.create_voice_channel(
                        GRAVEYARD_VOICE_CHANNEL_NAME, category=category, overwrites=grave_overwrites
                    ),
                ),
            ]:
                existing = discord.utils.get(guild.channels, name=name)
                if not existing:
                    existing = await creator()
                references[attr] = existing.id

            # Harden privacy even if channels pre-existed with stale overwrites.
            mafia_tc = guild.get_channel(references['mafia_tc_id'])
            graveyard_tc = guild.get_channel(references['grave_tc_id'])
            graveyard_vc = guild.get_channel(references['grave_vc_id'])
            for ch in [mafia_tc, graveyard_tc, graveyard_vc]:
                if not ch:
                    continue
                # Replace the complete ACL atomically, removing stale member and role allows.
                # Do not publish any private content if normalization fails.
                await ch.edit(overwrites=dict(private_overwrites), reason='Reconcile game-channel privacy')

            # Category-based access model:
            # - Everyone in game gets "Playing"
            # - Non-staff gets "Mafia - Lockdown"
            # - Mafia Game category: visible to spectators (@everyone) + Playing + staff.
            # - Other categories: hidden from "Mafia - Lockdown" only (staff still sees all).
            if playing_role:
                try:
                    # Spectators should be able to watch the day chat; keep the game category visible.
                    await category.set_permissions(guild.default_role, view_channel=True)
                    await category.set_permissions(playing_role, view_channel=True)
                    if overseer_role:
                        await category.set_permissions(overseer_role, view_channel=True)
                except discord.HTTPException:
                    logging.warning("Failed to set Mafia Game category permissions.", exc_info=True)

            # Create/get lockdown role and apply it at the CATEGORY level (much fewer API calls).
            lockdown_role = discord.utils.get(guild.roles, name="Mafia - Lockdown")
            if not lockdown_role:
                try:
                    lockdown_role = await guild.create_role(name="Mafia - Lockdown", mentionable=False)
                except discord.HTTPException:
                    lockdown_role = None
            if lockdown_role:
                references['lockdown_role_id'] = lockdown_role.id

            if lockdown_role:
                locked: List[int] = []
                for cat in list(guild.categories):
                    if cat.id == category.id:
                        continue
                    try:
                        await cat.set_permissions(lockdown_role, view_channel=False)
                        locked.append(cat.id)
                    except discord.HTTPException:
                        logging.warning("Failed to apply lockdown to category %s.", getattr(cat, "id", "unknown"), exc_info=True)
                references['locked_channel_ids'] = locked

                # Ensure lockdown can see the Mafia Game category (non-staff players need to see game channels).
                try:
                    await category.set_permissions(lockdown_role, view_channel=True)
                except discord.HTTPException:
                    logging.warning("Failed to grant lockdown role view on Mafia Game category.", exc_info=True)

            # Ensure sensitive channels stay private even though the category is visible to Playing.
            # (Already normalized above; keep this block as a no-op safety net if Discord drops overwrites.)
            if playing_role:
                for ch in [mafia_tc, graveyard_tc, graveyard_vc]:
                    if ch:
                        try:
                            await ch.set_permissions(playing_role, view_channel=False)
                        except discord.HTTPException:
                            logging.warning("Failed to hide private channel from Playing role.", exc_info=True)

            day_vc = guild.get_channel(references['day_vc_id'])
            if day_vc:
                try:
                    await day_vc.set_permissions(guild.default_role, speak=False, connect=False)
                    await day_vc.set_permissions(alive_role, connect=True, speak=True)
                    await day_vc.set_permissions(stand_role, connect=True, speak=True)
                except discord.HTTPException:
                    logging.warning("Failed to set day VC permissions (speak/connect).", exc_info=True)
            def save_references():
                if (active_games.get(self.guild_id) not in (None, self) or self.ending
                        or expected != (self.game_key, self.day_number, self.in_progress)):
                    raise Rejected('The game changed during channel setup.')
                for field, value in references.items():
                    setattr(self, field, value)
            await commit(self, save_references, persist=active_games.get(self.guild_id) is self)
        except (discord.Forbidden, discord.HTTPException) as e:
            raise RuntimeError(
                "Infrastructure setup failed. Ensure the bot has Administrator (or Manage Roles/Channels) permissions."
            ) from e

    async def _historical_reset(self, guild: discord.Guild, *, _from_check_win: bool = False) -> None:
        """Clear game infra. When not called from ``check_win``, serializes on ``_endgame_lock``."""
        if _from_check_win:
            await self._reset_impl_wrapped(guild)
            return

        async with self._endgame_lock:
            await self._reset_impl_wrapped(guild)

    async def _reset_impl_wrapped(self, guild: discord.Guild) -> None:
        # HP05 — one reset at a time; do not flip terminal flags if already resetting.
        async with self._reset_lock:
            if self._reset_in_progress:
                return
            self._reset_in_progress = True
        # CR16 — terminal flags before any await so concurrent commands see ended state.
        self.in_progress = False
        self.ending = True
        try:
            await self.persist_flush()
        except Exception:
            logging.exception("persist_flush failed at reset start guild_id=%s", self.guild_id)
        try:
            await self._reset_impl(guild)
        finally:
            self._reset_in_progress = False

    def _enqueue_game_over_dms(self) -> None:
        """Queue or send GAME OVER DMs without tearing down channels/roles."""
        try:
            bot = _require_bot()
            db = getattr(bot, "db", None)
        except RuntimeError:
            db = None
        gk = self.game_key or "unknown"
        for player in list(self.players):
            if db:
                db.enqueue_or_requeue_dm_outbox(
                    guild_id=self.guild_id,
                    kind="game_over",
                    match_key=self.game_key,
                    dedupe_key=f"mafia_game_over:{self.guild_id}:{gk}:{player.id}",
                    target_user_id=player.id,
                    content="--- GAME OVER ---\nThe game has ended.",
                )

    async def _reset_impl(self, guild: discord.Guild) -> None:
        self.resolving = False
        # Allow a subsequent game to commit stats again.
        self.stats_committed = False

        bot = try_get_bot()
        db = getattr(bot, "db", None) if bot is not None else None
        if db:
            from async_work import run_blocking
            await run_blocking(self._enqueue_game_over_dms)
        elif bot is not None:
            for player in list(self.players):
                try:
                    await player.send("--- GAME OVER ---\nThe game has ended.")
                except discord.HTTPException:
                    pass

        alive_role = guild.get_role(self.alive_role_id) if self.alive_role_id else None
        stand_role = guild.get_role(self.stand_role_id) if self.stand_role_id else None
        playing_role = guild.get_role(PLAYING_ROLE_ID)
        mafia_tc = guild.get_channel(self.mafia_tc_id) if self.mafia_tc_id else None
        graveyard_tc = guild.get_channel(self.grave_tc_id) if self.grave_tc_id else None
        graveyard_vc = guild.get_channel(self.grave_vc_id) if self.grave_vc_id else None

        # Best-effort cleanup should not rely solely on tracked members:
        # players can leave/rejoin, or state can desync after restarts.
        for m in list(getattr(guild, "members", []) or []):
            try:
                to_remove = []
                for r in [alive_role, stand_role, playing_role]:
                    if r and r in m.roles:
                        to_remove.append(r)
                if to_remove:
                    await m.remove_roles(*to_remove)
            except discord.HTTPException:
                pass

        async def _clear_member_overwrites(ch) -> None:
            if not ch or not hasattr(ch, "overwrites"):
                return
            try:
                for target in list(ch.overwrites.keys()):
                    if isinstance(target, discord.Member):
                        try:
                            await ch.set_permissions(target, overwrite=None)
                        except discord.HTTPException:
                            pass
            except Exception:
                pass

        if mafia_tc:
            await _clear_member_overwrites(mafia_tc)
            # Also clear by raw user id so leavers / non-Member overwrite keys
            # cannot keep mafia-chat access after reset (audit: deep review).
            for pid, role in list(self.player_roles.items()):
                if role in ALL_MAFIA_ROLES:
                    try:
                        await mafia_tc.set_permissions(discord.Object(id=int(pid)), overwrite=None)
                    except discord.HTTPException:
                        pass
                    except Exception:
                        pass
        if graveyard_tc:
            await _clear_member_overwrites(graveyard_tc)
        if graveyard_vc:
            await _clear_member_overwrites(graveyard_vc)

        # Unlock categories and remove the lockdown role (if used).
        lockdown_role = guild.get_role(self.lockdown_role_id) if self.lockdown_role_id else None
        if lockdown_role:
            for m in list(getattr(guild, "members", []) or []):
                try:
                    if lockdown_role in m.roles:
                        await m.remove_roles(lockdown_role)
                except discord.HTTPException:
                    pass
            for ch_id in list(self.locked_channel_ids):
                cat = guild.get_channel(ch_id)
                if not cat:
                    continue
                try:
                    await cat.set_permissions(lockdown_role, overwrite=None)
                except discord.HTTPException:
                    pass
        self.locked_channel_ids = []
        self.lockdown_role_id = None

        day_vc = guild.get_channel(self.day_vc_id) if self.day_vc_id else None
        if day_vc and alive_role:
            try:
                await day_vc.set_permissions(alive_role, speak=True)
            except discord.HTTPException:
                pass

        from async_work import run_blocking
        await run_blocking(commit_and_maybe_delete_game_state, self.guild_id)

        # (Already marked ended at the start of reset.)
        self.phase = None
        self.day_number = 0
        self.game_key = None
        self.started_at = None
        self.players.clear()
        self.living_players.clear()
        self.player_slots.clear()
        self.player_roles.clear()
        self.role_states.clear()
        self.graveyard.clear()
        self.night.clear_persisted()
        self.tribunal_state.clear_persisted()
        self.votes_today = 0
        self.bloodless_cycle_streak = 0
        self.deaths_this_cycle = 0
        self.bloodless_stalemate_pending = False
        self.cleanup_pending = False
        self.game_channel_id = None
        self.mafia_tc_id = None
        self.day_tc_id = None
        self.day_vc_id = None
        self.grave_tc_id = None
        self.grave_vc_id = None
        self.alive_role_id = None
        self.stand_role_id = None
        active_games.pop(self.guild_id, None)
        logging.info(f"Game reset for guild {self.guild_id}.")

    async def _historical_nuke_reset(self, guild: discord.Guild) -> None:
        """
        Nuclear cleanup that does not rely on a healthy in-memory game state.
        Best-effort: removes game roles from members, clears category/channel overwrites,
        deletes bot-created channels, and drops persisted state.
        """
        async with self._reset_lock:
            if self._reset_in_progress:
                return
            self._reset_in_progress = True
        try:
            async with self._endgame_lock:
                await self._nuke_reset_impl(guild)
        finally:
            self._reset_in_progress = False

    async def _nuke_reset_impl(self, guild: discord.Guild) -> None:
        self.in_progress = False
        self.ending = True
        self.resolving = False
        bot = try_get_bot()
        db = getattr(bot, "db", None) if bot is not None else None
        if db:
            from async_work import run_blocking
            await run_blocking(self._enqueue_game_over_dms)
        elif bot is not None:
            for player in list(self.players):
                try:
                    await player.send("--- GAME OVER ---\nThe game has ended.")
                except discord.HTTPException:
                    pass
        try:
            await self.persist_flush()
        except Exception:
            logging.exception("persist_flush failed at nuke_reset start guild_id=%s", self.guild_id)
        alive_role = guild.get_role(self.alive_role_id) if self.alive_role_id else discord.utils.get(guild.roles, name=ALIVE_ROLE_NAME)
        stand_role = guild.get_role(self.stand_role_id) if self.stand_role_id else discord.utils.get(guild.roles, name=STAND_ROLE_NAME)
        playing_role = guild.get_role(PLAYING_ROLE_ID)
        lockdown_role = guild.get_role(self.lockdown_role_id) if self.lockdown_role_id else discord.utils.get(guild.roles, name="Mafia - Lockdown")

        # Remove roles from all members (not just tracked players).
        for m in list(getattr(guild, "members", []) or []):
            try:
                to_remove = []
                for r in [alive_role, stand_role, playing_role, lockdown_role]:
                    if r and r in m.roles:
                        to_remove.append(r)
                if to_remove:
                    await m.remove_roles(*to_remove)
            except discord.HTTPException:
                pass

        # Clear permission overwrites on the Mafia Game category and categories we previously locked.
        category = guild.get_channel(GAME_CATEGORY_ID)
        if category and isinstance(category, discord.CategoryChannel):
            for r in [playing_role, lockdown_role, guild.default_role]:
                if r:
                    try:
                        await category.set_permissions(r, overwrite=None)
                    except discord.HTTPException:
                        pass

        if lockdown_role:
            for cat_id in list(self.locked_channel_ids):
                cat = guild.get_channel(cat_id)
                if cat and isinstance(cat, discord.CategoryChannel):
                    try:
                        await cat.set_permissions(lockdown_role, overwrite=None)
                    except discord.HTTPException:
                        pass

        # Clear per-channel member/role overwrites for bot-managed channels (if they exist).
        for ch_id in [self.day_tc_id, self.mafia_tc_id, self.grave_tc_id, self.day_vc_id, self.grave_vc_id]:
            ch = guild.get_channel(ch_id) if ch_id else None
            if not ch:
                continue
            for r in [playing_role, lockdown_role, guild.default_role]:
                if r:
                    try:
                        await ch.set_permissions(r, overwrite=None)
                    except discord.HTTPException:
                        pass

            # Clear per-member overwrites where supported (graveyard/mafia access).
            try:
                if hasattr(ch, "overwrites"):
                    for target in list(ch.overwrites.keys()):
                        if isinstance(target, discord.Member):
                            try:
                                await ch.set_permissions(target, overwrite=None)
                            except discord.HTTPException:
                                pass
            except Exception:
                pass

        # Delete bot-created channels (best-effort, ignore failures).
        for ch_id in [self.mafia_tc_id, self.grave_tc_id, self.day_tc_id, self.day_vc_id, self.grave_vc_id]:
            ch = guild.get_channel(ch_id) if ch_id else None
            if ch:
                try:
                    await ch.delete(reason="Nuke reset: removing game infrastructure")
                except discord.HTTPException:
                    pass

        # Optionally delete lockdown role we created.
        if lockdown_role and lockdown_role.name == "Mafia - Lockdown":
            try:
                await lockdown_role.delete(reason="Nuke reset: removing lockdown role")
            except discord.HTTPException:
                pass

        from async_work import run_blocking
        await run_blocking(commit_and_maybe_delete_game_state, self.guild_id)

        self.in_progress = False
        self.phase = None
        self.day_number = 0
        self.game_key = None
        self.started_at = None
        self.players.clear()
        self.living_players.clear()
        self.player_slots.clear()
        self.player_roles.clear()
        self.role_states.clear()
        self.graveyard.clear()
        self.night.clear_persisted()
        self.tribunal_state.clear_persisted()
        self.votes_today = 0
        self.bloodless_cycle_streak = 0
        self.deaths_this_cycle = 0
        self.bloodless_stalemate_pending = False
        self.cleanup_pending = False
        self.ending = False
        self.game_channel_id = None
        self.mafia_tc_id = None
        self.day_tc_id = None
        self.day_vc_id = None
        self.grave_tc_id = None
        self.grave_vc_id = None
        self.alive_role_id = None
        self.stand_role_id = None
        self.locked_channel_ids = []
        self.lockdown_role_id = None
        active_games.pop(self.guild_id, None)

    def _action_target_summary(self, action: dict, *, actor_id: Optional[int] = None) -> Tuple[str, List[int], str]:
        a_type = str(action.get("type") or "")
        targets = action.get("targets")
        if isinstance(targets, list) and targets:
            ids = [int(t) for t in targets]
            parts = [tos_msg.format_player(self, tid) for tid in ids]
            return a_type, ids, " and ".join(parts)
        tid = action.get("target")
        if tid is not None:
            try:
                i = int(tid)
                eff_id = i
                if actor_id is not None and a_type in _VISIT_REDIRECT_ACK_TYPES:
                    from engine.night import effective_primary_target

                    eff = effective_primary_target(self, int(actor_id))
                    if eff is not None:
                        eff_id = int(eff)
                return a_type, [eff_id], tos_msg.format_player(self, eff_id)
            except (TypeError, ValueError):
                pass
        return a_type, [], ""

    async def ack_night_action(self, ctx: commands.Context, action: dict) -> None:
        a_type, _ids, targets_fmt = self._action_target_summary(action, actor_id=int(ctx.author.id))
        verb = ACTION_VERBS.get(a_type, a_type.replace("_", " "))
        st = self.role_states.setdefault(ctx.author.id, {})
        prev = st.get("last_action_summary")
        cur = {"type": a_type, "target_ids": _ids, "targets": action.get("targets"), "target": action.get("target")}
        if prev and prev.get("type") == a_type and prev.get("target_ids") == _ids:
            return
        if a_type == "chaos" and isinstance(action.get("targets"), list) and len(action["targets"]) == 2:
            t1, t2 = int(action["targets"][0]), int(action["targets"][1])
            pair = f"{tos_msg.format_player(self, t1)} and {tos_msg.format_player(self, t2)}"
            msg = (
                f"You will instead spread chaos between {pair} tonight."
                if prev
                else f"You have decided to spread chaos between {pair} tonight."
            )
        elif not _ids and a_type in ("vest", "bg_vest", "clean", "alert", "ignite"):
            msg = tos_msg.action_decided(verb, "") if not prev else tos_msg.action_instead(verb, "")
        elif not targets_fmt:
            return
        else:
            msg = tos_msg.action_decided(verb, targets_fmt) if not prev else tos_msg.action_instead(verb, targets_fmt)
            if a_type in _VISIT_REDIRECT_ACK_TYPES:
                msg = f"{msg} (A Transporter may redirect your visit tonight.)"
        st["last_action_summary"] = cur
        try:
            await ctx.send(msg)
        except discord.HTTPException:
            pass

    async def clear_night_action(self, ctx: commands.Context) -> None:
        existing = self.night_actions.get(ctx.author.id)
        if existing and existing.get("type") == "plunder" and not existing.get("duel_finished", False):
            try:
                await ctx.send(tos_msg.action_plunder_locked())
            except discord.HTTPException:
                pass
            return
        self.night_actions.pop(ctx.author.id, None)
        st = self.role_states.setdefault(ctx.author.id, {})
        st.pop("last_action_summary", None)
        try:
            await ctx.send(tos_msg.action_changed_mind())
        except discord.HTTPException:
            pass
        await self.persist_flush()

    def _night_ammo_line(self, role: str, state: dict) -> Optional[str]:
        if role == "Vigilante":
            n = int(state.get("shots_remaining", 0))
            return tos_msg.ammo_vigilante(n) if n > 0 else None
        if role == "Bodyguard":
            n = int(state.get("uses_remaining", 0))
            return tos_msg.ammo_bodyguard(n) if n > 0 else None
        if role == "Doctor":
            n = int(state.get("self_heals_remaining", 0))
            return tos_msg.ammo_doctor(n) if n > 0 else None
        if role == "Survivor":
            n = int(state.get("vests_remaining", 0))
            return tos_msg.ammo_survivor(n) if n > 0 else None
        if role == "Guardian Angel":
            n = int(state.get("ga_ward_charges", 0))
            return tos_msg.ammo_ga(n) if n > 0 else None
        if role == "Gatekeeper":
            n = int(state.get("uses_remaining", 0))
            return tos_msg.ammo_gatekeeper(n) if n > 0 else None
        if role == "Tailor":
            n = int(state.get("uses_remaining", 0))
            return tos_msg.ammo_tailor(n) if n > 0 else None
        if role == "Gravedigger":
            n = int(state.get("uses_remaining", 0))
            return tos_msg.ammo_gravedigger(n) if n > 0 else None
        if role == "Scary Grandma":
            n = int(state.get("alerts_remaining", 0))
            return tos_msg.ammo_grandma(n) if n > 0 else None
        if role == "Retributionist":
            n = int(state.get("uses_remaining", 0))
            return tos_msg.ammo_retri(n) if n > 0 else None
        if role == "Chaos":
            n = int(state.get("uses_remaining", 0))
            return tos_msg.ammo_chaos(n) if n > 0 else None
        return None

    async def _post_staged_death(self, guild: discord.Guild, member_id: int, cause: str, *, custom_line1: Optional[str] = None) -> None:
        pname = tos_msg.format_player(self, member_id)
        real_role = self.player_roles.get(member_id, "Unknown")
        revealed_role = self.role_states.get(member_id, {}).get("is_tailored_as", real_role)
        is_hidden = bool(self.role_states.get(member_id, {}).get("is_hidden_by_gravedigger", False))
        will_text = str(self.role_states.get(member_id, {}).get("will", "") or "").strip()
        tag = self.night_death_causes.pop(member_id, None) or cause
        st = self.role_states.setdefault(member_id, {})

        async def _advance(step_num: int, *lines: str, strip_mentions: bool = False) -> bool:
            cur = int(st.get("death_announce_step", 0) or 0)
            if cur >= step_num:
                return True
            if lines:
                kwargs = {}
                if strip_mentions:
                    kwargs["allowed_mentions"] = discord.AllowedMentions.none()
                if not await post_game_channel(self, guild, *lines, **kwargs):
                    return False
            st["death_announce_step"] = step_num
            await self.persist_flush()
            return True

        line1 = custom_line1 if custom_line1 else tos_msg.death_notice(cause, pname)
        if not await _advance(1, line1, strip_mentions=bool(custom_line1)):
            return

        cl = tos_msg.death_cause_line(tag)
        if int(st.get("death_announce_step", 0) or 0) < 2:
            if cl and not await post_game_channel(self, guild, cl):
                return
            st["death_announce_step"] = 2
            await self.persist_flush()

        if int(st.get("death_announce_step", 0) or 0) < 3:
            if cause == "sk_counter_attack" or tag == "sk_counter_attack":
                if not await post_game_channel(self, guild, tos_msg.will_unreadable_blood()):
                    return
            elif will_text:
                if not await post_game_channel(self, guild, tos_msg.will_found()):
                    return
            else:
                if not await post_game_channel(self, guild, tos_msg.will_not_found()):
                    return
            st["death_announce_step"] = 3
            await self.persist_flush()

        if int(st.get("death_announce_step", 0) or 0) < 4:
            if is_hidden:
                if not await post_game_channel(self, guild, tos_msg.role_unknown(pname)):
                    return
            else:
                if not await post_game_channel(self, guild, tos_msg.role_was(pname, revealed_role)):
                    return
            st["death_announce_step"] = 4
            await self.persist_flush()

        if (
            int(st.get("death_announce_step", 0) or 0) < 5
            and will_text
            and cause != "sk_counter_attack"
            and tag != "sk_counter_attack"
        ):
            safe = will_text.replace("```", "'''").strip()[:1800]
            if not await post_game_channel(self, guild, f"```{safe}```"):
                return
            st["death_announce_step"] = 5
            await self.persist_flush()

        st.pop("death_announce_step", None)

    async def resume_pending_death_announces(self, guild: discord.Guild) -> None:
        """CR18 — finish staged death posts interrupted by crash/restart."""
        if not self.in_progress or self.ending:
            return
        for entry in list(self.graveyard):
            raw_pid = entry.get("player_id")
            if raw_pid is None:
                continue
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                continue
            st = self.role_states.get(pid, {}) or {}
            if not st.get("death_announce_step"):
                continue
            cause = str(entry.get("cause") or self.night_death_causes.get(pid) or "unknown")
            await self._post_staged_death(guild, pid, cause)

    async def set_night_action(self, ctx, action):
        # Compatibility entry point for extensions. Core commands use actions.submit.
        from gameplay.actions import submit
        kind = 'protect' if action.get('type') == 'bg_vest' else action.get('type')
        targets = action.get('targets', [action['target']] if action.get('target') else [])
        result = await submit(self, ctx.author.id, kind, targets, guild=ctx.guild,
            corpse_id=action.get('corpse_player_id'), message_type=action.get('msg_type'), fake_role=action.get('fake_role'))
        await ctx.send(result.message, allowed_mentions=discord.AllowedMentions.none())
        return result.accepted

    def record_cycle_death(self, count: int = 1) -> None:
        """Track deaths in the current day+night cycle (lynch or night kill)."""
        if not self.in_progress or self.ending:
            return
        self.deaths_this_cycle += max(0, int(count))

    async def _close_bloodless_cycle_and_maybe_draw(self) -> bool:
        """Finalize the completed day+night cycle; trigger Draw if streak is high enough."""
        from config import STALEMATE_DRAW_CYCLES

        if self.deaths_this_cycle <= 0:
            self.bloodless_cycle_streak += 1
        else:
            self.bloodless_cycle_streak = 0
        self.deaths_this_cycle = 0
        if self.bloodless_cycle_streak < STALEMATE_DRAW_CYCLES:
            return False
        self.bloodless_stalemate_pending = True
        return await self.check_win_conditions()

    async def _process_deferred_guilt_at_night_start(self, ctx) -> None:
        """ToS following-night guilt: shooters with ``guilty_tomorrow`` die when night begins."""
        await self.sync_living_players(ctx.guild)
        living_ids = {int(x) for x in await self.get_living_ids(ctx.guild)}
        guilt_ids = sorted(
            (
                int(pid)
                for pid, st in self.role_states.items()
                if isinstance(st, dict)
                and st.get("guilty_tomorrow")
                and int(pid) in living_ids
            ),
            key=lambda pid: self.player_slots.get(pid, 9999),
        )
        if not guilt_ids:
            return
        for pid in guilt_ids:
            st = self.role_states.setdefault(pid, {})
            st["will_die_of_guilt"] = True
            st.pop("guilty_tomorrow", None)
            member = await self.get_member_safe(ctx.guild, pid)
            custom = tos_msg.guilt_suicide_line(
                member.display_name if member else f"<@{pid}>"
            )
            if member:
                await self.process_death(ctx, member, "guilt", custom_message=custom)
            else:
                await self.process_death_by_id(ctx, ctx.guild, pid, "guilt", custom_message=custom)
            self.record_cycle_death(1)
        await self.check_win_conditions()
        await self.persist_flush()

    async def _historical_start_night(self, ctx) -> None:
        if not self.in_progress:
            return
        if self.phase == "day":
            if await self._close_bloodless_cycle_and_maybe_draw():
                return
        elif self.phase != "night":
            self.deaths_this_cycle = 0
        if self.phase == "night":
            try:
                await ctx.send("It is already **night** — players can submit actions, or run `!resolve` when ready.")
            except discord.HTTPException:
                pass
            return

        await self._process_deferred_guilt_at_night_start(ctx)

        self.phase = "night"
        self.night_actions = {}
        self.night_transport_swaps = []
        self._transport_pairs_seen = set()
        self.night_transport_dm_pairs = set()
        self._effective_visit_destinations_cache = None
        self.night_completion_snapshot = None
        self.psychic_visions_delivered_this_night = False

        from per_night_state import all_keys_cleared_at_start_night

        for state in self.role_states.values():
            if not isinstance(state, dict):
                continue
            for key in all_keys_cleared_at_start_night():
                state.pop(key, None)
        # Persist after clearing one-night flags so restarts don't leak prior-night state.
        await self.persist_flush()

        day_vc = ctx.guild.get_channel(self.day_vc_id) if self.day_vc_id else None
        alive_role = ctx.guild.get_role(self.alive_role_id) if self.alive_role_id else None
        if day_vc and alive_role:
            try:
                await day_vc.set_permissions(alive_role, speak=False)
            except discord.HTTPException:
                pass

        await post_game_channel(self, ctx.guild, tos_msg.night_header(self.day_number), tos_msg.night_submit_hint())

        for st in self.role_states.values():
            if isinstance(st, dict):
                st.pop("last_action_summary", None)

        await self.sync_living_players(ctx.guild)
        living_ids = await self.get_living_ids(ctx.guild)
        player_list_text = "\n".join(
            [f"{self.player_slots.get(p.id, '?')}: {p.display_name}" for p in self.ordered_living_players()]
        )

        for p_id, role in self.player_roles.items():
            if p_id not in living_ids:
                continue
            player = await self.get_member_safe(ctx.guild, p_id)
            if not player:
                continue
            action_text = self._get_night_prompt(player, role)
            if action_text:
                ammo = self._night_ammo_line(role, self.role_states.get(p_id, {}) or {})
                night_body = f"**Living Players:**\n{player_list_text}\n\n{action_text}"
                if ammo:
                    night_body += f"\n\n{ammo}"
                try:
                    await player.send(night_body)
                except discord.HTTPException:
                    pass
                await send_to_player_private_channel(
                    ctx.guild,
                    p_id,
                    f"🌙 **Night {self.day_number}** — {player.mention}\n\n{night_body}",
                    log_context="night prompt private channel",
                )

    def deputy_fired_today(self, deputy_id: int) -> bool:
        """True if this Deputy already fired their revolver today (per-player daily lock)."""
        st = self.role_states.get(int(deputy_id), {}) or {}
        try:
            return int(st.get("deputy_fired_day", 0)) == int(self.day_number)
        except (TypeError, ValueError):
            return False

    def mark_deputy_shot_today(self, deputy_id: int) -> None:
        """Consume this Deputy's bullet and record the day they fired."""
        st = self.role_states.setdefault(int(deputy_id), {})
        st["deputy_fired_day"] = int(self.day_number)
        st["deputy_shots_remaining"] = 0

    async def grant_mafia_channel_access(
        self, guild: discord.Guild, member: discord.Member
    ) -> None:
        """Ensure a mafia-role player can view/send in the private mafia channel."""
        if self.player_roles.get(member.id) not in ALL_MAFIA_ROLES:
            return
        mafia_tc = guild.get_channel(self.mafia_tc_id) if self.mafia_tc_id else None
        if not mafia_tc:
            return
        try:
            await mafia_tc.set_permissions(member, view_channel=True, send_messages=True)
        except discord.HTTPException:
            logging.warning(
                "grant_mafia_channel_access failed guild_id=%s member_id=%s",
                self.guild_id,
                member.id,
                exc_info=True,
            )

    async def apply_first_day_discord_setup(self, guild: discord.Guild) -> None:
        """Day 1 VC permissions (game start) without incrementing ``day_number``."""
        day_vc = guild.get_channel(self.day_vc_id) if self.day_vc_id else None
        alive_role = guild.get_role(self.alive_role_id) if self.alive_role_id else None
        if day_vc and alive_role:
            try:
                await day_vc.set_permissions(alive_role, connect=True, speak=True)
            except discord.HTTPException:
                pass

    async def _historical_start_day(self, ctx) -> None:
        if not self.in_progress:
            return
        # Idempotency: if we're already in day phase, do not re-run day-start side effects.
        if self.phase == "day":
            return
        self.phase = "day"
        self.day_number += 1
        self.votes_today = 0
        self.vote_in_progress = False
        await self.persist_flush()

        await self.sync_living_players(ctx.guild)

        # Guardian Angel public protection line (pending from last resolve).
        ch = ctx.guild.get_channel(self.game_channel_id) if self.game_channel_id else None
        if isinstance(ch, discord.TextChannel):
            for pid, role in self.player_roles.items():
                if role != "Guardian Angel":
                    continue
                st = self.role_states.get(pid, {}) or {}
                if not st.get("ga_announce_pending"):
                    continue
                bind_id = st.get("ga_target_id")
                try:
                    bid = int(bind_id) if bind_id is not None else None
                except (TypeError, ValueError):
                    bid = None
                bind = await self.get_member_safe(ctx.guild, bid) if bid is not None else None
                if bind:
                    await post_game_channel(
                        self, ctx.guild, tos_msg.ga_protected(tos_msg.format_player(self, bind.id))
                    )
                st["ga_announce_pending"] = False

        for st in self.role_states.values():
            if isinstance(st, dict):
                st.pop("last_action_summary", None)

        day_vc = ctx.guild.get_channel(self.day_vc_id) if self.day_vc_id else None
        alive_role = ctx.guild.get_role(self.alive_role_id) if self.alive_role_id else None
        if day_vc and alive_role:
            try:
                await day_vc.set_permissions(alive_role, connect=True, speak=True)
            except discord.HTTPException:
                pass
        await post_game_channel(
            self,
            ctx.guild,
            tos_msg.day_header(self.day_number),
            tos_msg.living_count(len(self.living_players)),
        )

        living_ids = await self.get_living_ids(ctx.guild)
        if self.day_number >= 2:
            for p_id, role in self.player_roles.items():
                if role != "Deputy" or p_id not in living_ids:
                    continue
                st = self.role_states.get(p_id, {}) or {}
                if int(st.get("deputy_shots_remaining", 0)) <= 0:
                    continue
                if self.deputy_fired_today(p_id):
                    continue
                dep = await self.get_member_safe(ctx.guild, p_id)
                if not dep:
                    continue
                deputy_msg = tos_msg.deputy_day_prompt(self.day_number)
                try:
                    await dep.send(deputy_msg)
                except discord.HTTPException:
                    pass
                await send_to_player_private_channel(
                    ctx.guild,
                    p_id,
                    f"{dep.mention} {deputy_msg}",
                    log_context="deputy day prompt private channel",
                )

    async def _announce_draw_override_winner(
        self,
        guild: Optional[discord.Guild],
        pid: int,
        role: str,
        win_post,
    ) -> None:
        member = await self.get_member_safe(guild, pid) if guild is not None else None
        mention = member.mention if member else None
        if role == "Survivor":
            line = tos_msg.win_survivor_named(mention) if mention else tos_msg.win_survivor_anon()
        elif role == "Chaos":
            line = tos_msg.win_chaos_named(mention) if mention else tos_msg.win_chaos_anon()
        elif role == "Jester":
            line = tos_msg.win_jester_named(mention) if mention else tos_msg.win_jester_anon()
        elif role == "Executioner":
            line = tos_msg.win_executioner_named(mention) if mention else tos_msg.win_executioner_anon()
        elif role == "Pirate":
            line = tos_msg.win_pirate_named(mention) if mention else tos_msg.win_pirate_anon()
        elif role == "Guardian Angel":
            line = tos_msg.win_guardian_angel_named(mention) if mention else tos_msg.win_guardian_angel_anon()
        else:
            return
        await win_post(line)

    async def _finish_stalemate_draw_or_override(
        self,
        *,
        living_ids: List[int],
        bloodless: bool,
        win_post,
        finish_endgame,
        guild: Optional[discord.Guild],
    ) -> None:
        from config import STALEMATE_DRAW_CYCLES

        winners = collect_draw_override_winners(self.player_roles, self.role_states, living_ids)
        self.ending = True
        if winners:
            for winner_id, role in winners:
                await self._announce_draw_override_winner(guild, winner_id, role, win_post)
            await finish_endgame(outcome=resolve_draw_override_outcome(winners))
            return

        if bloodless:
            await win_post(tos_msg.win_draw_bloodless_stalemate(STALEMATE_DRAW_CYCLES))
        else:
            await win_post(tos_msg.win_draw_all_dead())
        await finish_endgame(outcome="Draw")

    async def check_win_conditions(self) -> bool:
        # Many commands can trigger end checks (resolve, lynch, slay, etc.).
        # Make the win-check + reset sequence mutually exclusive.
        async with self._endgame_lock:
            if not self.in_progress:
                return False
            if self.ending:
                # Wedged crash snapshot: ending+in_progress — retry full endgame path.
                if self.in_progress and not self.cleanup_pending:
                    logging.warning(
                        "check_win: recovering wedged ending+in_progress guild_id=%s",
                        self.guild_id,
                    )
                    self.ending = False
                else:
                    return True

            self._check_win_active = True
            try:
                return await self._check_win_conditions_body()
            finally:
                self._check_win_active = False

    async def _check_win_conditions_body(self) -> bool:
            bot = try_get_bot()
            if bot is None:
                logging.error(
                    "check_win_conditions: bot not bound guild_id=%s",
                    self.guild_id,
                )
                return False
            guild = await resolve_game_guild(bot, self.guild_id)
            game_channel: Optional[discord.TextChannel] = None
            if guild is not None:
                game_channel = bot.get_channel(self.game_channel_id) if self.game_channel_id else None
                if not isinstance(game_channel, discord.TextChannel):
                    if self.day_tc_id:
                        day_tc = guild.get_channel(self.day_tc_id)
                        if isinstance(day_tc, discord.TextChannel):
                            me2 = guild.me
                            if not me2 and bot.user:
                                me2 = guild.get_member(bot.user.id)
                            if me2 and day_tc.permissions_for(me2).send_messages and day_tc.permissions_for(me2).view_channel:
                                game_channel = day_tc
                    if not game_channel:
                        me = guild.me
                        if not me and bot.user:
                            me = guild.get_member(bot.user.id)
                        if me:
                            game_channel = guild.system_channel or next(
                                (c for c in guild.text_channels if c.permissions_for(me).send_messages),
                                None,
                            )
                        else:
                            game_channel = guild.system_channel
                # HP01 — never abort win processing when no postable channel (CR20: do not persist fallback id).
                if not game_channel:
                    logging.warning(
                        "check_win_conditions: no announce channel; continuing without public posts guild_id=%s",
                        self.guild_id,
                    )
                await self.sync_living_players(guild)
                living_ids = await self.get_living_ids(guild)
            else:
                logging.warning(
                    "check_win_conditions: guild unavailable; using cached living guild_id=%s",
                    self.guild_id,
                )
                living_ids = [int(p.id) for p in self.living_players]

            async def _win_post(*lines: str) -> bool:
                if guild is None or not game_channel:
                    return False
                return await post_game_channel(self, guild, *lines)

            async def _finish_endgame(*, outcome: str) -> None:
                nonlocal guild
                await self.commit_endgame_stats_async(outcome=outcome, living_ids=living_ids)
                if bool(getattr(self, "refresh_stats_board", False)):
                    from bot_app.stats_board import schedule_stats_board_refresh

                    schedule_stats_board_refresh(bot=bot, guild_id=int(self.guild_id))
                if not bool(getattr(self, "stats_committed", False)):
                    logging.error(
                        "Endgame stats not committed; deferring infrastructure reset "
                        "guild_id=%s game_key=%s — retry when SQLite is available or run !reset after fix",
                        self.guild_id,
                        getattr(self, "game_key", None),
                    )
                    from async_work import run_blocking
                    await run_blocking(self._enqueue_game_over_dms)
                    self.in_progress = False
                    self.ending = True
                    self.cleanup_pending = True
                    self.vote_in_progress = False
                    self.tribunal_state.clear_persisted()
                    try:
                        await self.persist_flush()
                    except Exception:
                        logging.exception(
                            "persist_flush failed after deferred endgame reset guild_id=%s",
                            self.guild_id,
                        )
                    return
                reset_guild = guild
                if reset_guild is None:
                    reset_guild = await resolve_game_guild(bot, self.guild_id)
                    if reset_guild is not None:
                        guild = reset_guild
                if reset_guild is not None:
                    await self.reset(reset_guild, _from_check_win=True)
                else:
                    self.in_progress = False
                    self.ending = True
                    self.cleanup_pending = True
                    try:
                        await self.persist_flush()
                    except Exception:
                        logging.exception(
                            "persist_flush failed after deferred reset guild_id=%s",
                            self.guild_id,
                        )
                    logging.error(
                        "check_win_conditions: stats committed but infrastructure reset deferred "
                        "(guild unavailable) guild_id=%s — GM should run cleanup when bot recovers",
                        self.guild_id,
                    )

            async def _announce_personal_wins() -> None:
                """Public personal-win lines at endgame (DM victory sent earlier for Pirate/Exe/Jester)."""

                async def _announce_one(*, role: str, condition, named_fn, anon_fn) -> None:
                    winner_id = next(
                        (
                            p_id
                            for p_id, r in self.player_roles.items()
                            if r == role and condition(p_id) and not self.role_states.get(p_id, {}).get("win_announced")
                        ),
                        None,
                    )
                    if winner_id is None:
                        return
                    winner = await self.get_member_safe(guild, winner_id)
                    if winner:
                        await _win_post(named_fn(winner.mention))
                    else:
                        await _win_post(anon_fn())
                    self.role_states.setdefault(winner_id, {})["win_announced"] = True

                await _announce_one(
                    role="Pirate",
                    condition=lambda pid: int(self.role_states.get(pid, {}).get("wins", 0)) >= 2,
                    named_fn=tos_msg.win_pirate_named,
                    anon_fn=tos_msg.win_pirate_anon,
                )
                await _announce_one(
                    role="Executioner",
                    condition=lambda pid: bool(self.role_states.get(pid, {}).get("exe_won")),
                    named_fn=tos_msg.win_executioner_named,
                    anon_fn=tos_msg.win_executioner_anon,
                )
                await _announce_one(
                    role="Jester",
                    condition=lambda pid: bool(self.role_states.get(pid, {}).get("jester_won")),
                    named_fn=tos_msg.win_jester_named,
                    anon_fn=tos_msg.win_jester_anon,
                )

            async def _announce_survivor_style(*, winning_faction: Optional[str]) -> None:
                """Congratulate neutral personal wins at endgame consistently across win paths."""
                for p_id in sorted(living_ids):
                    if self.player_roles.get(p_id) != "Survivor":
                        continue
                    survivor = await self.get_member_safe(guild, p_id)
                    if survivor:
                        await _win_post(tos_msg.win_survivor_named(survivor.mention))

                if winning_faction in WITCH_TOWN_LOSES_OUTCOMES:
                    for p_id in sorted(living_ids):
                        if self.player_roles.get(p_id) != "Witch":
                            continue
                        witch = await self.get_member_safe(guild, p_id)
                        if witch:
                            await _win_post(tos_msg.win_witch_named(witch.mention))

                for p_id in sorted(living_ids):
                    if self.player_roles.get(p_id) != "Chaos":
                        continue
                    chaos = await self.get_member_safe(guild, p_id)
                    if chaos:
                        await _win_post(tos_msg.win_chaos_named(chaos.mention))

                living_set = set(living_ids)
                announce_flags = build_outcome_flags_for_game(
                    self.player_roles,
                    self.role_states,
                    living_set,
                    str(winning_faction or ""),
                )
                for ga_id, ga_role in self.player_roles.items():
                    if ga_role != "Guardian Angel":
                        continue
                    ga_st = self.role_states.get(ga_id, {}) or {}
                    bind_raw = ga_st.get("ga_target_id")
                    try:
                        bind_id = int(bind_raw) if bind_raw is not None else None
                    except (TypeError, ValueError):
                        bind_id = None
                    bind_role_ann = self.player_roles.get(bind_id) if bind_id is not None else None
                    ga_joint = guardian_angel_joint_win(
                        ga_alive=int(ga_id) in living_set,
                        ga_defeated=bool(ga_st.get("ga_defeated")),
                        bind_id=bind_id,
                        bind_role=bind_role_ann,
                        living_ids=living_set,
                        outcome_flags=announce_flags,
                        bind_role_state=self.role_states.get(bind_id, {}) if bind_id is not None else None,
                        bind_pirate_wins=int(self.role_states.get(bind_id, {}).get("wins", 0))
                        if bind_id is not None and bind_role_ann == "Pirate"
                        else None,
                        bind_exe_won=bool(self.role_states.get(bind_id, {}).get("exe_won"))
                        if bind_id is not None and bind_role_ann == "Executioner"
                        else None,
                        bind_jester_won=bool(self.role_states.get(bind_id, {}).get("jester_won"))
                        if bind_id is not None and bind_role_ann == "Jester"
                        else None,
                    )
                    if not guardian_angel_personal_win(
                        role="Guardian Angel",
                        player_id=int(ga_id),
                        ga_alive=int(ga_id) in living_set,
                        ga_defeated=bool(ga_st.get("ga_defeated")),
                        ga_joint_win=ga_joint,
                        stalemate_override=False,
                        override_winner_ids=frozenset(),
                    ):
                        continue
                    ga_member = await self.get_member_safe(guild, ga_id)
                    if ga_member:
                        await _win_post(tos_msg.win_guardian_angel_named(ga_member.mention))
                    else:
                        await _win_post(tos_msg.win_guardian_angel_anon())
                    break

            # Personal wins should announce even if the match ends immediately after (e.g., Draw).
            await _announce_personal_wins()

            if getattr(self, "bloodless_stalemate_pending", False):
                self.bloodless_stalemate_pending = False
                await self._finish_stalemate_draw_or_override(
                    living_ids=living_ids,
                    bloodless=True,
                    win_post=_win_post,
                    finish_endgame=_finish_endgame,
                    guild=guild,
                )
                return True

            if not living_ids:
                await self._finish_stalemate_draw_or_override(
                    living_ids=living_ids,
                    bloodless=False,
                    win_post=_win_post,
                    finish_endgame=_finish_endgame,
                    guild=guild,
                )
                return True

            mafia_ids = [p_id for p_id in living_ids if self.player_roles.get(p_id) in ALL_MAFIA_ROLES]

            # Mobster Promotion Logic
            if mafia_ids and not any(self.player_roles.get(p_id) == "Mobster" for p_id in mafia_ids):
                new_mobster_id = random.choice(mafia_ids)
                self.player_roles[new_mobster_id] = "Mobster"
                try:
                    await self.persist_flush()
                except Exception:
                    logging.exception("persist_flush failed after Mobster promotion guild_id=%s", self.guild_id)
                if guild is not None:
                    new_mobster = await self.get_member_safe(guild, new_mobster_id)
                    if new_mobster:
                        await self.grant_mafia_channel_access(guild, new_mobster)
                        try:
                            await new_mobster.send(
                                "🔪 **You have been promoted to Mobster.** The syndicate needs you. You can now use `!kill` or `/kill`."
                            )
                        except discord.HTTPException:
                            pass

            # Arsonist Win Condition
            arsonist_winner_id = next(
                (p_id for p_id in living_ids if self.player_roles.get(p_id) == "Arsonist" and len(living_ids) == 1),
                None,
            )
            if arsonist_winner_id is not None:
                self.ending = True
                arsonist = await self.get_member_safe(guild, arsonist_winner_id)
                if arsonist:
                    await _win_post(tos_msg.win_arsonist_named(arsonist.mention))
                else:
                    await _win_post(tos_msg.win_arsonist_anon())
                await _announce_survivor_style(winning_faction="Arsonist")
                await _finish_endgame(outcome="Arsonist")
                return True

            # Serial Killer solo win (last living killer).
            sk_winner_id = next(
                (p_id for p_id in living_ids if self.player_roles.get(p_id) == "Serial Killer" and len(living_ids) == 1),
                None,
            )
            if sk_winner_id is not None:
                self.ending = True
                sk = await self.get_member_safe(guild, sk_winner_id)
                if sk:
                    await _win_post(tos_msg.win_serial_killer_named(sk.mention))
                else:
                    await _win_post(tos_msg.win_serial_killer_anon())
                await _announce_survivor_style(winning_faction="Serial Killer")
                await _finish_endgame(outcome="Serial Killer")
                return True

            # ToS1 two-player stalemate table (Escort/Mobster/SK/Transporter/Arsonist).
            if len(living_ids) == 2:
                from stalemate_wins import lookup_two_player_stalemate

                r1 = self.player_roles.get(living_ids[0], "")
                r2 = self.player_roles.get(living_ids[1], "")
                stalemate = lookup_two_player_stalemate(r1, r2)
                if stalemate.applies:
                    if stalemate.winner is None:
                        return False
                    outcome = stalemate.winner
                    self.ending = True
                    if outcome == "Arsonist":
                        arsonist_id = next(
                            p_id for p_id in living_ids if self.player_roles.get(p_id) == "Arsonist"
                        )
                        arsonist = await self.get_member_safe(guild, arsonist_id)
                        if arsonist:
                            await _win_post(tos_msg.win_arsonist_named(arsonist.mention))
                        else:
                            await _win_post(tos_msg.win_arsonist_anon())
                        await _announce_survivor_style(winning_faction="Arsonist")
                    elif outcome == "Serial Killer":
                        sk_id = next(
                            p_id for p_id in living_ids if self.player_roles.get(p_id) == "Serial Killer"
                        )
                        sk = await self.get_member_safe(guild, sk_id)
                        if sk:
                            await _win_post(tos_msg.win_serial_killer_named(sk.mention))
                        else:
                            await _win_post(tos_msg.win_serial_killer_anon())
                        await _announce_survivor_style(winning_faction="Serial Killer")
                    else:
                        await _win_post(tos_msg.win_faction(outcome))
                        await _announce_survivor_style(winning_faction=outcome)
                    await _finish_endgame(outcome=outcome)
                    return True

            # Arsonist stalemate breaker: if only the Arsonist + non-killing neutrals remain, end the game.
            from faction_win_logic import arsonist_harmless_neutral_stalemate

            living_role_list = [self.player_roles.get(p_id, "") for p_id in living_ids]
            arsonist_id = next((p_id for p_id in living_ids if self.player_roles.get(p_id) == "Arsonist"), None)
            if arsonist_id is not None and arsonist_harmless_neutral_stalemate(living_role_list):
                self.ending = True
                arsonist = await self.get_member_safe(guild, arsonist_id)
                if arsonist:
                    await _win_post(tos_msg.win_arsonist_named(arsonist.mention))
                else:
                    await _win_post(tos_msg.win_arsonist_anon())
                await _announce_survivor_style(winning_faction="Arsonist")
                await _finish_endgame(outcome="Arsonist")
                return True

            # Faction Win Conditions
            from faction_win_logic import living_roles_faction_winner

            winning_faction = living_roles_faction_winner(
                self.player_roles.get(p_id, "") for p_id in living_ids
            )

            if not winning_faction:
                return False

            self.ending = True
            await _win_post(tos_msg.win_faction(winning_faction))
            await _announce_survivor_style(winning_faction=winning_faction)
            await _finish_endgame(outcome=winning_faction)
            return True

    async def process_death(
        self, ctx_or_channel, member: discord.Member, cause: str,
        voters: Optional[List[discord.Member]] = None, custom_message: Optional[str] = None,
    ) -> None:
        from gameplay.death import apply_death
        from gameplay.state import commit, Rejected
        await self.sync_living_players(member.guild)
        def update():
            if active_games.get(self.guild_id) not in (None, self):
                raise Rejected("This game has been replaced.")
            if self.resolving or self.ending:
                raise Rejected("The game is resolving or ending. Please wait.")
            return apply_death(self, member.id, cause, voters=[v.id for v in voters or []],
                               custom_message=custom_message)
        receipt = await commit(self, update, persist=self.in_progress and not self.ending)
        if receipt and not receipt.get("delivered"):
            await self.deliver_death_receipt(ctx_or_channel, member.guild, receipt, member)

    async def _apply_graveyard_discord_perms(
        self, guild: discord.Guild, member: discord.Member
    ) -> None:
        get_ch = getattr(guild, "get_channel", None)
        if not callable(get_ch):
            return
        graveyard_tc = get_ch(self.grave_tc_id)
        graveyard_vc = get_ch(self.grave_vc_id)
        if graveyard_tc:
            try:
                await graveyard_tc.set_permissions(member, view_channel=True, send_messages=True)
            except discord.HTTPException:
                pass
        if graveyard_vc:
            try:
                await graveyard_vc.set_permissions(member, view_channel=True, connect=True, speak=True)
            except discord.HTTPException:
                pass
        if graveyard_vc and member.voice and member.voice.channel:
            try:
                await member.move_to(graveyard_vc)
            except discord.HTTPException:
                pass
        mafia_tc = get_ch(self.mafia_tc_id)
        if mafia_tc:
            try:
                await mafia_tc.set_permissions(member, overwrite=None)
            except discord.HTTPException:
                pass

    async def process_death_by_id(
        self, ctx_or_channel, guild: discord.Guild, player_id: int, cause: str,
        *, custom_message: Optional[str] = None,
    ) -> None:
        from gameplay.death import apply_death
        from gameplay.state import commit, Rejected
        await self.sync_living_players(guild)
        def update():
            if active_games.get(self.guild_id) not in (None, self):
                raise Rejected("This game has been replaced.")
            if self.resolving or self.ending:
                raise Rejected("The game is resolving or ending. Please wait.")
            return apply_death(self, player_id, cause, custom_message=custom_message)
        receipt = await commit(self, update, persist=self.in_progress and not self.ending)
        if receipt and not receipt.get("delivered"):
            await self.deliver_death_receipt(ctx_or_channel, guild, receipt)

    def _get_night_prompt(self, player: discord.Member, role: str) -> Optional[str]:
        state = self.role_states.get(player.id, {})
        if role == "Mobster":
            return "Use `!kill <number>`."
        if role == "Serial Killer":
            return "Use `!stab <number>` to attack. Toggle counter-attacks with `!cautious`."
        if role == "Guardian Angel":
            return "Use `!ward <number>` on your bound slot only (check your game-start DM)."
        if role == "Psychic":
            return "You are passive — you will receive automatic visions after each night resolves."
        if role == "Seer":
            return "Use `!gaze <number1> <number2>` to compare two living players."
        if role == "Deputy":
            return (
                "Starting Day 2, you may fire **once per day** until your bullet is spent "
                "(`!shoot <slot>` from your private channel or DMs)."
            )
        if role == "Doctor":
            return "Use `!heal <number>`."
        if role in ["Escort", "Consort"]:
            return "Use `!roleblock <number>`."
        if role in ["Sheriff", "Investigator"]:
            return "Use `!investigate <number>`."
        if role == "Lookout":
            return "Use `!watch <number>`."
        if role == "Tracker":
            return "Use `!track <number>`."
        if role == "Transporter":
            return "Use `!transport <number1> <number2>`."
        if role == "Witch":
            return "Use `!control <number1> <number2>`."
        if role == "Arsonist":
            return (
                "Use `!douse <number>` to douse a player, `!doused` to list everyone doused, "
                "`!ignite` to ignite, or `!clean` to remove gasoline from yourself."
            )
        if role == "Hypnotist":
            return (
                "Who do you want to confuse? Use `!hypnotize <number> <type>`.\n"
                "Valid types: `healed`, `roleblocked`, `transported`, `controlled`, `attacked`"
            )
        if role == "Pirate":
            return "Use `!plunder <number>`."
        if role == "Retributionist" and state.get("uses_remaining", 0) > 0:
            return (
                f"You have {state['uses_remaining']} use(s) left. Use `!corpses` to list usable corpses, then "
                "`!reanimate <corpse> <slot>`."
            )
        if role == "Chaos" and state.get("uses_remaining", 0) > 0:
            return f"You have {state['uses_remaining']} use(s) left. Use `!chaos <number1> <number2>`."

        # Note: this bot increments day_number in start_day(), so Night N corresponds to day_number == N.
        if role == "Framer" and self.day_number <= 2:
            return "Use `!frame <number>`."
        if role == "Gatekeeper" and state.get("uses_remaining", 0) > 0:
            return f"You have {state['uses_remaining']} use(s) left. Use `!guard <number>`."
        if role == "Gravedigger" and state.get("uses_remaining", 0) > 0:
            return f"You have {state['uses_remaining']} use(s) left. Use `!hide <number>`."
        if role == "Vigilante" and state.get("shots_remaining", 0) > 0:
            return "You have 1 bullet left. Use `!shoot <number>`."
        if role == "Survivor" and state.get("vests_remaining", 0) > 0:
            return f"You have {state['vests_remaining']} vest(s) left. Use `!vest`."
        if role == "Scary Grandma" and state.get("alerts_remaining", 0) > 0:
            return f"You have {state['alerts_remaining']} alert(s) left. Use `!alert`."
        if role == "Mole" and state.get("uses_remaining", 0) > 0:
            return "You have 1 use left. Use `!investigate <number>`."
        if role == "Tailor" and state.get("uses_remaining", 0) > 0:
            return "You have 1 use left. Use `!tailor <number> <fake_role>`."
        if role == "Bodyguard" and (state.get("uses_remaining", 0) > 0 or state.get("self_protects_remaining", 0) > 0):
            return "Use `!protect <number>`."

        return None

    # --- NIGHT RESOLUTION PIPELINE (delegates) ---
    async def _resolve_transports(self, guild: discord.Guild) -> None:
        await night_engine.resolve_transports(self, guild)

    async def _resolve_control(self, guild: discord.Guild) -> None:
        await night_engine.resolve_control(self, guild)

    def _build_visit_log(self) -> Dict[int, List[int]]:
        return night_engine.build_visit_log(self)

    def _resolve_blocking(self, visit_log: Dict[int, List[int]]) -> List[int]:
        return night_engine.resolve_blocking(self, visit_log)

    async def _apply_misc_actions(
        self, blocked: List[int], guild: discord.Guild
    ) -> Tuple[Dict[int, int], Dict[int, List[Dict[str, object]]]]:
        return await night_engine.apply_misc_actions(self, blocked, guild)

    async def _resolve_investigative(self, blocked: List[int], visit_log: Dict[int, List[int]], guild: discord.Guild) -> None:
        await night_engine.resolve_investigative(self, blocked, visit_log, guild)

    async def _resolve_killing(
        self,
        visit_log: Dict[int, List[int]],
        blocked: List[int],
        healed_by_map: Dict[int, int],
        protected_by_map: Dict[int, List[Dict[str, object]]],
        guild: discord.Guild,
    ) -> Set[int]:
        return await night_engine.resolve_killing(self, visit_log, blocked, healed_by_map, protected_by_map, guild)

    async def _send_night_feedback(
        self,
        blocked: List[int],
        guild: discord.Guild,
        *,
        deaths: Optional[Set[int]] = None,
        healed_by_map: Optional[Dict[int, int]] = None,
    ) -> None:
        await night_engine.send_night_feedback(
            self, blocked, guild, deaths=deaths, healed_by_map=healed_by_map
        )



    async def deliver_death_receipt(self, channel, guild, receipt, member=None) -> None:
        uid = str(receipt['player_id'])
        from gameplay.lifecycle import message_lock
        try:
            async with self.delivery_locks.setdefault(uid, asyncio.Lock()):
                async with message_lock(self.guild_id):
                    current = self.gameplay['deaths'].get(uid)
                    if current and not self.ending and active_games.get(self.guild_id) in (None, self):
                        await self._deliver_death_receipt(channel, guild, current, member)
        finally:
            controller = getattr(try_get_bot(), 'gameplay_controller', None)
            if controller and active_games.get(self.guild_id) is self and self.in_progress and not self.ending:
                controller.retry_death(self, int(uid))

    async def _deliver_death_receipt(self, channel, guild, receipt, member=None) -> None:
        from async_work import finish_pending
        from gameplay.death import death_text
        from gameplay.state import commit, Rejected
        if receipt.get('delivered'):
            return
        if getattr(guild, '_monte_carlo_fake', False):
            receipt['delivered'] = True
            return
        match = self.game_key
        def current():
            if self.game_key != match or self.ending or active_games.get(self.guild_id) not in (None, self):
                raise Rejected('This game has ended.')
        def checkpoint(**changes):
            current()
            self.gameplay['deaths'][str(receipt['player_id'])].update(changes)
        member = member or await self.lookup_member(guild, receipt['player_id'])
        member = member or next((p for p in self.players if p.id == receipt['player_id'] and isinstance(p, discord.Member)), None)
        current()
        # Security cleanup precedes and is independent of public delivery.
        if not receipt.get('access_cleaned'):
            ok = True
            if member:
                alive = guild.get_role(self.alive_role_id) if self.alive_role_id else None
                if alive and alive in member.roles:
                    try:
                        await finish_pending(member.remove_roles(alive))
                    except discord.NotFound:
                        pass
                    except discord.HTTPException:
                        ok = False
                    current()
            mafia = guild.get_channel(self.mafia_tc_id) if self.mafia_tc_id else None
            if mafia:
                try:
                    if member:
                        await finish_pending(mafia.set_permissions(member, overwrite=None))
                    else:
                        # Delete by snowflake even when the departed member is no longer cached.
                        await finish_pending(mafia._state.http.delete_channel_permissions(mafia.id, receipt['player_id']))
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    ok = False
                current()
            if member:
                for cid in (self.grave_tc_id, self.grave_vc_id):
                    ch = guild.get_channel(cid) if cid else None
                    if ch:
                        try:
                            if cid == self.grave_tc_id:
                                await finish_pending(ch.set_permissions(member, view_channel=True, send_messages=True))
                            else:
                                await finish_pending(ch.set_permissions(member, view_channel=True, connect=True, speak=True))
                                if getattr(member, 'voice', None) and member.voice.channel:
                                    await finish_pending(member.move_to(ch))
                        except discord.HTTPException:
                            ok = False
                        current()
            await commit(self, lambda: checkpoint(access_cleaned=ok), persist=self.in_progress)
        if channel and not receipt.get('announcement_id'):
            message = None
            marker = f"-# Game event: {receipt.get('notice_id', '')}"
            destination = getattr(channel, 'channel', channel)
            try:
                if receipt.get('notice_id') and hasattr(destination, 'history'):
                    try:
                        async for candidate in destination.history(limit=100):
                            if marker in candidate.content and candidate.author.id == getattr(getattr(_BOT, 'user', None), 'id', None):
                                message = candidate
                                break
                    except discord.HTTPException:
                        pass  # Sending may still work without Read Message History.
                current()
                if message is None:
                    message = await finish_pending(channel.send(death_text(receipt) + ('\n' + marker if receipt.get('notice_id') else ''),
                        allowed_mentions=discord.AllowedMentions.none()))
                await commit(self, lambda: checkpoint(announcement_id=getattr(message, 'id', None)), persist=self.in_progress)
            except discord.HTTPException:
                pass  # The saved receipt is retried separately from access cleanup.
        if not receipt.get('notices_delivered'):
            if member and receipt['real_role'] == 'Jester' and receipt['cause'] == 'lynch' and receipt['voters']:
                eligible = [f"#{self.player_slots.get(uid, '?')}: {p.display_name}" for uid in receipt['voters']
                            for p in self.players if p.id == uid]
                try:
                    await member.send('You have been successfully lynched! You win!\nChoose a Guilty or abstaining voter to haunt:\n' +
                                      '\n'.join(eligible) + '\nUse /actions tonight, or !haunt for eligible numbers.')
                except discord.HTTPException:
                    pass
                current()
            for uid in receipt.get('converted', []):
                converted = await self.get_member_safe(guild, uid)
                if converted:
                    try:
                        await converted.send('Your target has died. You have failed your goal and become a Jester.')
                    except discord.HTTPException:
                        pass
                current()
            await commit(self, lambda: checkpoint(notices_delivered=True), persist=self.in_progress)
        await commit(self, lambda: checkpoint(delivered=bool(receipt.get('access_cleaned') and receipt.get('announcement_id'))),
            persist=self.in_progress)

    async def start_night(self, ctx, *, trial_token=None):
        from gameplay.integration import start_night
        async with self._phase_transition_lock:
            return await start_night(self, ctx, trial_token=trial_token)

    async def start_day(self, ctx, *, resolution_token=None):
        from gameplay.integration import start_day
        async with self._phase_transition_lock:
            return await start_day(self, ctx, resolution_token=resolution_token)

    async def reset(self, guild, *, _from_check_win=False):
        from gameplay.integration import reset
        return await reset(self, guild, _from_check_win=_from_check_win)

    async def nuke_reset(self, guild):
        from gameplay.integration import reset
        return await reset(self, guild, nuke=True)


def get_game_for_guild(guild_id: int, *, allowed_guild_id: int) -> Game:
    if guild_id != allowed_guild_id:
        raise RuntimeError(f"This bot is locked to guild {allowed_guild_id}.")
    existing = active_games.get(guild_id)
    if existing is not None:
        return existing
    data = load_state(guild_id)
    if data and not is_stale_ended_state(data):
        game = Game.from_persisted(data)
        game._rehydrate_pending = True
        if game.in_progress and not game.game_key:
            game.started_at = game.started_at or datetime.now(timezone.utc).replace(
                microsecond=0
            ).isoformat()
            game.game_key = f"{guild_id}:{game.started_at}:{secrets.token_hex(8)}"
            try:
                from night_resume import normalize_night_completion_snapshot

                snap = normalize_night_completion_snapshot(
                    getattr(game, "night_completion_snapshot", None)
                )
                if snap is not None:
                    snap["game_key"] = game.game_key
                    game.night_completion_snapshot = snap
            except Exception:
                logging.exception(
                    "Failed to align snapshot game_key after mint guild_id=%s",
                    guild_id,
                )
            for attempt in range(3):
                try:
                    with game._file_write_lock:
                        save_state(guild_id, game.to_persisted())
                    break
                except Exception:
                    if attempt == 2:
                        logging.exception(
                            "Failed to persist minted game_key on lazy restore guild_id=%s",
                            guild_id,
                        )
                    else:
                        time.sleep(0.05 * (attempt + 1))
        active_games[guild_id] = game
        return game
    if data and is_stale_ended_state(data):
        stale = Game.from_persisted(data)
        active_games[guild_id] = stale
        return stale
    active_games[guild_id] = Game(guild_id)
    return active_games[guild_id]


def get_game_by_player_id(user_id: int) -> Optional[Game]:
    """
    Resolve the Game for this user by roster membership (`players`), not by living status.

    Intentionally includes dead players: they remain in `players` while `living_players` shrinks
    (e.g. haunt DMs). Last wills are editable only while alive; night action eligibility is enforced
    separately via `checks.only_during_night_gameplay` (living-id sync) and per-command role checks.
    """
    for game in active_games.values():
        if any(p.id == user_id for p in game.players):
            return game
        if user_id in getattr(game, "player_roles", {}):
            return game
    return None


install_game_state_delegates(Game)
