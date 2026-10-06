"""Player state and day/night heuristics."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import config as bot_config
from stalemate_wins import NEUTRAL_KILLING_ROLES, lookup_two_player_stalemate

from scripts.monte_carlo.config import (
    Action,
    GATEKEEPER_BLOCKS_ONE,
    MAFIA,
    NEUTRAL,
    ROLEBLOCK_IMMUNE,
    TOWN,
    _competence_for_axis,
    _pick_with_competence,
    mc_action_jitter,
    mc_pick_from,
    mc_roll,
    SEER_HOSTILE_NEUTRAL_ROLES,
    SEER_NEUTRAL_KILLING_ROLES,
    SEER_FRIENDLY_EXTRA,
    PSYCHIC_ODD_EVIL_NEUTRALS,
    DEPUTY_GUN_EVIL_NEUTRALS,
)


def first_living_with_role(players, alive: Set[int], role: str) -> Optional[int]:
    """Deterministic first seat (lowest pid) with ``role`` among living players."""
    for pid in sorted(alive):
        if players[pid].role == role:
            return pid
    return None

@dataclass
class Player:
    i: int
    role: str
    alive: bool = True
    # Generic per-role state (kept intentionally small; enough to model abilities + wincons)
    vests_left: int = 0  # Survivor
    shots_left: int = 0  # Vigilante
    self_heals_left: int = 0  # Doctor
    bg_uses_left: int = 0  # Bodyguard
    bg_self_protects_left: int = 0
    alerts_left: int = 0  # Scary Grandma
    mayor_revealed: bool = False  # Mayor
    gatekeeper_uses_left: int = 0
    gatekeeper_last_guard_target_id: Optional[int] = None
    gatekeeper_last_successful_guard_day_number: Optional[int] = None
    gravedigger_uses_left: int = 0
    mole_uses_left: int = 0
    tailor_uses_left: int = 0
    pirate_wins: int = 0
    witch_shield_used: bool = False  # only used for light modeling
    chaos_shield_used: bool = False  # Night 1 vs normal kills (mirrors Witch)
    jester_shield_used: bool = False
    guilty_next_day: bool = False  # Vigilante guilt (dies next day if shot killed Town)
    retri_uses_left: int = 0
    chaos_uses_left: int = 0
    deputy_shots_remaining: int = 0
    ga_ward_charges: int = 0
    ga_bind_id: Optional[int] = None
    ga_trial_lock_day: Optional[int] = None  # bind cannot be nominated this day (live parity)
    ga_defeated: bool = False
    ga_shield_active_tonight: bool = False
    sk_cautious: bool = False
    sk_suppressed_by_pirate: bool = False
    seer_gazed_pairs: Set[frozenset[int]] = field(default_factory=set)
    seer_self_gaze_slots: Set[int] = field(default_factory=set)
    is_vested: bool = False  # Survivor vest (role_states is_vested; used for Deputy defense check)

    # Night flags
    framed_tonight: bool = False  # Framer
    roleblocked_tonight: bool = False  # Escort/Consort/Gatekeeper
    protected_tonight: bool = False  # Doctor/Bodyguard/Survivor vest
    on_alert_tonight: bool = False  # Scary Grandma
    transported_with: Optional[int] = None  # Transporter swap partner
    tailored_as: Optional[str] = None  # Tailor disguise (death reveal only; live Seer ignores)

    # Neutral win tracking
    exe_target: Optional[int] = None  # Executioner only
    exe_won: bool = False
    jester_won: bool = False
    survivor_won: bool = False
    pirate_won: bool = False
    arsonist_won: bool = False
    # Arsonist douse tracking (global list is easier, but keep flag too)
    doused: bool = False


def _mafia_ids(players: List[Player], alive: Set[int]) -> List[int]:
    return [pid for pid in alive if players[pid].role in MAFIA]


def _mafia_killer_id(players: List[Player], alive: Set[int]) -> Optional[int]:
    """Only Mobster submits the faction kill in the live bot (night_factory 'kill')."""
    mobsters = sorted(pid for pid in alive if players[pid].role == "Mobster")
    if mobsters:
        return mobsters[0]
    mafia_alive = _mafia_ids(players, alive)
    return mafia_alive[0] if mafia_alive else None


def _apply_transport_to_id(x: Optional[int], swap: Optional[Tuple[int, int]]) -> Optional[int]:
    if x is None or swap is None:
        return x
    a, b = swap
    if x == a:
        return b
    if x == b:
        return a
    return x


def _seer_bucket(apparent: str) -> int:
    if apparent in SEER_HOSTILE_NEUTRAL_ROLES:
        return 4
    if apparent in SEER_NEUTRAL_KILLING_ROLES:
        return 3
    if apparent in MAFIA:
        return 2
    if apparent in TOWN or apparent in SEER_FRIENDLY_EXTRA:
        return 1
    return 4


def _seer_apparent_role_for_player(players: List["Player"], pid: int) -> str:
    p = players[pid]
    if p.framed_tonight:
        return "Framer"
    return p.role


def _seer_bucket_for_player(players: List["Player"], pid: int, *, doused: Set[int]) -> int:
    p = players[pid]
    if p.framed_tonight:
        return 2
    if pid in doused:
        if p.role == "Arsonist":
            return _seer_bucket("Arsonist")
        return 2
    return _seer_bucket(_seer_apparent_role_for_player(players, pid))


def _deputy_sees_evil(players: List[Player], pid: int, *, doused: Set[int]) -> bool:
    r = players[pid].role
    if r in MAFIA:
        return True
    if r in DEPUTY_GUN_EVIL_NEUTRALS:
        return True
    if players[pid].framed_tonight:
        return True
    return pid in doused


def _psychic_apply_vision(
    players: List[Player],
    alive: Set[int],
    psychic_id: int,
    day: int,
    evidence: Dict[int, int],
    *,
    doused: Set[int],
) -> None:
    from faction_taxonomy import (
        psychic_even_night_good_role,
        psychic_odd_evil_roles,
        psychic_vision_living_too_small,
        psychic_vision_pool_too_small_even,
        psychic_vision_pool_too_small_odd,
    )

    if psychic_vision_living_too_small(len(alive)):
        return
    pool = alive - {psychic_id}
    if day % 2 == 1:
        evil_pool = [
            pid
            for pid in pool
            if (
                players[pid].role in MAFIA
                or players[pid].role in psychic_odd_evil_roles()
                or players[pid].framed_tonight
                or pid in doused
            )
        ]
        if evil_pool and not psychic_vision_pool_too_small_odd(len(pool)):
            e = mc_pick_from(evil_pool)
            if e is not None:
                evidence[e] = evidence.get(e, 0) + 2
    else:
        good_pool = [
            pid for pid in pool if psychic_even_night_good_role(players[pid].role)
        ]
        if good_pool and not psychic_vision_pool_too_small_even(len(pool)):
            g = mc_pick_from(good_pool)
            if g is None:
                return
            evidence[g] = evidence.get(g, 0) + 1


def faction(role: str) -> str:
    if role in MAFIA:
        return "Mafia"
    if role in TOWN:
        return "Town"
    if role in NEUTRAL:
        return "Neutral"
    return "Unknown"


def _init_role_state(p: Player, *, player_count: int) -> None:
    """Initialize per-role counters/flags for every live-bot role."""
    r = p.role
    two_charge = bot_config.role_starting_charges(player_count=player_count, full_charges=2)
    if r == "Survivor":
        p.vests_left = two_charge
    elif r == "Vigilante":
        p.shots_left = 1
    elif r == "Doctor":
        p.self_heals_left = 1
    elif r == "Bodyguard":
        p.bg_uses_left = 1
        p.bg_self_protects_left = 1
    elif r == "Scary Grandma":
        p.alerts_left = two_charge
    elif r == "Gatekeeper":
        p.gatekeeper_uses_left = two_charge
    elif r == "Gravedigger":
        p.gravedigger_uses_left = 1
    elif r == "Mole":
        p.mole_uses_left = 1
    elif r == "Tailor":
        p.tailor_uses_left = 1
    elif r == "Retributionist":
        p.retri_uses_left = two_charge
    elif r == "Chaos":
        p.chaos_uses_left = bot_config.chaos_starting_uses(player_count)
    elif r == "Deputy":
        p.deputy_shots_remaining = 1
    elif r == "Serial Killer":
        p.sk_cautious = False
    elif r == "Pirate":
        p.pirate_wins = 0
    elif r == "Guardian Angel":
        p.ga_ward_charges = 0  # set after bind assignment
    elif r in {
        "Mobster",
        "Framer",
        "Consort",
        "Hypnotist",
        "Sheriff",
        "Investigator",
        "Lookout",
        "Tracker",
        "Escort",
        "Transporter",
        "Mayor",
        "Psychic",
        "Seer",
        "Executioner",
        "Jester",
        "Witch",
        "Arsonist",
    }:
        pass
    else:
        raise ValueError(f"Uninitialized role in monte carlo sim: {r!r}")


def pick_alive(players: List[Player], alive: Set[int], *, exclude: Set[int] | None = None) -> Optional[int]:
    ex = exclude or set()
    choices = [pid for pid in alive if pid not in ex]
    return mc_pick_from(choices) if choices else None


def _likely_mafia_kill_target(players: List[Player], alive: Set[int]) -> Optional[int]:
    """Heuristic kill target before Mobster submits (omniscient sim AI)."""
    mafia_killer = _mafia_killer_id(players, alive)
    if mafia_killer is None:
        return None
    candidates = [pid for pid in alive if pid != mafia_killer]
    if not candidates:
        return None
    best = max(score_kill_target(players, pid) for pid in candidates)
    top = [pid for pid in candidates if score_kill_target(players, pid) == best]
    return random.choice(top) if top else None


def pick_transporter_pair(
    players: List[Player],
    alive: Set[int],
    actor_id: int,
) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """Good pair swaps likely kill target with mafia killer; random pair is uniform."""
    rnd_a = pick_alive(players, alive, exclude={actor_id})
    rnd_b = pick_alive(players, alive, exclude={actor_id, rnd_a} if rnd_a is not None else {actor_id})
    mafia_killer = _mafia_killer_id(players, alive)
    good_a = _likely_mafia_kill_target(players, alive)
    good_b = mafia_killer if mafia_killer is not None and mafia_killer != good_a else None
    if good_a is None:
        good_a = pick_alive(players, alive, exclude={actor_id})
    if good_b is None or good_b == good_a:
        good_b = pick_alive(players, alive, exclude={actor_id, good_a} if good_a is not None else {actor_id})
    return good_a, good_b, rnd_a, rnd_b


def pick_chaos_pair(
    players: List[Player],
    alive: Set[int],
    actor_id: int,
) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """Good pair pairs a mafia member with a high-priority town slot when possible."""
    mafia = [pid for pid in alive if players[pid].role in MAFIA and pid != actor_id]
    town_prio = sorted(
        (pid for pid in alive if pid != actor_id and faction(players[pid].role) == "Town"),
        key=lambda p: score_kill_target(players, p),
        reverse=True,
    )
    good1 = town_prio[0] if town_prio else pick_alive(players, alive, exclude={actor_id})
    good2 = mafia[0] if mafia else pick_alive(players, alive, exclude={actor_id, good1} if good1 else {actor_id})
    rnd1 = pick_alive(players, alive, exclude={actor_id})
    rnd2 = pick_alive(players, alive, exclude={actor_id, rnd1} if rnd1 else {actor_id})
    return good1, good2, rnd1, rnd2


def pick_seer_gaze_pair(
    players: List[Player],
    alive: Set[int],
    seer_id: int,
    doused: Set[int],
    *,
    used_pairs: Set[frozenset[int]],
) -> Optional[Tuple[int, int]]:
    """Prefer cross-bucket (Friends/Enemies) pairs when targeting competence succeeds."""
    candidates = [
        x
        for x in alive
        if x != seer_id
        and not (players[x].role == "Mayor" and players[x].mayor_revealed)
    ]
    if len(candidates) < 2:
        return None
    all_pairs: List[Tuple[int, int]] = []
    good_pairs: List[Tuple[int, int]] = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            pair = frozenset({a, b})
            if pair in used_pairs:
                continue
            all_pairs.append((a, b))
            ba = _seer_bucket_for_player(players, a, doused=doused)
            bb = _seer_bucket_for_player(players, b, doused=doused)
            if ba != bb or ba == 4 or bb == 4:
                good_pairs.append((a, b))
    if not all_pairs:
        return None
    competence = _competence_for_axis("Seer", "targeting")
    if mc_action_jitter():
        pool = all_pairs
    else:
        pool = good_pairs if good_pairs and random.random() < competence else all_pairs
    return random.choice(pool)


def pirate_pick_plunder_target(players: List[Player], alive: Set[int], pirate_id: int) -> Optional[int]:
    candidates = [pid for pid in alive if pid != pirate_id]
    if not candidates:
        return None
    best = max(score_kill_target(players, pid) for pid in candidates)
    top = [pid for pid in candidates if score_kill_target(players, pid) == best]
    good = random.choice(top) if top else None
    rnd = random.choice(candidates)
    return _pick_with_competence(
        competence=_competence_for_axis("Pirate", "targeting"),
        good_choice=good,
        random_choice=rnd,
    )


def pirate_duel_won() -> bool:
    """Usage-axis duel success (was flat 50%)."""
    c = _competence_for_axis("Pirate", "usage")
    return mc_roll(0.28 + 0.52 * c)


def scary_grandma_should_alert(players: List[Player]) -> bool:
    """Usage-axis alert timing (replaces flat act-roll)."""
    alive_count = sum(1 for x in players if x.alive)
    c = _competence_for_axis("Scary Grandma", "usage")
    if alive_count <= 6:
        return mc_roll(0.35 + 0.45 * c)
    return mc_roll(0.10 + 0.35 * c)


def arsonist_should_ignite(
    *,
    living_doused: List[int],
    others_alive: int,
    total_doused: int,
) -> bool:
    """Usage-axis ignite timing (1v1 doused still always ignites)."""
    if others_alive == 1 and living_doused:
        return True
    c = _competence_for_axis("Arsonist", "usage")
    ignite_alive_cap = max(3, 5 - int(c * 2))
    if living_doused and others_alive <= ignite_alive_cap:
        return True
    doused_threshold = max(2, 4 - int(c * 1.5))
    return total_doused >= doused_threshold


def pick_priority_role_target(
    players: List[Player],
    alive: Set[int],
    actor_id: int,
    role: str,
    prefer: Tuple[str, ...],
) -> Optional[int]:
    """Role action on a meta priority target vs random (scaled by role competence)."""
    good: Optional[int] = None
    for prefer_role in prefer:
        good = first_living_with_role(players, alive, prefer_role)
        if good is not None:
            break
    if good is None:
        good = pick_alive(players, alive, exclude={actor_id})
    rnd = pick_alive(players, alive, exclude={actor_id})
    return _pick_with_competence(
        competence=_competence_for_axis(role, "targeting"),
        good_choice=good,
        random_choice=rnd,
    )


def pick_investigation_target(
    players: List[Player],
    alive: Set[int],
    actor_id: int,
    evidence: Dict[int, int],
    role: str,
) -> Optional[int]:
    """Investigate top suspect from day evidence when competent; else random."""
    good = town_lynch_decision(players, alive, evidence)
    if good is None or good == actor_id:
        good = town_lynch_decision_desperate(players, alive, evidence)
    if good is None or good == actor_id:
        good = pick_alive(players, alive, exclude={actor_id})
    rnd = pick_alive(players, alive, exclude={actor_id})
    return _pick_with_competence(
        competence=_competence_for_axis(role, "targeting"),
        good_choice=good,
        random_choice=rnd,
    )


def pick_mole_reveal_target(
    players: List[Player],
    alive: Set[int],
    actor_id: int,
    evidence: Dict[int, int],
) -> Optional[int]:
    """Mole reveal: avoid teammates; prefer high-value town reads when competent."""
    non_mafia = [pid for pid in alive if pid != actor_id and players[pid].role not in MAFIA]
    if not non_mafia:
        return None
    good = town_lynch_decision(players, alive, evidence)
    if good is None or good == actor_id or good not in non_mafia:
        good = None
        for prefer in ("Sheriff", "Doctor", "Investigator", "Mayor", "Vigilante"):
            hit = first_living_with_role(players, set(non_mafia), prefer)
            if hit is not None:
                good = hit
                break
        if good is None:
            good = random.choice(non_mafia)
    rnd = random.choice(non_mafia)
    return _pick_with_competence(
        competence=_competence_for_axis("Mole", "targeting"),
        good_choice=good,
        random_choice=rnd,
    )


def pick_watch_target(players: List[Player], alive: Set[int], lookout_id: int) -> int:
    good = lookout_watch(players, alive, lookout_id)
    rnd = pick_alive(players, alive, exclude={lookout_id}) or lookout_id
    picked = _pick_with_competence(
        competence=_competence_for_axis("Lookout", "targeting"),
        good_choice=good,
        random_choice=rnd,
    )
    return picked if picked is not None else lookout_id


def pick_track_target(
    players: List[Player],
    alive: Set[int],
    tracker_id: int,
    evidence: Dict[int, int],
) -> int:
    good = tracker_track(players, alive, tracker_id, evidence)
    rnd = pick_alive(players, alive, exclude={tracker_id}) or tracker_id
    picked = _pick_with_competence(
        competence=_competence_for_axis("Tracker", "targeting"),
        good_choice=good,
        random_choice=rnd,
    )
    return picked if picked is not None else tracker_id


def score_kill_target(players: List[Player], pid: int) -> int:
    """Mafia prefers killing info/power roles."""
    r = players[pid].role
    if r in {"Lookout", "Tracker", "Sheriff", "Investigator", "Psychic", "Seer"}:
        return 5
    if r in {"Doctor", "Escort", "Vigilante", "Deputy", "Scary Grandma", "Gatekeeper", "Mole"}:
        return 4
    if r in {"Mayor", "Transporter", "Bodyguard", "Retributionist", "Gravedigger", "Hypnotist", "Tailor"}:
        return 3
    if r in {"Consort", "Framer"}:
        return 2
    # neutrals are usually not Mafia priority early, but can be.
    return 1


def mafia_choose_kill(players: List[Player], alive: Set[int], mafia_id: int) -> Optional[int]:
    candidates = [pid for pid in alive if pid != mafia_id]
    if not candidates:
        return None
    best = max(score_kill_target(players, pid) for pid in candidates)
    top = [pid for pid in candidates if score_kill_target(players, pid) == best]
    good = random.choice(top) if top else None
    rnd = random.choice(candidates) if candidates else None
    return _pick_with_competence(
        competence=_competence_for_axis(players[mafia_id].role, "targeting"),
        good_choice=good,
        random_choice=rnd,
    )

def _alive_mafia_ids(players: List[Player], alive: Set[int]) -> List[int]:
    return [pid for pid in alive if players[pid].role in MAFIA]


def sheriff_investigate(players: List[Player], alive: Set[int], sheriff_id: int) -> Tuple[int, bool]:
    target = pick_alive(players, alive, exclude={sheriff_id})
    if target is None:
        return sheriff_id, False
    suspicious = faction(players[target].role) == "Mafia" or players[target].role in {"Arsonist"}
    return target, suspicious


def lookout_watch(players: List[Player], alive: Set[int], lookout_id: int) -> int:
    # Watch likely kill targets: self > sheriff > doc > random.
    for prefer in ("Sheriff", "Doctor"):
        for pid in alive:
            if players[pid].role == prefer:
                return pid
    return pick_alive(players, alive, exclude={lookout_id}) or lookout_id


def tracker_track(
    players: List[Player],
    alive: Set[int],
    tracker_id: int,
    evidence: Dict[int, int],
) -> int:
    mafia = [pid for pid in alive if players[pid].role in MAFIA and pid != tracker_id]
    if mafia:
        return random.choice(mafia)
    good = town_lynch_decision(players, alive, evidence)
    if good is None or good == tracker_id:
        good = town_lynch_decision_desperate(players, alive, evidence)
    if good is None or good == tracker_id:
        good = pick_alive(players, alive, exclude={tracker_id})
    return good if good is not None else tracker_id


def doctor_heal(players: List[Player], alive: Set[int], doctor_id: int) -> int:
    # Heal likely kill targets: sheriff/lookout/tracker > self.
    for prefer in ("Lookout", "Tracker", "Sheriff", "Investigator", "Doctor"):
        for pid in alive:
            if players[pid].role == prefer:
                good = pid
                rnd = pick_alive(players, alive, exclude={doctor_id}) or doctor_id
                return _pick_with_competence(
                    competence=_competence_for_axis("Doctor", "targeting"),
                    good_choice=good,
                    random_choice=rnd,
                ) or doctor_id
    return doctor_id


def survivor_vest_if_needed(players: List[Player], pid: int) -> bool:
    p = players[pid]
    if p.role != "Survivor":
        return False
    if p.vests_left <= 0:
        return False
    # Heuristic: vest when late or occasionally early; competence = remembering to vest at all.
    should = sum(1 for x in players if x.alive) <= 4 or mc_roll(0.20)
    if not should:
        return False
    return mc_roll(_competence_for_axis("Survivor", "usage"))


def town_lynch_decision(
    players: List[Player],
    alive: Set[int],
    evidence: Dict[int, int],
) -> Optional[int]:
    """
    Evidence model: evidence[pid] is suspicion points.
    Town lynches the top suspect if there is a clear leader; otherwise, no lynch.
    """
    if not alive:
        return None
    scored = [(evidence.get(pid, 0), pid) for pid in alive]
    scored.sort(reverse=True)
    top_score, top_pid = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1
    if top_score >= 3 and top_score > second_score:
        return top_pid
    return None


def town_lynch_decision_desperate(
    players: List[Player],
    alive: Set[int],
    evidence: Dict[int, int],
) -> Optional[int]:
    """
    Late-game / low-info behavior: Town will eventually lynch someone rather than stall forever.
    Pick the highest-suspicion alive player, breaking ties randomly.
    """
    if not alive:
        return None
    scored = [(evidence.get(pid, 0), pid) for pid in alive]
    best = max(s for s, _pid in scored)
    top = [pid for s, pid in scored if s == best]
    return mc_pick_from(top) if top else None


def _town_competence(players: List[Player], alive: Set[int]) -> float:
    """Aggregate day-play competence from living Town roles."""
    town_ids = [pid for pid in alive if players[pid].role in TOWN]
    if not town_ids:
        return 0.60
    vals = [_competence_for_axis(players[pid].role, "day") for pid in town_ids]
    return sum(vals) / len(vals)


def check_main_winner(players: List[Player]) -> Optional[str]:
    """
    Main game winner (Town/Mafia/Draw) ignoring personal neutral wins.

    Uses faction_win_logic.living_roles_faction_winner (same rules as game.py).
    Serial Killer solo (last living) is handled in simulate_once.
    """
    from faction_win_logic import living_roles_faction_winner

    alive = [p for p in players if p.alive]
    if not alive:
        return "Draw"
    if len(alive) == 1 and alive[0].role == "Serial Killer":
        return None
    if len(alive) == 2:
        stalemate = lookup_two_player_stalemate(alive[0].role, alive[1].role)
        if stalemate.applies:
            if stalemate.winner in ("Town", "Mafia"):
                return stalemate.winner
            return None
    return living_roles_faction_winner(p.role for p in alive)


def _alive_ids(players: List[Player]) -> Set[int]:
    return {p.i for p in players if p.alive}
