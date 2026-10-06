from __future__ import annotations

import hashlib


def apply_death(game, player_id: int, cause: str, *, voters=(), custom_message=None):
    """Commit all logical death effects without Discord I/O or an await boundary."""
    receipts = game.gameplay.setdefault("deaths", {})
    if str(player_id) in receipts:
        return receipts[str(player_id)]
    living_ids = {p.id for p in game.living_players}
    if player_id not in living_ids:
        return None
    role = game.player_roles.get(player_id, "Unknown")
    state = game.role_states.setdefault(player_id, {})
    receipt = {
        "player_id": player_id, "cause": cause, "day": game.day_number,
        "real_role": role, "revealed_role": state.get("is_tailored_as", role),
        "is_hidden": bool(state.get("is_hidden_by_gravedigger", False)),
        "will": str(state.get("will", "") or ""), "custom_message": custom_message,
        "voters": list(voters), "announcement_id": None, "delivered": False, "converted": [],
        "notice_id": hashlib.sha256(f"{game.game_key}:{player_id}".encode()).hexdigest()[:16],
    }
    game.living_players = [p for p in game.living_players if p.id != player_id]
    state.setdefault("death_cause", cause)
    state.setdefault("died_day", game.day_number)
    if not any(str(e.get("player_id")) == str(player_id) for e in game.graveyard if isinstance(e, dict)):
        game.graveyard.append({"player_id": player_id, "real_role": role,
                               "died_day": game.day_number, "cause": cause,
                               "is_hidden": receipt["is_hidden"], "used_by_retri": False})
    if role == "Jester" and cause == "lynch":
        state.update(can_haunt=True, jester_won=True, guilty_voters=list(voters))
    for pid, other in game.role_states.items():
        if pid != player_id and pid in living_ids and game.player_roles.get(pid) == "Executioner" and other.get("exe_target") == player_id:
            if cause == "lynch":
                other["exe_won"] = True
            else:
                game.player_roles[pid] = "Jester"
                receipt["converted"].append(pid)
    receipts[str(player_id)] = receipt
    from death_side_effects import apply_ga_defeat_on_bind_death
    game.record_cycle_death(1)
    apply_ga_defeat_on_bind_death(game, player_id, cause=cause)
    return receipt


def death_text(receipt) -> str:
    pid = receipt["player_id"]
    text = receipt.get("custom_message") or f"**<@{pid}>** was found dead."
    if receipt["is_hidden"]:
        text += "\n\nTheir role was obscured by a Gravedigger."
    else:
        text += f"\n\nIt was discovered that <@{pid}>'s role was **{receipt['revealed_role']}**."
    will = receipt.get("will", "").replace("```", "'''").strip()[:1800]
    if will:
        text += f"\n\n**Last Will:**\n```{will}```"
    return text
