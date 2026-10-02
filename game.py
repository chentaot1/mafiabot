from __future__ import annotations

import asyncio
import logging
import random
import secrets
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

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
    STAND_ROLE_NAME,
    TOWN_ROLES,
)
from persistence import delete_state, save_state, save_state_async
from persistence import load_stats, save_stats
from engine import night as night_engine


_BOT: Optional[commands.Bot] = None


def bind_bot(bot: commands.Bot) -> None:
    global _BOT
    _BOT = bot


def _require_bot() -> commands.Bot:
    if _BOT is None:
        raise RuntimeError("Bot not bound. Call game.bind_bot(bot) during startup.")
    return _BOT


# --- MULTI-SERVER STATE ---
active_games: Dict[int, "Game"] = {}


class Game:
    def __init__(self, guild_id: int) -> None:
        self.guild_id: int = guild_id
        self.in_progress: bool = False
        self.phase: Optional[str] = None
        self.resolving: bool = False
        self.day_number: int = 0
        # Used for SQLite game-history idempotency and attribution.
        self.game_key: Optional[str] = None
        self.started_at: Optional[str] = None
        self.players: List[discord.Member] = []
        self.living_players: List[discord.Member] = []
        # Stable player numbers for targeting (`!heal 3`, etc.). Assigned at game start; persists across nights/deaths.
        self.player_slots: Dict[int, int] = {}

        self.player_roles: Dict[int, str] = {}
        self.night_actions: Dict[int, Dict] = {}
        self.role_states: Dict[int, Dict] = {}
        # Track doused players as a set (fast membership/removal, clearer semantics).
        self.doused_players: Set[int] = set()
        # Simple graveyard bookkeeping for Retributionist.
        # Entries are dicts with keys:
        #  - player_id: int
        #  - real_role: str
        #  - died_day: int
        #  - cause: str
        #  - is_hidden: bool (Gravedigger)
        #  - used_by_retri: bool
        self.graveyard: List[Dict] = []

        self.game_channel_id: Optional[int] = None
        self.mafia_tc_id: Optional[int] = None
        self.day_tc_id: Optional[int] = None
        self.day_vc_id: Optional[int] = None
        self.grave_tc_id: Optional[int] = None
        self.grave_vc_id: Optional[int] = None
        self.alive_role_id: Optional[int] = None
        self.stand_role_id: Optional[int] = None

        self.vote_in_progress: bool = False
        self.votes_today: int = 0
        self.locked_channel_ids: List[int] = []
        self.lockdown_role_id: Optional[int] = None
        # Tribunal crash-recovery snapshot. If the process dies mid-tribunal, `on_ready()`
        # can use this to restore VC permissions + stand role.
        self.tribunal_muted: bool = False
        self.tribunal_defendant_id: Optional[int] = None
        # B4 tribunal timers (persisted; resume when remaining wall-clock above floor).
        self.tribunal_defense_deadline_utc: Optional[str] = None
        self.tribunal_judgment_deadline_utc: Optional[str] = None
        self.tribunal_judgment_message_id: Optional[int] = None
        self.tribunal_subphase: Optional[str] = None  # "defense" | "judgment"
        self.tribunal_verdict_committed: bool = False

        # Prevent end-of-game races: multiple commands can trigger win checks/reset concurrently.
        self._endgame_lock: asyncio.Lock = asyncio.Lock()
        self.ending: bool = False
        # Idempotency: prevent double-counting stats on repeated endgame checks (race/restart).
        self.stats_committed: bool = False

    def _commit_endgame_stats(self, *, outcome: str, living_ids: List[int]) -> None:
        """
        Update per-guild persistent player stats.

        ToS-style personal wins:
          - Pirate/Executioner/Jester: win if personal condition met (even if Town/Mafia wins later).
          - Survivor/Chaos: win if they survive to end.
          - Draw: counts as neither win nor loss.
        """
        participants = list(self.player_roles.keys())
        if not participants:
            return
        if bool(getattr(self, "stats_committed", False)):
            return
        self.stats_committed = True

        data = load_stats(self.guild_id) or {}
        players = data.setdefault("players", {})

        outcome_norm = str(outcome)
        is_draw = outcome_norm == "Draw"

        for pid in participants:
            role = self.player_roles.get(pid, "Unknown")
            role_state = self.role_states.get(pid, {}) or {}
            alive = pid in set(living_ids)

            # Personal win flags
            pirate_win = role == "Pirate" and int(role_state.get("wins", 0)) >= 2
            exe_win = role == "Executioner" and bool(role_state.get("exe_won"))
            jester_win = role == "Jester" and bool(role_state.get("jester_won"))
            survivor_win = role == "Survivor" and alive
            chaos_win = role == "Chaos" and alive
            # ToS1-like: Witch wins if Town loses and Witch is alive.
            # In this bot's ruleset, Town loses when Mafia wins OR Arsonist wins.
            witch_win = role == "Witch" and outcome_norm in {"Mafia", "Arsonist"} and alive

            # Faction win flags
            # Faction win attribution: members win even if they died.
            # (Survivor/Chaos are handled separately as survivability-based personal wins.)
            town_win = outcome_norm == "Town" and role in TOWN_ROLES
            mafia_win = outcome_norm == "Mafia" and role in ALL_MAFIA_ROLES
            # Arsonist win condition remains survivability-based (Arsonist must be alive at end).
            arso_win = outcome_norm == "Arsonist" and role == "Arsonist" and alive

            did_win = False
            if not is_draw:
                did_win = any(
                    [pirate_win, exe_win, jester_win, survivor_win, chaos_win, witch_win, town_win, mafia_win, arso_win]
                )

            rec = players.setdefault(str(pid), {})
            rec["games_played"] = int(rec.get("games_played", 0)) + 1
            rec["wins"] = int(rec.get("wins", 0)) + (1 if did_win else 0)
            rec["losses"] = int(rec.get("losses", 0)) + (0 if (did_win or is_draw) else 1)
            rec["draws"] = int(rec.get("draws", 0)) + (1 if is_draw else 0)

            role_played = rec.setdefault("role_played", {})
            role_played[role] = int(role_played.get(role, 0)) + 1
            role_wins = rec.setdefault("role_wins", {})
            role_wins[role] = int(role_wins.get(role, 0)) + (1 if did_win else 0)

            # Coarse faction category for breakdown.
            if role in TOWN_ROLES:
                faction = "Town"
            elif role in ALL_MAFIA_ROLES:
                faction = "Mafia"
            else:
                faction = "Neutral"

            faction_played = rec.setdefault("faction_played", {})
            faction_played[faction] = int(faction_played.get(faction, 0)) + 1
            faction_wins = rec.setdefault("faction_wins", {})
            faction_wins[faction] = int(faction_wins.get(faction, 0)) + (1 if did_win else 0)

            personal = rec.setdefault("personal_wins", {})

            def _safe_int(v: object) -> int:
                try:
                    return int(v)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    return 0

            # Canonical personal-win keys match SQLite + /leaderboard.
            # Back-compat: migrate older role-name keys into the canonical keys on write.
            old_to_new = {
                "Pirate": "pirate_win",
                "Executioner": "exe_win",
                "Jester": "jester_win",
                "Survivor": "survivor_survived",
                "Chaos": "chaos_survived",
                "Witch": "witch_town_loses",
                "Arsonist": "arsonist_win",
            }
            if isinstance(personal, dict):
                migrated: Dict[str, int] = {}
                # Keep any already-canonical keys.
                for k, v in list(personal.items()):
                    if k in old_to_new:
                        continue
                    migrated[str(k)] = _safe_int(v)
                # Fold old keys into canonical keys.
                for old_key, new_key in old_to_new.items():
                    prev = _safe_int(personal.get(old_key, 0))
                    if prev:
                        migrated[new_key] = migrated.get(new_key, 0) + prev
                personal = migrated
            else:
                personal = {}
            rec["personal_wins"] = personal

            if pirate_win:
                personal["pirate_win"] = _safe_int(personal.get("pirate_win", 0)) + 1
            if exe_win:
                personal["exe_win"] = _safe_int(personal.get("exe_win", 0)) + 1
            if jester_win:
                personal["jester_win"] = _safe_int(personal.get("jester_win", 0)) + 1
            if survivor_win:
                personal["survivor_survived"] = _safe_int(personal.get("survivor_survived", 0)) + 1
            if chaos_win:
                personal["chaos_survived"] = _safe_int(personal.get("chaos_survived", 0)) + 1
            if witch_win:
                personal["witch_town_loses"] = _safe_int(personal.get("witch_town_loses", 0)) + 1
            if arso_win:
                personal["arsonist_win"] = _safe_int(personal.get("arsonist_win", 0)) + 1

        save_stats(self.guild_id, data)

        # Best-effort: also write to SQLite if a DB is attached to the bot.
        try:
            bot = _require_bot()
            db = getattr(bot, "db", None)
            if db is None:
                return

            def _role_to_faction(r: str) -> str:
                if r in TOWN_ROLES:
                    return "Town"
                if r in ALL_MAFIA_ROLES:
                    return "Mafia"
                return "Neutral"

            # Ensure we have a durable game_key.
            if not self.game_key:
                # If the server restarted mid-game before startgame snapshot, synthesize a key.
                nonce = secrets.token_hex(8)
                self.started_at = self.started_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                self.game_key = f"{self.guild_id}:{self.started_at}:{nonce}"

            ended_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            is_first, game_id = db.begin_game_commit(
                guild_id=self.guild_id,
                game_key=str(self.game_key),
                started_at=self.started_at,
                ended_at=ended_at,
                outcome=str(outcome),
                player_count=len(participants),
                ended_day_number=int(self.day_number),
                ended_phase=str(self.phase) if self.phase else None,
            )
            if not is_first:
                return

            # Write per-player game rows and accumulate aggregates.
            game_rows: List[Dict[str, object]] = []
            outcome_norm = str(outcome)
            is_draw = outcome_norm == "Draw"

            for pid in participants:
                role_end = self.player_roles.get(pid, "Unknown")
                s = self.role_states.get(pid, {}) or {}
                role_start = s.get("role_start") or role_end

                faction_start = _role_to_faction(str(role_start))
                faction_end = _role_to_faction(str(role_end))
                alive = pid in set(living_ids)

                death_cause = s.get("death_cause")
                died_day = s.get("died_day")

                game_rows.append(
                    {
                        "player_id": int(pid),
                        "role_start": str(role_start),
                        "role_end": str(role_end),
                        "faction_start": str(faction_start),
                        "faction_end": str(faction_end),
                        "survived": 1 if alive else 0,
                        "died_day": int(died_day) if died_day is not None else None,
                        "death_cause": str(death_cause) if death_cause is not None else None,
                    }
                )

                # Recompute win flags (match JSON semantics) for SQLite aggregates.
                pirate_win = role_end == "Pirate" and int(s.get("wins", 0)) >= 2
                exe_win = role_end == "Executioner" and bool(s.get("exe_won"))
                jester_win = role_end == "Jester" and bool(s.get("jester_won"))
                survivor_win = role_end == "Survivor" and alive
                chaos_win = role_end == "Chaos" and alive
                witch_win = role_end == "Witch" and outcome_norm in {"Mafia", "Arsonist"} and alive
                arso_win = outcome_norm == "Arsonist" and role_end == "Arsonist" and alive
                town_win = outcome_norm == "Town" and role_end in TOWN_ROLES
                mafia_win = outcome_norm == "Mafia" and role_end in ALL_MAFIA_ROLES

                did_win = False
                if not is_draw:
                    did_win = any([pirate_win, exe_win, jester_win, survivor_win, chaos_win, witch_win, town_win, mafia_win, arso_win])

                db.upsert_player_stats_delta(
                    guild_id=self.guild_id,
                    player_id=int(pid),
                    games_played=1,
                    wins_total=1 if did_win else 0,
                    losses_total=0 if (did_win or is_draw) else 1,
                    draws_total=1 if is_draw else 0,
                    wins_town=1 if town_win else 0,
                    wins_mafia=1 if mafia_win else 0,
                    wins_arsonist=1 if arso_win else 0,
                    last_game_at=ended_at,
                )
                db.upsert_player_role_stats_delta(
                    guild_id=self.guild_id,
                    player_id=int(pid),
                    role=str(role_end),
                    played=1,
                    wins_total=1 if did_win else 0,
                    losses_total=0 if (did_win or is_draw) else 1,
                )

                if pirate_win:
                    db.upsert_personal_win_delta(guild_id=self.guild_id, player_id=int(pid), key="pirate_win", delta=1)
                if exe_win:
                    db.upsert_personal_win_delta(guild_id=self.guild_id, player_id=int(pid), key="exe_win", delta=1)
                if jester_win:
                    db.upsert_personal_win_delta(guild_id=self.guild_id, player_id=int(pid), key="jester_win", delta=1)
                if survivor_win:
                    db.upsert_personal_win_delta(guild_id=self.guild_id, player_id=int(pid), key="survivor_survived", delta=1)
                if chaos_win:
                    db.upsert_personal_win_delta(guild_id=self.guild_id, player_id=int(pid), key="chaos_survived", delta=1)
                if witch_win:
                    db.upsert_personal_win_delta(guild_id=self.guild_id, player_id=int(pid), key="witch_town_loses", delta=1)
                if arso_win:
                    db.upsert_personal_win_delta(guild_id=self.guild_id, player_id=int(pid), key="arsonist_win", delta=1)

            db.insert_game_players(game_id=game_id, guild_id=self.guild_id, rows=game_rows)
        except Exception:
            logging.exception("SQLite endgame commit failed (non-fatal).")

    def to_persisted(self) -> Dict:
        return {
            "guild_id": self.guild_id,
            "in_progress": self.in_progress,
            "phase": self.phase,
            "resolving": self.resolving,
            "day_number": self.day_number,
            "game_key": self.game_key,
            "started_at": self.started_at,
            "player_ids": [p.id for p in self.players],
            "living_ids": [p.id for p in self.living_players],
            "player_slots": {str(k): int(v) for k, v in self.player_slots.items()},
            "player_roles": {str(k): v for k, v in self.player_roles.items()},
            "night_actions": {str(k): v for k, v in self.night_actions.items()},
            "role_states": {str(k): v for k, v in self.role_states.items()},
            "doused_players": sorted(self.doused_players),
            "graveyard": list(self.graveyard),
            "game_channel_id": self.game_channel_id,
            "mafia_tc_id": self.mafia_tc_id,
            "day_tc_id": self.day_tc_id,
            "day_vc_id": self.day_vc_id,
            "grave_tc_id": self.grave_tc_id,
            "grave_vc_id": self.grave_vc_id,
            "alive_role_id": self.alive_role_id,
            "stand_role_id": self.stand_role_id,
            "vote_in_progress": self.vote_in_progress,
            "votes_today": self.votes_today,
            "locked_channel_ids": list(self.locked_channel_ids),
            "lockdown_role_id": self.lockdown_role_id,
            "tribunal_muted": bool(getattr(self, "tribunal_muted", False)),
            "tribunal_defendant_id": self.tribunal_defendant_id,
            "tribunal_defense_deadline_utc": getattr(self, "tribunal_defense_deadline_utc", None),
            "tribunal_judgment_deadline_utc": getattr(self, "tribunal_judgment_deadline_utc", None),
            "tribunal_judgment_message_id": getattr(self, "tribunal_judgment_message_id", None),
            "tribunal_subphase": getattr(self, "tribunal_subphase", None),
            "tribunal_verdict_committed": bool(getattr(self, "tribunal_verdict_committed", False)),
            "stats_committed": bool(getattr(self, "stats_committed", False)),
        }

    @staticmethod
    def from_persisted(data: Dict) -> "Game":
        def _coerce_bool(v: object) -> bool:
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(int(v))
            if isinstance(v, str):
                s = v.strip().lower()
                if s in {"true", "1", "yes", "y", "on"}:
                    return True
                if s in {"false", "0", "no", "n", "off", ""}:
                    return False
            return bool(v)

        try:
            guild_id_raw = data.get("guild_id")
            guild_id = int(guild_id_raw) if guild_id_raw is not None else 0
        except (TypeError, ValueError, AttributeError):
            guild_id = 0
        g = Game(guild_id)
        g.in_progress = _coerce_bool(data.get("in_progress", False))
        g.phase = data.get("phase")
        # Transient lock flags should never persist across restarts.
        g.resolving = False
        try:
            g.day_number = int(data.get("day_number", 0))
        except (TypeError, ValueError):
            g.day_number = 0
        g.game_key = data.get("game_key")
        g.started_at = data.get("started_at")
        # Members are rehydrated later.
        g.players = []
        g.living_players = []
        # Corruption tolerance: persisted dict keys may not be int-coercible.
        g.player_roles = {}
        player_roles_raw = data.get("player_roles")
        if isinstance(player_roles_raw, dict):
            for k, v in player_roles_raw.items():
                try:
                    g.player_roles[int(k)] = v
                except (TypeError, ValueError):
                    continue

        g.night_actions = {}
        night_actions_raw = data.get("night_actions")
        if isinstance(night_actions_raw, dict):
            for k, v in night_actions_raw.items():
                try:
                    g.night_actions[int(k)] = v
                except (TypeError, ValueError):
                    continue

        g.role_states = {}
        role_states_raw = data.get("role_states")
        if isinstance(role_states_raw, dict):
            for k, v in role_states_raw.items():
                try:
                    g.role_states[int(k)] = v
                except (TypeError, ValueError):
                    continue
        doused: Set[int] = set()
        for x in (data.get("doused_players") or []):
            try:
                doused.add(int(x))
            except (TypeError, ValueError):
                continue
        g.doused_players = doused
        g.graveyard = list(data.get("graveyard") or [])
        g.stats_committed = _coerce_bool(data.get("stats_committed", False))
        def _coerce_opt_int(v: object) -> Optional[int]:
            if v is None:
                return None
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        g.game_channel_id = _coerce_opt_int(data.get("game_channel_id"))
        g.mafia_tc_id = _coerce_opt_int(data.get("mafia_tc_id"))
        g.day_tc_id = _coerce_opt_int(data.get("day_tc_id"))
        g.day_vc_id = _coerce_opt_int(data.get("day_vc_id"))
        g.grave_tc_id = _coerce_opt_int(data.get("grave_tc_id"))
        g.grave_vc_id = _coerce_opt_int(data.get("grave_vc_id"))
        g.alive_role_id = _coerce_opt_int(data.get("alive_role_id"))
        g.stand_role_id = _coerce_opt_int(data.get("stand_role_id"))
        vip_raw = _coerce_bool(data.get("vote_in_progress", False))
        has_tribunal_resume = bool(
            data.get("tribunal_defendant_id") is not None
            or data.get("tribunal_subphase")
            or data.get("tribunal_defense_deadline_utc")
            or data.get("tribunal_judgment_deadline_utc")
        )
        # Restore mid-tribunal sessions only when tribunal markers exist (B4); otherwise clear stale flags.
        g.vote_in_progress = bool(vip_raw and has_tribunal_resume)
        try:
            g.votes_today = max(0, int(data.get("votes_today", 0)))
        except (TypeError, ValueError):
            g.votes_today = 0
        locked: List[int] = []
        for x in (data.get("locked_channel_ids") or []):
            try:
                locked.append(int(x))
            except (TypeError, ValueError):
                continue
        g.locked_channel_ids = locked
        # Corruption tolerance: role ids may be strings in persisted JSON.
        lr = data.get("lockdown_role_id")
        try:
            g.lockdown_role_id = int(lr) if lr is not None else None
        except (TypeError, ValueError):
            g.lockdown_role_id = None
        g.tribunal_muted = _coerce_bool(data.get("tribunal_muted", False))
        t_def = data.get("tribunal_defendant_id")
        try:
            g.tribunal_defendant_id = int(t_def) if t_def is not None else None
        except (TypeError, ValueError):
            g.tribunal_defendant_id = None
        g.tribunal_defense_deadline_utc = data.get("tribunal_defense_deadline_utc")
        g.tribunal_judgment_deadline_utc = data.get("tribunal_judgment_deadline_utc")
        tjm = data.get("tribunal_judgment_message_id")
        try:
            g.tribunal_judgment_message_id = int(tjm) if tjm is not None else None
        except (TypeError, ValueError):
            g.tribunal_judgment_message_id = None
        tsp = data.get("tribunal_subphase")
        g.tribunal_subphase = str(tsp) if tsp else None
        g.tribunal_verdict_committed = _coerce_bool(data.get("tribunal_verdict_committed", False))
        g._persist_player_ids = []
        player_ids_raw = data.get("player_ids")
        if isinstance(player_ids_raw, list):
            for x in player_ids_raw:
                try:
                    g._persist_player_ids.append(int(x))
                except (TypeError, ValueError):
                    continue
        g._persist_living_ids = []
        living_ids_raw = data.get("living_ids")
        if isinstance(living_ids_raw, list):
            for x in living_ids_raw:
                try:
                    g._persist_living_ids.append(int(x))
                except (TypeError, ValueError):
                    continue

        slots_raw = data.get("player_slots") or {}
        if isinstance(slots_raw, dict) and slots_raw:
            slots: Dict[int, int] = {}
            for k, v in slots_raw.items():
                try:
                    pid = int(k)
                    slot = int(v)
                except (TypeError, ValueError):
                    continue
                slots[pid] = slot
            g.player_slots = slots
        else:
            # Back-compat: older saves won't have stable slots; derive deterministic slots from join order if possible.
            ordered_ids: List[int] = []
            player_ids_raw2 = data.get("player_ids")
            if isinstance(player_ids_raw2, list):
                for x in player_ids_raw2:
                    try:
                        ordered_ids.append(int(x))
                    except (TypeError, ValueError):
                        continue
            if not ordered_ids:
                # Corruption tolerance: persisted player_roles keys may not be int-coercible.
                tmp_ids: List[int] = []
                player_roles_raw2 = data.get("player_roles")
                if isinstance(player_roles_raw2, dict):
                    for k in player_roles_raw2.keys():
                        try:
                            tmp_ids.append(int(k))
                        except (TypeError, ValueError):
                            continue
                ordered_ids = sorted(tmp_ids)
            g.player_slots = {pid: i + 1 for i, pid in enumerate(ordered_ids)}

        # Restart safety: any in-progress Pirate duel cannot continue after a restart.
        # Force-finish persisted duels so `!resolve` can proceed.
        for act in g.night_actions.values():
            if not isinstance(act, dict):
                continue
            if act.get("type") == "plunder" and not act.get("duel_finished", False):
                act["duel_finished"] = True
                act["duel_won"] = False
        return g

    async def rehydrate_members(self, guild: discord.Guild) -> None:
        player_ids = getattr(self, "_persist_player_ids", [])
        living_ids = getattr(self, "_persist_living_ids", [])
        players: List[discord.Member] = []
        living: List[discord.Member] = []
        for pid in player_ids:
            m = await self.get_member_safe(guild, pid)
            if m:
                players.append(m)
        for lid in living_ids:
            m = await self.get_member_safe(guild, lid)
            if m:
                living.append(m)
        self.players = players
        self.living_players = living
        if hasattr(self, "_persist_player_ids"):
            delattr(self, "_persist_player_ids")
        if hasattr(self, "_persist_living_ids"):
            delattr(self, "_persist_living_ids")

    def persist_now(self) -> None:
        save_state(self.guild_id, self.to_persisted())

    async def persist_flush(self) -> None:
        """Write game JSON off the event loop (B3); await before dependent Discord sends."""
        try:
            await save_state_async(self.guild_id, self.to_persisted())
        except Exception:
            logging.exception("persist_flush failed guild_id=%s", self.guild_id)
            raise

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

    async def sync_living_players(self, guild: discord.Guild) -> None:
        alive_role = guild.get_role(self.alive_role_id) if self.alive_role_id else None
        dead_ids: Set[int] = set()
        for e in (self.graveyard or []):
            if not isinstance(e, dict):
                continue
            pid = e.get("player_id")
            if pid is None:
                continue
            try:
                dead_ids.add(int(pid))
            except (TypeError, ValueError):
                continue

        valid_living: List[discord.Member] = []
        for p in self.living_players:
            if p.id in dead_ids:
                continue
            member = await self.get_member_safe(guild, p.id)
            if not member:
                continue

            # Don't treat Discord role drift as "this player left the game".
            # If they're still alive in engine state, try to repair the Alive role.
            if alive_role and alive_role not in member.roles:
                try:
                    await member.add_roles(alive_role)
                except discord.HTTPException:
                    logging.warning(
                        "Alive role missing for living player_id=%s (%s); could not auto-repair.",
                        member.id,
                        getattr(member, "display_name", "?"),
                    )

            valid_living.append(member)

        # Stable ordering for any UI that lists living players by slot number.
        valid_living.sort(key=lambda m: (self.player_slots.get(m.id, 10**9), m.display_name.lower()))
        self.living_players = valid_living

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
        try:
            alive_role = discord.utils.get(guild.roles, name=ALIVE_ROLE_NAME)
            if not alive_role:
                alive_role = await guild.create_role(name=ALIVE_ROLE_NAME, color=discord.Color.green())
            self.alive_role_id = alive_role.id

            stand_role = discord.utils.get(guild.roles, name=STAND_ROLE_NAME)
            if not stand_role:
                stand_role = await guild.create_role(name=STAND_ROLE_NAME, color=discord.Color.red())
            self.stand_role_id = stand_role.id

            category = guild.get_channel(GAME_CATEGORY_ID)
            if category and not isinstance(category, discord.CategoryChannel):
                category = None
            if not category:
                category = discord.utils.get(guild.categories, name="Mafia Game")
            if not category:
                category = await guild.create_category("Mafia Game")

            playing_role = guild.get_role(PLAYING_ROLE_ID)
            overseer_role = guild.get_role(GAME_OVERSEER_ROLE_ID)

            grave_overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                alive_role: discord.PermissionOverwrite(view_channel=False),
            }

            mafia_overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                alive_role: discord.PermissionOverwrite(view_channel=False),
            }

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
                setattr(self, attr, existing.id)

            # Harden privacy even if channels pre-existed with stale overwrites.
            mafia_tc = guild.get_channel(self.mafia_tc_id) if self.mafia_tc_id else None
            graveyard_tc = guild.get_channel(self.grave_tc_id) if self.grave_tc_id else None
            graveyard_vc = guild.get_channel(self.grave_vc_id) if self.grave_vc_id else None
            for ch in [mafia_tc, graveyard_tc, graveyard_vc]:
                if not ch:
                    continue
                try:
                    await ch.set_permissions(guild.default_role, view_channel=False)
                except discord.HTTPException:
                    logging.warning("Failed to hide private channel from @everyone.", exc_info=True)
                try:
                    await ch.set_permissions(alive_role, view_channel=False)
                except discord.HTTPException:
                    logging.warning("Failed to hide private channel from Alive role.", exc_info=True)
                if playing_role:
                    try:
                        await ch.set_permissions(playing_role, view_channel=False)
                    except discord.HTTPException:
                        logging.warning("Failed to hide private channel from Playing role.", exc_info=True)

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
                self.lockdown_role_id = lockdown_role.id

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
                self.locked_channel_ids = locked

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

            day_vc = guild.get_channel(self.day_vc_id)
            if day_vc:
                try:
                    await day_vc.set_permissions(guild.default_role, speak=False, connect=False)
                    await day_vc.set_permissions(alive_role, connect=True, speak=True)
                    await day_vc.set_permissions(stand_role, connect=True, speak=True)
                except discord.HTTPException:
                    logging.warning("Failed to set day VC permissions (speak/connect).", exc_info=True)
        except (discord.Forbidden, discord.HTTPException) as e:
            raise RuntimeError(
                "Infrastructure setup failed. Ensure the bot has Administrator (or Manage Roles/Channels) permissions."
            ) from e

    async def reset(self, guild: discord.Guild) -> None:
        # Mark ended immediately to prevent concurrent tasks from persisting/resolving mid-reset.
        self.in_progress = False
        self.ending = True
        self.resolving = False
        self.vote_in_progress = False
        # Allow a subsequent game to commit stats again.
        self.stats_committed = False

        bot = _require_bot()
        db = getattr(bot, "db", None)
        gk = self.game_key or "unknown"
        for player in list(self.players):
            if db:
                db.enqueue_dm_outbox(
                    guild_id=self.guild_id,
                    kind="game_over",
                    dedupe_key=f"mafia_game_over:{self.guild_id}:{gk}:{player.id}",
                    target_user_id=player.id,
                    content="--- GAME OVER ---\nThe game has ended.",
                )
            else:
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

        # (Already marked ended at the start of reset.)
        self.phase = None
        self.day_number = 0
        self.game_key = None
        self.started_at = None
        self.players.clear()
        self.living_players.clear()
        self.player_slots.clear()
        self.player_roles.clear()
        self.night_actions.clear()
        self.role_states.clear()
        self.doused_players.clear()
        self.graveyard.clear()
        self.vote_in_progress = False
        self.votes_today = 0
        active_games.pop(self.guild_id, None)
        logging.info(f"Game reset for guild {self.guild_id}.")
        delete_state(self.guild_id)

    async def nuke_reset(self, guild: discord.Guild) -> None:
        """
        Nuclear cleanup that does not rely on a healthy in-memory game state.
        Best-effort: removes game roles from members, clears category/channel overwrites,
        deletes bot-created channels, and drops persisted state.
        """
        self.in_progress = False
        self.ending = True
        self.resolving = False
        self.vote_in_progress = False
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

        self.in_progress = False
        self.phase = None
        self.day_number = 0
        self.game_key = None
        self.started_at = None
        self.players.clear()
        self.living_players.clear()
        self.player_slots.clear()
        self.player_roles.clear()
        self.night_actions.clear()
        self.role_states.clear()
        self.doused_players.clear()
        self.graveyard.clear()
        self.vote_in_progress = False
        self.votes_today = 0
        self.locked_channel_ids = []
        self.lockdown_role_id = None
        active_games.pop(self.guild_id, None)
        delete_state(self.guild_id)

    async def set_night_action(self, ctx: commands.Context, action: dict) -> None:
        if getattr(self, "resolving", False):
            try:
                await ctx.send("🛑 **Night is resolving.** Your action cannot be changed right now.")
            except discord.HTTPException:
                pass
            return

        existing = self.night_actions.get(ctx.author.id)
        if existing and existing.get("type") == "plunder" and not existing.get("duel_finished", False):
            try:
                await ctx.send("⚔️ Your plunder duel is already in progress. You can't change actions until it finishes.")
            except discord.HTTPException:
                pass
            return
        if ctx.author.id in self.night_actions:
            try:
                await ctx.send("*(Your previous action has been replaced.)*")
            except discord.HTTPException:
                pass
        self.night_actions[ctx.author.id] = action
        await self.persist_flush()

    async def start_night(self, ctx) -> None:
        if not self.in_progress:
            return
        # Idempotency: if we're already in night phase AND players have started submitting actions,
        # do not re-run night-start side effects (otherwise we'd wipe submitted night actions).
        if self.phase == "night" and self.night_actions:
            return
        self.phase = "night"
        self.night_actions = {}

        for state in self.role_states.values():
            for key in [
                "is_framed",
                "is_vested",
                "is_on_alert",
                "pirate_win_this_night",
                "gatekeeper_used_this_night",
                "self_heal_used_this_night",
                "vest_used_this_night",
                "alert_used_this_night",
                "bg_protect_used_this_night",
                "bg_self_protect_used_this_night",
                "vig_shot_used_this_night",
                "mole_used_this_night",
                "tailor_used_this_night",
                "gravedigger_used_this_night",
                "chaos_used_this_night",
                "chaos_protected_by",
                "attacked_tonight",
                "attacked_tonight_reason",
            ]:
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

        flavor_texts = [
            "A deathly silence falls over the Town Square as the sun dips below the horizon.",
            "The moon hangs high in the sky, casting long eerie shadows across the deserted streets.",
            "An unsettling quiet descends. The time for secrets and whispers has begun.",
        ]
        await ctx.send(
            f"{random.choice(flavor_texts)}\n🌙 It is now **Night {self.day_number}**. Check your DMs for your nightly duties."
        )

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
                try:
                    await player.send(f"**Living Players:**\n{player_list_text}\n\n{action_text}")
                except discord.HTTPException:
                    pass

    async def start_day(self, ctx) -> None:
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

        day_vc = ctx.guild.get_channel(self.day_vc_id) if self.day_vc_id else None
        alive_role = ctx.guild.get_role(self.alive_role_id) if self.alive_role_id else None
        if day_vc and alive_role:
            try:
                await day_vc.set_permissions(alive_role, connect=True, speak=True)
            except discord.HTTPException:
                pass
        await ctx.send(f"☀️ The sun rises on **Day {self.day_number}**. Remaining players: {len(self.living_players)}")

    async def check_win_conditions(self) -> bool:
        # Many commands can trigger end checks (resolve, lynch, slay, etc.).
        # Make the win-check + reset sequence mutually exclusive.
        async with self._endgame_lock:
            if not self.in_progress:
                return False
            if self.ending:
                return True

            bot = _require_bot()
            game_channel = bot.get_channel(self.game_channel_id) if self.game_channel_id else None
            if not game_channel:
                # Best-effort recovery: fall back to a usable guild channel to keep win checks functional
                # if the original game channel was deleted.
                guild = bot.get_guild(self.guild_id)
                if guild:
                    # Prefer the dedicated day text channel if present.
                    if self.day_tc_id:
                        day_tc = guild.get_channel(self.day_tc_id)
                        if isinstance(day_tc, discord.TextChannel):
                            me2 = guild.me
                            if not me2 and bot.user:
                                me2 = guild.get_member(bot.user.id)
                            if me2 and day_tc.permissions_for(me2).send_messages and day_tc.permissions_for(me2).view_channel:
                                game_channel = day_tc

                    if game_channel is None:
                        me = guild.me
                        if not me and bot.user:
                            me = guild.get_member(bot.user.id)

                        if me:
                            game_channel = guild.system_channel or next(
                                (c for c in guild.text_channels if c.permissions_for(me).send_messages),
                                None,
                            )
                        else:
                            # If we can't resolve the bot member yet, avoid crashing; fall back to system_channel only.
                            game_channel = guild.system_channel
                if not game_channel:
                    return False
                self.game_channel_id = game_channel.id
                await self.persist_flush()

            await self.sync_living_players(game_channel.guild)
            living_ids = await self.get_living_ids(game_channel.guild)

            async def _announce_personal_wins() -> None:
                """Announce ToS-style personal wins (do not end the match)."""
                pirate_winner_id = next(
                    (
                        p_id
                        for p_id, r in self.player_roles.items()
                        if r == "Pirate"
                        and self.role_states.get(p_id, {}).get("wins", 0) >= 2
                        and not self.role_states.get(p_id, {}).get("win_announced")
                    ),
                    None,
                )
                if pirate_winner_id is not None:
                    pirate = await self.get_member_safe(game_channel.guild, pirate_winner_id)
                    if pirate:
                        await game_channel.send(
                            f"⚔️ **The Pirate, {pirate.mention}, has successfully plundered their way to victory!**"
                        )
                    else:
                        await game_channel.send("⚔️ **The Pirate has successfully plundered their way to victory!**")
                    self.role_states.setdefault(pirate_winner_id, {})["win_announced"] = True

                exe_winner_id = next(
                    (
                        p_id
                        for p_id, r in self.player_roles.items()
                        if r == "Executioner"
                        and self.role_states.get(p_id, {}).get("exe_won")
                        and not self.role_states.get(p_id, {}).get("win_announced")
                    ),
                    None,
                )
                if exe_winner_id is not None:
                    exe = await self.get_member_safe(game_channel.guild, exe_winner_id)
                    if exe:
                        await game_channel.send(
                            f"⚖️ **The Executioner, {exe.mention}, has achieved their goal and wins!**"
                        )
                    else:
                        await game_channel.send("⚖️ **The Executioner has achieved their goal and wins!**")
                    self.role_states.setdefault(exe_winner_id, {})["win_announced"] = True

            async def _announce_survivor_style(*, winning_faction: Optional[str]) -> None:
                """Congratulate neutral survivals at endgame consistently across win paths."""
                survivor_id = next((p_id for p_id in living_ids if self.player_roles.get(p_id) == "Survivor"), None)
                survivor = await self.get_member_safe(game_channel.guild, survivor_id) if survivor_id else None
                if survivor:
                    await game_channel.send(
                        f"Congratulations to the Survivor, {survivor.mention}, for making it to the end!"
                    )

                witch_id = next((p_id for p_id in living_ids if self.player_roles.get(p_id) == "Witch"), None)
                witch = await self.get_member_safe(game_channel.guild, witch_id) if witch_id else None
                if witch and winning_faction in {"Mafia", "Arsonist"}:
                    await game_channel.send(f"Congratulations to the Witch, {witch.mention}, for surviving to see the Town fall!")

                chaos_id = next((p_id for p_id in living_ids if self.player_roles.get(p_id) == "Chaos"), None)
                chaos = await self.get_member_safe(game_channel.guild, chaos_id) if chaos_id else None
                if chaos:
                    await game_channel.send(f"Congratulations to Chaos, {chaos.mention}, for surviving the madness!")

            async def _collect_personal_winner_labels() -> List[str]:
                """End-of-game recap: list who fulfilled personal win conditions."""
                labels: List[str] = []

                pirate_ids = [
                    p_id
                    for p_id, r in self.player_roles.items()
                    if r == "Pirate" and self.role_states.get(p_id, {}).get("wins", 0) >= 2
                ]
                for p_id in pirate_ids:
                    m = await self.get_member_safe(game_channel.guild, p_id)
                    labels.append(f"Pirate ({m.mention})" if m else "Pirate")

                exe_ids = [
                    p_id
                    for p_id, r in self.player_roles.items()
                    if r == "Executioner" and self.role_states.get(p_id, {}).get("exe_won")
                ]
                for p_id in exe_ids:
                    m = await self.get_member_safe(game_channel.guild, p_id)
                    labels.append(f"Executioner ({m.mention})" if m else "Executioner")

                return sorted(set(labels), key=str.lower)

            # Personal wins should announce even if the match ends immediately after (e.g., Draw).
            await _announce_personal_wins()

            if not living_ids:
                self.ending = True
                await game_channel.send("💀 **GAME OVER! Everyone has died. It's a DRAW!**")
                personal = await _collect_personal_winner_labels()
                if personal:
                    await game_channel.send("🏆 **Personal victories:** " + ", ".join(personal))
                await asyncio.to_thread(self._commit_endgame_stats, outcome="Draw", living_ids=living_ids)
                await self.reset(game_channel.guild)
                return True

            mafia_ids = [p_id for p_id in living_ids if self.player_roles.get(p_id) in ALL_MAFIA_ROLES]

            # Mobster Promotion Logic
            if mafia_ids and not any(self.player_roles.get(p_id) == "Mobster" for p_id in mafia_ids):
                new_mobster_id = random.choice(mafia_ids)
                self.player_roles[new_mobster_id] = "Mobster"
                new_mobster = await self.get_member_safe(game_channel.guild, new_mobster_id)
                if new_mobster:
                    try:
                        await new_mobster.send(
                            "🔪 **You have been promoted to Mobster.** The syndicate needs you. You can now use `!kill`."
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
                arsonist = await self.get_member_safe(game_channel.guild, arsonist_winner_id)
                if arsonist:
                    await game_channel.send(f"🔥 **GAME OVER! The Arsonist, {arsonist.mention}, has won!**")
                    await _announce_survivor_style(winning_faction="Arsonist")
                await asyncio.to_thread(self._commit_endgame_stats, outcome="Arsonist", living_ids=living_ids)
                await self.reset(game_channel.guild)
                return True

            # Arsonist stalemate breaker: if only the Arsonist + non-killing neutrals remain, end the game.
            arsonist_id = next((p_id for p_id in living_ids if self.player_roles.get(p_id) == "Arsonist"), None)
            if arsonist_id is not None and len(living_ids) >= 2:
                town_count_now = sum(1 for p_id in living_ids if self.player_roles.get(p_id) in TOWN_ROLES)
                mafia_count_now = len(mafia_ids)
                other_roles = [self.player_roles.get(p_id) for p_id in living_ids if p_id != arsonist_id]
                harmless_neutrals = {"Witch", "Survivor", "Executioner", "Jester", "Chaos"}
                if mafia_count_now == 0 and town_count_now == 0 and other_roles and all(r in harmless_neutrals for r in other_roles):
                    self.ending = True
                    arsonist = await self.get_member_safe(game_channel.guild, arsonist_id)
                    if arsonist:
                        await game_channel.send(f"🔥 **GAME OVER! The Arsonist, {arsonist.mention}, has won!**")
                    else:
                        await game_channel.send("🔥 **GAME OVER! The Arsonist has won!**")
                    await _announce_survivor_style(winning_faction="Arsonist")
                    await asyncio.to_thread(self._commit_endgame_stats, outcome="Arsonist", living_ids=living_ids)
                    await self.reset(game_channel.guild)
                    return True

            # Faction Win Conditions
            mafia_count = len(mafia_ids)
            town_count = sum(1 for p_id in living_ids if self.player_roles.get(p_id) in TOWN_ROLES)
            is_arso_alive = any(self.player_roles.get(p_id) == "Arsonist" for p_id in living_ids)

            winning_faction = None
            if mafia_count == 0 and town_count > 0 and not is_arso_alive:
                winning_faction = "Town"
            elif mafia_count > 0 and mafia_count >= (len(living_ids) - mafia_count) and not is_arso_alive:
                winning_faction = "Mafia"

            if not winning_faction:
                return False

            self.ending = True
            await game_channel.send(f"🎉 **GAME OVER! The {winning_faction} has won!**")
            await _announce_survivor_style(winning_faction=winning_faction)
            await asyncio.to_thread(self._commit_endgame_stats, outcome=winning_faction, living_ids=living_ids)
            await self.reset(game_channel.guild)
            return True

    async def process_death(
        self,
        ctx_or_channel,
        member: discord.Member,
        cause: str,
        voters: Optional[List[discord.Member]] = None,
        custom_message: Optional[str] = None,
    ) -> None:
        await self.sync_living_players(member.guild)
        living_ids = await self.get_living_ids(member.guild)
        if member.id not in living_ids:
            return

        self.living_players = [p for p in self.living_players if p.id != member.id]

        alive_role = member.guild.get_role(self.alive_role_id)
        if alive_role and alive_role in member.roles:
            try:
                await member.remove_roles(alive_role)
            except discord.HTTPException:
                pass

        real_role = self.player_roles.get(member.id, "Unknown")
        revealed_role = self.role_states.get(member.id, {}).get("is_tailored_as", real_role)
        is_hidden = self.role_states.get(member.id, {}).get("is_hidden_by_gravedigger", False)
        will_text = str(self.role_states.get(member.id, {}).get("will", "") or "")

        # Persist death metadata for endgame history (first-write wins).
        ds = self.role_states.setdefault(member.id, {})
        ds.setdefault("death_cause", str(cause))
        ds.setdefault("died_day", int(self.day_number))

        def _fmt_will(txt: str) -> str:
            # Avoid breaking code blocks; keep output bounded.
            safe = txt.replace("```", "'''").strip()
            return safe[:1800]

        # Record corpse for Retributionist (true role, regardless of Tailor).
        self.graveyard.append(
            {
                "player_id": member.id,
                "real_role": real_role,
                "died_day": int(self.day_number),
                "cause": cause,
                "is_hidden": bool(is_hidden),
                "used_by_retri": False,
            }
        )

        flavor_texts = [
            "A blood-curdling scream shatters the morning's peace...",
            "As dawn breaks, a grim discovery is made...",
            "The town gathers in the square, one face fewer than the day before...",
            "A chilling wind blows through the town. Another soul has been lost to the darkness.",
        ]
        flavor = random.choice(flavor_texts)

        if is_hidden:
            death_announcement = (
                f"{flavor}\n{custom_message or f'**{member.mention}** was found dead.'}\n\nTheir role was obscured by a Gravedigger."
            )
        elif custom_message:
            death_announcement = (
                f"{flavor}\n{custom_message}\n\nIt was discovered that {member.mention}'s role was **{revealed_role}**."
            )
        else:
            death_announcement = (
                f"{flavor}\nA tragedy has occurred! **{member.mention}** was found dead. Their role was **{revealed_role}**."
            )

        if will_text.strip():
            death_announcement += f"\n\n**Last Will:**\n```{_fmt_will(will_text)}```"

        await ctx_or_channel.send(death_announcement)

        # Executioner Check
        for p_id, state in self.role_states.items():
            player = await self.get_member_safe(member.guild, p_id)
            if (
                player
                and p_id in living_ids
                and self.player_roles.get(p_id) == "Executioner"
                and state.get("exe_target") == member.id
            ):
                if cause == "lynch":
                    # Mark win; win announcement/reset handled by check_win_conditions.
                    self.role_states.setdefault(p_id, {})["exe_won"] = True
                else:
                    # ToS-like: any non-lynch death of the Executioner's target causes EXE -> Jester.
                    self.player_roles[p_id] = "Jester"
                    try:
                        await player.send("Your target has died. You have failed your goal and become a Jester.")
                    except discord.HTTPException:
                        pass

        # Jester Check
        if real_role == "Jester" and cause == "lynch":
            self.role_states.setdefault(member.id, {})["can_haunt"] = True
            self.role_states.setdefault(member.id, {})["jester_won"] = True
            if voters:
                try:
                    await ctx_or_channel.send("The jester will get his revenge from the grave!")
                except discord.HTTPException:
                    pass

                voter_list_text = "\n".join([f"{i + 1}: {v.display_name}" for i, v in enumerate(voters)])
                self.role_states[member.id]["guilty_voters"] = [v.id for v in voters]
                try:
                    await member.send(
                        "You have been successfully lynched! You win!\n"
                        "Now, choose one of the players who voted **guilty** or **abstained** to take to the grave.\n"
                        f"Eligible voters:\n{voter_list_text}\n"
                        "Use `!haunt <number>` in this DM to enact your revenge."
                    )
                except discord.HTTPException:
                    pass

        # Graveyard Permissions
        graveyard_tc = member.guild.get_channel(self.grave_tc_id)
        graveyard_vc = member.guild.get_channel(self.grave_vc_id)
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

        # Mafia Chat Permissions
        mafia_tc = member.guild.get_channel(self.mafia_tc_id)
        if mafia_tc:
            try:
                await mafia_tc.set_permissions(member, overwrite=None)
            except discord.HTTPException:
                pass

    async def process_death_by_id(
        self,
        ctx_or_channel,
        guild: discord.Guild,
        player_id: int,
        cause: str,
        *,
        custom_message: Optional[str] = None,
    ) -> None:
        """
        Fallback death processing when the Discord Member cannot be fetched (e.g., left server).
        Keeps engine state/graveyard consistent and still announces death to the game channel.
        """
        await self.sync_living_players(guild)
        living_ids = await self.get_living_ids(guild)
        if player_id not in living_ids:
            return

        # Remove from living list (by id).
        self.living_players = [p for p in self.living_players if p.id != player_id]

        real_role = self.player_roles.get(player_id, "Unknown")
        revealed_role = self.role_states.get(player_id, {}).get("is_tailored_as", real_role)
        is_hidden = self.role_states.get(player_id, {}).get("is_hidden_by_gravedigger", False)
        will_text = str(self.role_states.get(player_id, {}).get("will", "") or "")

        # Persist death metadata for endgame history (first-write wins).
        ds = self.role_states.setdefault(int(player_id), {})
        ds.setdefault("death_cause", str(cause))
        ds.setdefault("died_day", int(self.day_number))

        def _fmt_will(txt: str) -> str:
            safe = txt.replace("```", "'''").strip()
            return safe[:1800]

        # Record corpse for Retributionist (true role, regardless of Tailor).
        self.graveyard.append(
            {
                "player_id": int(player_id),
                "real_role": real_role,
                "died_day": int(self.day_number),
                "cause": cause,
                "is_hidden": bool(is_hidden),
                "used_by_retri": False,
            }
        )

        flavor_texts = [
            "A blood-curdling scream shatters the morning's peace...",
            "As dawn breaks, a grim discovery is made...",
            "The town gathers in the square, one face fewer than the day before...",
            "A chilling wind blows through the town. Another soul has been lost to the darkness.",
        ]
        flavor = random.choice(flavor_texts)

        mention = f"<@{int(player_id)}>"
        if is_hidden:
            death_announcement = (
                f"{flavor}\n{custom_message or f'**{mention}** was found dead.'}\n\nTheir role was obscured by a Gravedigger."
            )
        elif custom_message:
            death_announcement = f"{flavor}\n{custom_message}\n\nIt was discovered that {mention}'s role was **{revealed_role}**."
        else:
            death_announcement = f"{flavor}\nA tragedy has occurred! **{mention}** was found dead. Their role was **{revealed_role}**."

        if will_text.strip():
            death_announcement += f"\n\n**Last Will:**\n```{_fmt_will(will_text)}```"

        await ctx_or_channel.send(death_announcement)

        # Jester Check (by id): if a Jester is lynched but the member can't be fetched,
        # still mark their personal win and allow haunt selection via existing DM command.
        if real_role == "Jester" and cause == "lynch":
            s = self.role_states.setdefault(int(player_id), {})
            s["can_haunt"] = True
            s["jester_won"] = True

        # Executioner conversion/win check (works without the target member object).
        for p_id, state in self.role_states.items():
            exe_player = await self.get_member_safe(guild, p_id)
            if (
                exe_player
                and p_id in living_ids
                and self.player_roles.get(p_id) == "Executioner"
                and state.get("exe_target") == player_id
            ):
                if cause == "lynch":
                    self.role_states.setdefault(p_id, {})["exe_won"] = True
                else:
                    self.player_roles[p_id] = "Jester"
                    try:
                        await exe_player.send("Your target has died. You have failed your goal and become a Jester.")
                    except discord.HTTPException:
                        pass

    def _get_night_prompt(self, player: discord.Member, role: str) -> Optional[str]:
        state = self.role_states.get(player.id, {})
        if role == "Mobster":
            return "Use `!kill <number>`."
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
            return "Use `!douse <number>` to douse a player, `!ignite` to ignite, or `!clean` to remove gasoline from yourself."
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
                "`!reanimate <corpse> <target>` (or `!reanimate <corpse> <t1> <t2>` for Transporter)."
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

    async def _send_night_feedback(self, blocked: List[int], guild: discord.Guild) -> None:
        await night_engine.send_night_feedback(self, blocked, guild)


def get_game_for_guild(guild_id: int, *, allowed_guild_id: int) -> Game:
    if guild_id != allowed_guild_id:
        raise RuntimeError(f"This bot is locked to guild {allowed_guild_id}.")
    if guild_id not in active_games:
        active_games[guild_id] = Game(guild_id)
    return active_games[guild_id]


def get_game_by_player_id(user_id: int) -> Optional[Game]:
    for game in active_games.values():
        if any(p.id == user_id for p in game.players):
            return game
    return None
