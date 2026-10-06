"""Day-phase behavior: tribunal lynch, Mayor reveal, Deputy revolver."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from scripts.monte_carlo.config import (
    LYNCH_PROB_CAP,
    LYNCH_PROB_PER_SUSPICION,
    TOWN,
    _competence_for_axis,
    _pick_with_competence,
    mc_pick_from,
    mc_roll,
)
from scripts.monte_carlo.state import Player, faction, town_lynch_decision, town_lynch_decision_desperate


@dataclass
class DayPhaseState:
    """Mirrors a subset of Game day fields used by the sim."""

    deputy_revolver_fired: bool = False
    vote_in_progress: bool = False


def reset_day_phase(state: DayPhaseState) -> None:
    state.deputy_revolver_fired = False
    state.vote_in_progress = False


def _vote_guilty_probability(
    players: List[Player],
    voter_id: int,
    defendant_id: int,
    suspect_score: int,
) -> float:
    """Per-voter tribunal guilty probability (faction-aware average pub behavior)."""
    role = players[voter_id].role
    voter_fac = faction(role)
    def_fac = faction(players[defendant_id].role)
    c = _competence_for_axis(role, "day")
    if voter_fac == "Mafia":
        if def_fac == "Mafia":
            return max(0.02, 0.10 * (1.0 - c))
        return min(0.60, 0.16 + 0.07 * suspect_score) * c + 0.14 * (1.0 - c)
    if voter_fac == "Town":
        return min(0.85, 0.18 + 0.11 * suspect_score) * c + 0.26 * (1.0 - c)
    return min(0.55, 0.22 + 0.06 * suspect_score * c)


def _vote_innocent_probability(
    players: List[Player],
    voter_id: int,
    defendant_id: int,
    suspect_score: int,
) -> float:
    """Per-voter tribunal innocent (❌) probability — complement to guilty/abstain split."""
    role = players[voter_id].role
    voter_fac = faction(role)
    def_fac = faction(players[defendant_id].role)
    c = _competence_for_axis(role, "day")
    slack = max(0, 8 - suspect_score)
    if voter_fac == "Mafia":
        if def_fac == "Mafia":
            return min(0.92, 0.78 + 0.12 * c)
        return min(0.58, 0.14 + 0.05 * slack) * c + 0.22 * (1.0 - c)
    if voter_fac == "Town":
        if def_fac == "Town":
            return min(0.82, 0.22 + 0.09 * slack) * c + 0.28 * (1.0 - c)
        return min(0.52, 0.12 + 0.04 * slack) * c + 0.20 * (1.0 - c)
    return min(0.52, 0.16 + 0.05 * slack * c)


def infer_guilty_voters(
    players: List[Player],
    alive: Set[int],
    defendant_id: int,
    evidence: Dict[int, int],
    *,
    mayor_id: Optional[int],
    mayor_revealed: bool,
) -> List[int]:
    """Tribunal voters who voted guilty (✅) — used for guilty vote weights."""
    del mayor_id, mayor_revealed
    suspect_score = evidence.get(defendant_id, 0)
    guilty: List[int] = []
    for pid in alive:
        if pid == defendant_id:
            continue
        p_guilty = _vote_guilty_probability(players, pid, defendant_id, suspect_score)
        if mc_roll(p_guilty):
            guilty.append(pid)
    if not guilty:
        others = [x for x in alive if x != defendant_id]
        if others:
            guilty.append(mc_pick_from(others))
    return guilty


def infer_eligible_haunt_voters(
    players: List[Player],
    alive: Set[int],
    defendant_id: int,
    evidence: Dict[int, int],
    *,
    mayor_id: Optional[int],
    mayor_revealed: bool,
) -> List[int]:
    """
    Jester haunt pool (prod ``_build_eligible_haunt_voters``): guilty or abstain, not innocent.
    """
    del mayor_id, mayor_revealed
    suspect_score = evidence.get(defendant_id, 0)
    eligible: List[int] = []
    for pid in alive:
        if pid == defendant_id:
            continue
        if mc_roll(_vote_guilty_probability(players, pid, defendant_id, suspect_score)):
            eligible.append(pid)
            continue
        if mc_roll(_vote_innocent_probability(players, pid, defendant_id, suspect_score)):
            continue
        eligible.append(pid)
    if not eligible:
        others = [x for x in alive if x != defendant_id]
        if others:
            eligible.append(mc_pick_from(others))
    return eligible


def _town_competence_day(players: List[Player], alive: Set[int]) -> float:
    town_ids = [pid for pid in alive if players[pid].role in TOWN]
    if not town_ids:
        return 0.60
    vals = [_competence_for_axis(players[pid].role, "day") for pid in town_ids]
    return sum(vals) / len(vals)


def tribunal_vote_weights(
    players: List[Player],
    alive: Set[int],
    defendant_id: int,
    evidence: Dict[int, int],
    *,
    mayor_id: Optional[int],
) -> Tuple[int, int]:
    """Return (guilty_weight, innocent_weight) like bot tribunal judgment."""
    guilty_voters = infer_guilty_voters(
        players,
        alive,
        defendant_id,
        evidence,
        mayor_id=mayor_id,
        mayor_revealed=mayor_id is not None and players[mayor_id].mayor_revealed,
    )
    guilty_w = 0
    innocent_w = 0
    for pid in alive:
        if pid == defendant_id:
            continue
        weight = 2 if (mayor_id == pid and players[pid].mayor_revealed) else 1
        if pid in guilty_voters:
            guilty_w += weight
        else:
            innocent_w += weight
    return guilty_w, innocent_w


def _top_suspect(alive: Set[int], evidence: Dict[int, int]) -> Tuple[Optional[int], int, int]:
    if not alive:
        return None, 0, 0
    scored = [(evidence.get(pid, 0), pid) for pid in alive]
    scored.sort(reverse=True)
    top_score, top_pid = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    return top_pid, top_score, second_score


def lynch_attempt_probability(suspicion: int) -> float:
    """0 suspicion => 0%; each point adds LYNCH_PROB_PER_SUSPICION up to cap."""
    if suspicion <= 0:
        return 0.0
    return min(LYNCH_PROB_CAP, float(suspicion) * LYNCH_PROB_PER_SUSPICION)


def pick_lynch_defendant(
    players: List[Player],
    alive: Set[int],
    evidence: Dict[int, int],
    *,
    mayor_id: Optional[int],
    no_lynch_streak: int,
    force: bool = False,
) -> Optional[int]:
    """
    Lynch chance scales with top suspect's suspicion (0 => skip day).
    Requires a unique top suspect; then tribunal guilty vs innocent (no random nominee).
    """
    del no_lynch_streak, force
    top_pid, top_score, second_score = _top_suspect(alive, evidence)
    if top_pid is None or top_score <= 0:
        return None
    if top_score <= second_score:
        return None
    if not mc_roll(lynch_attempt_probability(top_score)):
        return None
    guilty_w, innocent_w = tribunal_vote_weights(
        players, alive, top_pid, evidence, mayor_id=mayor_id
    )
    if guilty_w <= innocent_w:
        return None
    return top_pid


def pick_alive_simple(players: List[Player], alive: Set[int], *, exclude: Set[int] | None = None) -> Optional[int]:
    ex = exclude or set()
    choices = [pid for pid in alive if pid not in ex]
    return mc_pick_from(choices) if choices else None


def mayor_may_reveal(
    players: List[Player],
    mayor_id: int,
    alive: Set[int],
    evidence: Dict[int, int],
    day: int,
) -> bool:
    """Player-driven reveal heuristic (!reveal during day when case is strong)."""
    if not players[mayor_id].alive or players[mayor_id].mayor_revealed:
        return False
    top = max((evidence.get(pid, 0) for pid in alive if pid != mayor_id), default=0)
    if day < 2:
        return False
    pressure = (day >= 3 or top >= 3) and (day >= 4 or top >= 4 or mc_roll(0.35))
    if not pressure:
        return False
    return mc_roll(_competence_for_axis("Mayor", "day"))


def deputy_should_shoot_today(day: int, *, already_fired: bool, shots_left: int) -> bool:
    if day < 2 or already_fired or shots_left <= 0:
        return False
    return mc_roll(0.22 * _competence_for_axis("Deputy", "usage") + 0.08)


def deputy_pick_target(
    players: List[Player],
    alive: Set[int],
    deputy_id: int,
    evidence: Dict[int, int],
) -> Optional[int]:
    good = town_lynch_decision(players, alive, evidence)
    rnd = pick_alive_simple(players, alive, exclude={deputy_id})
    return _pick_with_competence(
        competence=_competence_for_axis("Deputy", "targeting"),
        good_choice=good,
        random_choice=rnd,
    )


@dataclass
class PendingJesterHaunt:
    jester_id: int
    guilty_voters: List[int] = field(default_factory=list)  # eligible haunt pool (guilty + abstain)


def pick_haunt_target(
    players: List[Player],
    alive: Set[int],
    pending: PendingJesterHaunt,
) -> Optional[int]:
    """Mirror gm.py haunt fallback: guilty voter who is still alive."""
    by_id = {p.i: p for p in players}
    eligible = [vid for vid in pending.guilty_voters if vid in alive and vid in by_id]
    if not eligible:
        return None
    non_town = [pid for pid in eligible if faction(by_id[pid].role) != "Town"]
    good = random.choice(non_town) if non_town else random.choice(eligible)
    rnd = random.choice(eligible)
    return _pick_with_competence(
        competence=_competence_for_axis("Jester", "day"),
        good_choice=good,
        random_choice=rnd,
    )
