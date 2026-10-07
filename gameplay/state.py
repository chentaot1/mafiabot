from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable, TypeVar

T = TypeVar("T")


class Rejected(ValueError):
    """A submission is no longer valid; the message is safe to show its owner."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def remaining(deadline: str) -> float:
    try:
        parsed = datetime.fromisoformat(deadline)
        if parsed.tzinfo is None:
            raise ValueError("UTC offset required")
        return (parsed.astimezone(timezone.utc) - now()).total_seconds()
    except (ValueError, TypeError, AttributeError):
        raise Rejected("This session has an invalid deadline.") from None


def identity(game) -> tuple:
    return game.game_key, game.day_number, game.gameplay.get("night_token")


def require_current(game, *, phase=None, expected=None) -> None:
    from game import active_games
    if active_games.get(game.guild_id) is not game or not game.in_progress or game.ending:
        raise Rejected("This game has ended. Open the controls for the current game.")
    if (game._rehydrate_pending or game._recovering_permissions
            or game.gameplay.get('startup', {}).get('complete') is False):
        raise Rejected("Game setup/recovery is still in progress. Please retry shortly.")
    if phase and game.phase != phase:
        raise Rejected(f"These controls are only available during {phase}.")
    if expected is not None and identity(game) != tuple(expected):
        raise Rejected("These controls belong to an earlier phase. Use /actions to reopen them.")


# Discord Members and locks must retain their identities during rollback.
_REFERENCES = {"players", "living_players"}
_FIELDS = (
    "in_progress", "phase", "resolving", "ending", "day_number", "game_key",
    "started_at", "game_channel_id", "players", "living_players", "player_slots", "player_roles",
    "night_actions", "role_states", "doused_players", "graveyard", "votes_today",
    "vote_in_progress", "tribunal_muted", "tribunal_defendant_id",
    "tribunal_defense_deadline_utc", "tribunal_judgment_deadline_utc",
    "tribunal_judgment_message_id", "tribunal_subphase", "tribunal_verdict_committed",
    "gameplay", "stats_committed", "action_cooldowns",
    "mafia_tc_id", "day_tc_id", "day_vc_id", "grave_tc_id", "grave_vc_id",
    "alive_role_id", "stand_role_id", "lockdown_role_id", "locked_channel_ids",
    "night", "tribunal_state", "bloodless_cycle_streak", "deaths_this_cycle",
    "bloodless_stalemate_pending", "cleanup_pending",
    "_rehydrate_pending", "_recovering_permissions", "_persist_player_ids", "_persist_living_ids",
)


async def flush_committed(game) -> bool:
    """Finish the disk operation before unlocking, even if the caller is cancelled."""
    task = asyncio.create_task(game.persist_flush())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()  # Real write failures propagate; cancellation never masks them.
    return cancelled


async def commit(game, update: Callable[[], T], *, persist=True, allow_recovery=False) -> T:
    """Apply a synchronous model change and save it as one guarded operation."""
    cancelled = False
    async with game.state_lock:
        if (game._rehydrate_pending or game._recovering_permissions) and not allow_recovery:
            raise Rejected("Saved players are still being recovered. Please retry shortly.")
        before = {name: (list(getattr(game, name)) if name in _REFERENCES else
                         deepcopy(getattr(game, name))) for name in _FIELDS}
        try:
            result = update()
            if persist:
                cancelled = await flush_committed(game)
        except BaseException:
            for name, value in before.items():
                # Validation rejections do not replace otherwise unchanged records.
                # Controllers may still hold a reference to those session records.
                if getattr(game, name) != value:
                    setattr(game, name, value)
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def session(game, token: str, *, phase=None, open_only=True):
    require_current(game, phase=phase)
    trial = game.gameplay.get("trial")
    if (not isinstance(trial, dict) or trial.get("id") != token or trial.get("day") != game.day_number
            or trial.get("match", game.game_key) != game.game_key):
        raise Rejected("This trial is no longer active.")
    if open_only and trial.get("stage") in {"closed", "done", "cancelled"}:
        raise Rejected("Voting has closed.")
    return trial
