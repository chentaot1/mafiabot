"""Single source of truth for endgame win/delta computation (SQLite + JSON mirror)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AbstractSet, Any, Dict, List, Mapping, MutableMapping, Optional, Set

from config import ALL_MAFIA_ROLES, TOWN_ROLES, WITCH_TOWN_LOSES_OUTCOMES
from faction_taxonomy import role_endgame_faction_bucket
from draw_override_wins import (
    build_draw_override_outcome_flags,
    draw_override_winner_ids,
    is_draw_override_outcome,
    player_counts_as_loss_on_draw,
)
from guardian_angel_wins import (
    build_outcome_flags_for_game,
    guardian_angel_joint_win,
    guardian_angel_personal_win,
)
from stats_personal import apply_personal_win_delta, migrate_personal_wins_dict


def role_to_faction_bucket(role: str) -> str:
    return role_endgame_faction_bucket(role)


def json_faction_win_bucket(*, town_win: bool, mafia_win: bool, arso_win: bool) -> Optional[str]:
    """Faction win keys stored in JSON faction_wins (aligns with SQLite wins_* columns)."""
    if town_win:
        return "Town"
    if mafia_win:
        return "Mafia"
    if arso_win:
        return "Arsonist"
    return None


@dataclass
class PlayerEndgameDelta:
    player_id: int
    role: str
    role_start: str
    faction_start: str
    faction_end: str
    alive: bool
    did_win: bool
    is_loss: bool
    is_draw: bool
    wins_town: int = 0
    wins_mafia: int = 0
    wins_arsonist: int = 0
    personal_deltas: Dict[str, int] = field(default_factory=dict)
    death_cause: Optional[str] = None
    died_day: Optional[int] = None


def compute_player_endgame_deltas(
    *,
    player_roles: Mapping[int, str],
    role_states: Mapping[int, Mapping[str, Any]],
    living_ids: AbstractSet[int],
    outcome_norm: str,
) -> List[PlayerEndgameDelta]:
    """Compute per-player endgame deltas once for SQLite commit and JSON mirror."""
    is_draw = outcome_norm == "Draw"
    living_set = {int(x) for x in living_ids}
    stalemate_override = is_draw_override_outcome(outcome_norm)
    override_winner_set = (
        draw_override_winner_ids(player_roles, role_states, living_set)
        if stalemate_override
        else frozenset()
    )
    if stalemate_override:
        endgame_outcome_flags = build_draw_override_outcome_flags(
            player_roles, role_states, living_set
        )
    else:
        endgame_outcome_flags = build_outcome_flags_for_game(
            player_roles,
            role_states,
            living_set,
            outcome_norm,
        )

    deltas: List[PlayerEndgameDelta] = []
    for pid in player_roles.keys():
        ipid = int(pid)
        role = str(player_roles.get(pid, "Unknown"))
        role_state = role_states.get(pid, {}) or {}
        role_start = str(role_state.get("role_start") or role)
        alive = ipid in living_set

        pirate_win = role == "Pirate" and int(role_state.get("wins", 0)) >= 2
        exe_win = role == "Executioner" and bool(role_state.get("exe_won"))
        jester_win = role == "Jester" and bool(role_state.get("jester_won"))
        survivor_win = role == "Survivor" and alive
        chaos_win = role == "Chaos" and alive
        witch_win = role == "Witch" and outcome_norm in WITCH_TOWN_LOSES_OUTCOMES and alive
        sk_win = outcome_norm == "Serial Killer" and role == "Serial Killer" and alive
        town_win = outcome_norm == "Town" and role in TOWN_ROLES
        mafia_win = outcome_norm == "Mafia" and role in ALL_MAFIA_ROLES
        arso_win = outcome_norm == "Arsonist" and role == "Arsonist" and alive

        bind_raw = role_state.get("ga_target_id")
        try:
            bind_id = int(bind_raw) if bind_raw is not None else None
        except (TypeError, ValueError):
            bind_id = None
        ga_joint = False
        if role == "Guardian Angel":
            bind_role_for_ga = player_roles.get(bind_id) if bind_id is not None else None
            ga_joint = guardian_angel_joint_win(
                ga_alive=alive,
                ga_defeated=bool(role_state.get("ga_defeated")),
                bind_id=bind_id,
                bind_role=bind_role_for_ga,
                living_ids=living_set,
                outcome_flags=endgame_outcome_flags,
                bind_role_state=role_states.get(bind_id, {}) if bind_id is not None else None,
                bind_pirate_wins=int(role_states.get(bind_id, {}).get("wins", 0))
                if bind_id is not None and bind_role_for_ga == "Pirate"
                else None,
                bind_exe_won=bool(role_states.get(bind_id, {}).get("exe_won"))
                if bind_id is not None and bind_role_for_ga == "Executioner"
                else None,
                bind_jester_won=bool(role_states.get(bind_id, {}).get("jester_won"))
                if bind_id is not None and bind_role_for_ga == "Jester"
                else None,
            )
        ga_personal = guardian_angel_personal_win(
            role=role,
            player_id=ipid,
            ga_alive=alive,
            ga_defeated=bool(role_state.get("ga_defeated")),
            ga_joint_win=ga_joint,
            stalemate_override=stalemate_override,
            override_winner_ids=override_winner_set,
        )

        did_win = False
        if stalemate_override:
            did_win = ipid in override_winner_set
        elif not is_draw:
            did_win = any(
                [
                    pirate_win,
                    exe_win,
                    jester_win,
                    survivor_win,
                    chaos_win,
                    witch_win,
                    ga_joint,
                    sk_win,
                    town_win,
                    mafia_win,
                    arso_win,
                ]
            )

        is_loss = player_counts_as_loss_on_draw(role=role, did_win=did_win, is_draw=is_draw)

        personal: Dict[str, int] = {}
        if pirate_win:
            personal["pirate_win"] = 1
        if exe_win:
            personal["exe_win"] = 1
        if jester_win:
            personal["jester_win"] = 1
        if survivor_win:
            personal["survivor_survived"] = 1
        if chaos_win:
            personal["chaos_survived"] = 1
        if witch_win:
            personal["witch_town_loses"] = 1
        if ga_personal:
            personal["guardian_angel_win"] = 1
        if sk_win:
            personal["serial_killer_win"] = 1
        if arso_win:
            personal["arsonist_win"] = 1

        death_cause = role_state.get("death_cause")
        died_day_raw = role_state.get("died_day")

        deltas.append(
            PlayerEndgameDelta(
                player_id=ipid,
                role=role,
                role_start=role_start,
                faction_start=role_to_faction_bucket(role_start),
                faction_end=role_to_faction_bucket(role),
                alive=alive,
                did_win=did_win,
                is_loss=is_loss,
                is_draw=is_draw,
                wins_town=1 if town_win else 0,
                wins_mafia=1 if mafia_win else 0,
                wins_arsonist=1 if arso_win else 0,
                personal_deltas=personal,
                death_cause=str(death_cause) if death_cause is not None else None,
                died_day=int(died_day_raw) if died_day_raw is not None else None,
            )
        )
    return deltas


def apply_deltas_to_json_players(
    players: MutableMapping[str, MutableMapping[str, Any]],
    deltas: List[PlayerEndgameDelta],
) -> None:
    """Apply precomputed deltas to JSON stats players dict (mirror of SQLite)."""
    for d in deltas:
        rec = players.setdefault(str(d.player_id), {})
        rec["games_played"] = int(rec.get("games_played", 0)) + 1
        rec["wins"] = int(rec.get("wins", 0)) + (1 if d.did_win else 0)
        rec["losses"] = int(rec.get("losses", 0)) + (1 if d.is_loss else 0)
        rec["draws"] = int(rec.get("draws", 0)) + (1 if d.is_draw else 0)

        role_played = rec.setdefault("role_played", {})
        role_played[d.role] = int(role_played.get(d.role, 0)) + 1
        role_wins = rec.setdefault("role_wins", {})
        role_wins[d.role] = int(role_wins.get(d.role, 0)) + (1 if d.did_win else 0)

        faction_played = rec.setdefault("faction_played", {})
        fp_key = role_to_faction_bucket(d.role)
        faction_played[fp_key] = int(faction_played.get(fp_key, 0)) + 1

        faction_wins = rec.setdefault("faction_wins", {})
        fw_key = json_faction_win_bucket(
            town_win=bool(d.wins_town),
            mafia_win=bool(d.wins_mafia),
            arso_win=bool(d.wins_arsonist),
        )
        if fw_key and d.did_win:
            faction_wins[fw_key] = int(faction_wins.get(fw_key, 0)) + 1

        personal = migrate_personal_wins_dict(rec.get("personal_wins"))
        for key, delta in d.personal_deltas.items():
            apply_personal_win_delta(personal, key, delta)
        rec["personal_wins"] = personal


def deltas_to_sqlite_payloads(
    deltas: List[PlayerEndgameDelta],
    *,
    ended_at: str,
) -> tuple[
    List[Dict[str, object]],
    List[Dict[str, object]],
    List[Dict[str, object]],
    List[Dict[str, object]],
]:
    """Convert deltas to commit_endgame_atomic row/delta dicts."""
    game_rows: List[Dict[str, object]] = []
    player_stats_deltas: List[Dict[str, object]] = []
    role_stats_deltas: List[Dict[str, object]] = []
    personal_win_deltas: List[Dict[str, object]] = []

    for d in deltas:
        game_rows.append(
            {
                "player_id": d.player_id,
                "role_start": d.role_start,
                "role_end": d.role,
                "faction_start": d.faction_start,
                "faction_end": d.faction_end,
                "survived": 1 if d.alive else 0,
                "died_day": d.died_day,
                "death_cause": d.death_cause,
            }
        )
        player_stats_deltas.append(
            {
                "player_id": d.player_id,
                "games_played": 1,
                "wins_total": 1 if d.did_win else 0,
                "losses_total": 1 if d.is_loss else 0,
                "draws_total": 1 if d.is_draw else 0,
                "wins_town": d.wins_town,
                "wins_mafia": d.wins_mafia,
                "wins_arsonist": d.wins_arsonist,
                "last_game_at": ended_at,
            }
        )
        role_stats_deltas.append(
            {
                "player_id": d.player_id,
                "role": d.role,
                "played": 1,
                "wins_total": 1 if d.did_win else 0,
                "losses_total": 1 if d.is_loss else 0,
            }
        )
        for key, delta in d.personal_deltas.items():
            if delta:
                personal_win_deltas.append(
                    {"player_id": d.player_id, "key": key, "delta": int(delta)}
                )

    return game_rows, player_stats_deltas, role_stats_deltas, personal_win_deltas
