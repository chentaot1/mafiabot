"""Hybrid personal-win DMs: immediate private victory, public announce at endgame."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from messages import tos as tos_msg
from messages.delivery import dm_member

if TYPE_CHECKING:
    from game import Game


def personal_win_condition_met(game: "Game", player_id: int, role: str) -> bool:
    st = game.role_states.get(int(player_id), {}) or {}
    if role == "Pirate":
        return int(st.get("wins", 0)) >= 2
    if role == "Executioner":
        return bool(st.get("exe_won"))
    if role == "Jester":
        return bool(st.get("jester_won"))
    return False


def _victory_dm_text(role: str) -> Optional[str]:
    if role == "Pirate":
        return tos_msg.win_pirate_anon()
    if role == "Executioner":
        return tos_msg.win_executioner_anon()
    if role == "Jester":
        return tos_msg.win_jester_anon()
    return None


async def send_personal_win_dm_if_needed(game: "Game", guild: object, player_id: int) -> bool:
    """
    Send private victory DM once when Pirate/Exe/Jester personal win is locked in.
    Returns True if a DM was sent this call.
    """
    role = game.player_roles.get(int(player_id))
    if role not in ("Pirate", "Executioner", "Jester"):
        return False
    if not personal_win_condition_met(game, int(player_id), str(role)):
        return False
    st = game.role_states.setdefault(int(player_id), {})
    if bool(st.get("personal_win_dm_sent")):
        return False
    member = await game.get_member_safe(guild, int(player_id))  # type: ignore[arg-type]
    if member is None:
        return False
    body = _victory_dm_text(str(role))
    if not body:
        return False
    ok = await dm_member(member, body)
    if ok:
        st["personal_win_dm_sent"] = True
        try:
            await game.persist_flush()
        except Exception:
            logging.exception(
                "persist_flush failed after personal_win_dm_sent guild_id=%s player_id=%s",
                game.guild_id,
                player_id,
            )
    else:
        logging.warning(
            "Personal win victory DM failed guild_id=%s player_id=%s role=%s",
            game.guild_id,
            player_id,
            role,
        )
    return ok
