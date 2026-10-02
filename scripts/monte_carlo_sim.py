import argparse
import csv
import hashlib
import itertools
from pathlib import Path
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, TypedDict, Literal, cast


TOWN = {
    "Doctor",
    "Sheriff",
    "Investigator",
    "Lookout",
    "Tracker",
    "Escort",
    "Vigilante",
    "Retributionist",
    "Bodyguard",
    "Transporter",
    "Mayor",
    "Scary Grandma",
    # Sim-only: no-power Town role for controlled experiments via --roles.
    # Not sampled by the generator weights.
    "Civilian",
}

MAFIA = {
    "Mobster",
    # <=6p pool
    "Gravedigger",
    "Consort",
    "Framer",
    # 7+p pool
    "Gatekeeper",
    "Hypnotist",
    "Mole",
    "Tailor",
}

NEUTRAL = {
    "Survivor",
    "Executioner",
    "Jester",
    "Witch",
    "Pirate",
    "Arsonist",
    "Chaos",
}

INVESTIGATIVE = {"Sheriff", "Investigator", "Lookout", "Tracker"}
PROTECTIVE = {"Doctor", "Bodyguard"}

# Mirrors config.py (night-engine semantics)
ROLEBLOCK_IMMUNE = {"Scary Grandma", "Witch", "Consort", "Escort", "Pirate", "Transporter"}
CONTROL_IMMUNE = {"Transporter", "Scary Grandma", "Witch", "Pirate", "Chaos"}

#
# --- Role difficulty / competence model ---
#
# Goal: reflect that some roles are inherently harder to play optimally.
# We approximate this by a per-role probability that the player makes a "good" decision
# when choosing targets / timing. When they fail the check, they pick a random legal option.
#
# Tuning notes:
# - Investigatives & disruption roles are more skill-sensitive -> lower baseline competence.
# - Simple roles (Mobster kill selection, Survivor vest timing) are easier -> higher.
# - This is not meant to be "ranked skill"; it's just a difficulty normalization layer.
#
ROLE_COMPETENCE: Dict[str, float] = {
    # Town (harder to play well)
    "Sheriff": 0.70,
    "Investigator": 0.60,
    "Lookout": 0.60,
    "Tracker": 0.60,
    "Transporter": 0.45,
    "Mayor": 0.60,
    "Escort": 0.65,
    "Doctor": 0.70,
    "Bodyguard": 0.65,
    "Vigilante": 0.55,
    "Scary Grandma": 0.70,
    "Retributionist": 0.50,
    # Mafia
    "Mobster": 0.75,
    "Consort": 0.70,
    "Framer": 0.65,
    "Gravedigger": 0.60,
    "Hypnotist": 0.55,
    "Mole": 0.60,
    "Tailor": 0.55,
    "Gatekeeper": 0.60,
    # Neutrals
    "Witch": 0.50,
    "Executioner": 0.55,
    "Jester": 0.55,
    "Survivor": 0.75,
    "Pirate": 0.60,
    "Arsonist": 0.55,
    "Chaos": 0.50,
}


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


# Lobby-wide skill adjustment.
#  - lobby_skill=0.0: closer to "average" play (more random mistakes)
#  - lobby_skill=1.0: closer to "competent" play (fewer random mistakes)
LOBBY_SKILL: float = 0.5
USE_DIFFICULTY_LAYER: bool = True

# Balance toggles
GATEKEEPER_BLOCKS_ONE: bool = False


def _competence_for_role(role: str) -> float:
    if not USE_DIFFICULTY_LAYER:
        return 1.0
    base = float(ROLE_COMPETENCE.get(role, 0.60))
    # Shift competence slightly based on lobby skill, preserving relative role difficulty.
    # Range: +/- 0.10 around base across the full [0,1] lobby range.
    shift = (LOBBY_SKILL - 0.5) * 0.20
    return _clamp01(base + shift)


def _pick_with_competence(
    *,
    competence: float,
    good_choice: Optional[int],
    random_choice: Optional[int],
) -> Optional[int]:
    if good_choice is None:
        return random_choice
    if random_choice is None:
        return good_choice
    return good_choice if random.random() < competence else random_choice


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
    gravedigger_uses_left: int = 0
    mole_uses_left: int = 0
    tailor_uses_left: int = 0
    pirate_wins: int = 0
    witch_shield_used: bool = False  # only used for light modeling
    guilty_next_day: bool = False  # Vigilante guilt (dies next day if shot killed Town)
    retri_uses_left: int = 0
    chaos_uses_left: int = 0

    # Night flags
    framed_tonight: bool = False  # Framer
    roleblocked_tonight: bool = False  # Escort/Consort/Gatekeeper
    protected_tonight: bool = False  # Doctor/Bodyguard/Survivor vest
    on_alert_tonight: bool = False  # Scary Grandma
    transported_with: Optional[int] = None  # Transporter swap partner
    tailored_as: Optional[str] = None  # Tailor (death-reveal only; does NOT affect investigations)

    # Neutral win tracking
    exe_target: Optional[int] = None  # Executioner only
    exe_won: bool = False
    jester_won: bool = False
    survivor_won: bool = False
    pirate_won: bool = False
    arsonist_won: bool = False
    # Arsonist douse tracking (global list is easier, but keep flag too)
    doused: bool = False


ActionType = Literal[
    "kill",
    "shoot",
    "plunder",
    "ignite",
    "douse",
    "clean",
    "heal",
    "protect",
    "bg_vest",
    "vest",
    "alert",
    "frame",
    "roleblock",
    "guard",  # Gatekeeper
    "control",  # Witch
    "transport",
    "tailor",
    "hide",  # Gravedigger
    "investigate",  # Sheriff/Investigator/Mole
    "watch",  # Lookout
    "track",  # Tracker
    "hypnotize",  # Hypnotist (misinformation)
    "ret_protect",  # Retributionist using Bodyguard corpse (BG counterkill, but Ret does not die)
]


class Action(TypedDict, total=False):
    type: ActionType
    actor: int
    target: int
    targets: List[int]
    role: str  # for investigate and for helper filtering
    duel_won: bool
    fake_role: str  # tailor
    msg_type: str  # hypnotist


def _mafia_ids(players: List[Player], alive: Set[int]) -> List[int]:
    return [pid for pid in alive if players[pid].role in MAFIA]


def _apply_transport_to_id(x: Optional[int], swap: Optional[Tuple[int, int]]) -> Optional[int]:
    if x is None or swap is None:
        return x
    a, b = swap
    if x == a:
        return b
    if x == b:
        return a
    return x


def _build_visit_log(actions: List[Action]) -> Dict[int, List[int]]:
    visit_log: Dict[int, List[int]] = {}
    for act in actions:
        actor = act.get("actor")
        if actor is None:
            continue
        a_type = act.get("type")
        if a_type == "control":
            # Witch only "visits" the controlled player.
            raw = act.get("targets")
            if not isinstance(raw, list) or not raw:
                t = None
            else:
                t = raw[0]
            if t is not None:
                visit_log.setdefault(t, []).append(actor)
        elif a_type == "transport":
            for t in act.get("targets", []):
                visit_log.setdefault(t, []).append(actor)
        else:
            t = act.get("target")
            if t is not None:
                visit_log.setdefault(t, []).append(actor)
    return visit_log


def _resolve_blocking(players: List[Player], alive: Set[int], actions: List[Action], visit_log_raw: Dict[int, List[int]]) -> Set[int]:
    # Fixed-point roleblock; Gatekeeper guard blocks visitors (non-mafia, non-transporter).
    blocked: Set[int] = set()
    blockers: List[Tuple[int, int]] = []
    for act in actions:
        if act.get("type") in {"roleblock", "plunder"} and act.get("actor") is not None and act.get("target") is not None:
            blockers.append((act["actor"], act["target"]))

    # Compute a stable blocked set. In some cases (mutual roleblocks), this relation can
    # oscillate; detect cycles and break conservatively (prefer "more blocked").
    seen: Set[frozenset[int]] = set()
    while True:
        new_blocked: Set[int] = set()
        for actor, target in blockers:
            if actor in blocked:
                continue
            if players[target].role in ROLEBLOCK_IMMUNE:
                continue
            new_blocked.add(target)
        key = frozenset(new_blocked)
        if key in seen:
            blocked |= new_blocked
            break
        seen.add(key)
        if new_blocked == blocked:
            break
        blocked = new_blocked

    for act in actions:
        if act.get("type") != "guard":
            continue
        actor = act.get("actor")
        target = act.get("target")
        if actor is None or target is None:
            continue
        if actor in blocked:
            continue
        eligible: List[int] = []
        for visitor in visit_log_raw.get(target, []):
            if visitor in blocked:
                continue
            vr = players[visitor].role
            if vr in MAFIA or vr == "Transporter":
                continue
            eligible.append(visitor)

        if not eligible:
            continue

        if GATEKEEPER_BLOCKS_ONE:
            visitor = random.choice(eligible)
            blocked.add(visitor)
            players[visitor].roleblocked_tonight = True
        else:
            for visitor in eligible:
                blocked.add(visitor)
                players[visitor].roleblocked_tonight = True

    return blocked


def _bucket_for_investigator(r: str) -> List[str]:
    buckets: List[List[str]] = [
        ["Investigator", "Mole", "Mayor", "Tracker"],
        ["Doctor", "Bodyguard", "Survivor"],
        ["Escort", "Consort", "Hypnotist"],
        ["Lookout", "Transporter", "Tailor"],
        ["Vigilante", "Pirate", "Scary Grandma"],
        ["Mobster", "Gatekeeper", "Gravedigger"],
        ["Framer", "Jester", "Executioner", "Witch", "Chaos"],
        ["Arsonist"],
        ["Retributionist", "Sheriff"],
    ]
    for b in buckets:
        if r in b:
            return b
    return [r]


def faction(role: str) -> str:
    if role in MAFIA:
        return "Mafia"
    if role in TOWN:
        return "Town"
    if role in NEUTRAL:
        return "Neutral"
    return "Unknown"


def pick_alive(players: List[Player], alive: Set[int], *, exclude: Set[int] | None = None) -> Optional[int]:
    ex = exclude or set()
    choices = [pid for pid in alive if pid not in ex]
    return random.choice(choices) if choices else None


def score_kill_target(players: List[Player], pid: int) -> int:
    """Mafia prefers killing info/power roles."""
    r = players[pid].role
    if r in {"Lookout", "Tracker", "Sheriff", "Investigator"}:
        return 5
    if r in {"Doctor", "Escort", "Vigilante"}:
        return 4
    if r in {"Mayor", "Transporter", "Bodyguard"}:
        return 3
    if r in {"Retributionist"}:
        return 1
    # neutrals are usually not Mafia priority early, but can be.
    return 2


def mafia_choose_kill(players: List[Player], alive: Set[int], mafia_id: int) -> Optional[int]:
    candidates = [pid for pid in alive if pid != mafia_id]
    if not candidates:
        return None
    best = max(score_kill_target(players, pid) for pid in candidates)
    top = [pid for pid in candidates if score_kill_target(players, pid) == best]
    good = random.choice(top) if top else None
    rnd = random.choice(candidates) if candidates else None
    return _pick_with_competence(
        competence=_competence_for_role(players[mafia_id].role),
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


def tracker_track(players: List[Player], alive: Set[int], tracker_id: int) -> int:
    return pick_alive(players, alive, exclude={tracker_id}) or tracker_id


def doctor_heal(players: List[Player], alive: Set[int], doctor_id: int) -> int:
    # Heal likely kill targets: sheriff/lookout/tracker > self.
    for prefer in ("Lookout", "Tracker", "Sheriff", "Investigator", "Doctor"):
        for pid in alive:
            if players[pid].role == prefer:
                good = pid
                rnd = pick_alive(players, alive, exclude={doctor_id}) or doctor_id
                return _pick_with_competence(
                    competence=_competence_for_role("Doctor"),
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
    # Heuristic: in 6p, vest when 4 or fewer alive (late), or 25% early.
    if sum(1 for x in players if x.alive) <= 4 or random.random() < 0.25:
        # IMPORTANT: In the bot, vests are NOT consumed if roleblocked.
        # So we only *schedule* vest here; consumption happens after blocking.
        return True
    return False


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
    return random.choice(top) if top else None


def _town_competence(players: List[Player], alive: Set[int]) -> float:
    """Aggregate day-play competence from living Town roles."""
    town_ids = [pid for pid in alive if players[pid].role in TOWN]
    if not town_ids:
        return 0.60
    vals = [_competence_for_role(players[pid].role) for pid in town_ids]
    return sum(vals) / len(vals)


def check_main_winner(players: List[Player]) -> Optional[str]:
    """
    Main game winner (Town/Mafia/Draw) ignoring personal neutral wins.

    Mirrors bot.py/game.py semantics:
      - Town/Mafia faction wins do NOT trigger while an Arsonist is alive.
      - Mafia wins on parity against non-mafia (town + neutrals), but only if no Arsonist alive.
    """
    alive = [p for p in players if p.alive]
    if not alive:
        return "Draw"
    mafia_alive = any(p.role in MAFIA for p in alive)
    town_alive = any(p.role in TOWN for p in alive)
    arso_alive = any(p.role == "Arsonist" for p in alive)
    if not mafia_alive and town_alive:
        return None if arso_alive else "Town"
    mafia_count = sum(1 for p in alive if p.role in MAFIA)
    non_mafia_count = sum(1 for p in alive if p.role not in MAFIA)
    if mafia_alive and mafia_count >= non_mafia_count:
        return None if arso_alive else "Mafia"
    return None


def _alive_ids(players: List[Player]) -> Set[int]:
    return {p.i for p in players if p.alive}


class SimStats(TypedDict, total=False):
    days: int
    lynches: int
    mislynches: int
    night_deaths: int
    doc_saves: int
    roleblocks: int
    gatekeeper_blocks: int
    controls: int
    controls_prevent_ignite: int
    ignites: int


def simulate_once(
    roles: List[str],
    *,
    max_days: int = 20,
    collect_stats: bool = False,
    trace: bool = False,
) -> Dict[str, bool] | Tuple[Dict[str, bool], SimStats] | Tuple[Dict[str, bool], List[str]]:
    players = [Player(i=i, role=r) for i, r in enumerate(roles)]
    for p in players:
        if p.role == "Survivor":
            p.vests_left = 2
        if p.role == "Vigilante":
            p.shots_left = 1
        if p.role == "Doctor":
            p.self_heals_left = 1
        if p.role == "Bodyguard":
            p.bg_uses_left = 1
            p.bg_self_protects_left = 1
        if p.role == "Scary Grandma":
            p.alerts_left = 2
        if p.role == "Gatekeeper":
            p.gatekeeper_uses_left = 2
        if p.role == "Gravedigger":
            p.gravedigger_uses_left = 1
        if p.role == "Mole":
            p.mole_uses_left = 1
        if p.role == "Tailor":
            p.tailor_uses_left = 1
        if p.role == "Pirate":
            p.pirate_wins = 0
        if p.role == "Retributionist":
            p.retri_uses_left = 2
        if p.role == "Chaos":
            p.chaos_uses_left = 2

    alive: Set[int] = {p.i for p in players}
    evidence: Dict[int, int] = {}  # suspicion points
    doused: Set[int] = set()  # Arsonist global douse list
    # Graveyard bookkeeping for Retributionist (simplified):
    # store dead Town roles and allow each corpse to be used once.
    dead_town_corpses: List[Tuple[int, str]] = []  # (pid, role)
    used_corpse_ids: Set[int] = set()

    # Assign Executioner target (ToS-like: a Town role, excluding self, exclude Mayor).
    exe_ids = [p.i for p in players if p.role == "Executioner"]
    if exe_ids:
        exe_id = exe_ids[0]
        town_targets = [p.i for p in players if p.i != exe_id and p.role in TOWN and p.role != "Mayor"]
        players[exe_id].exe_target = random.choice(town_targets) if town_targets else None

    # Outcome flags (multiple can be true).
    out: Dict[str, bool] = {
        "Town": False,
        "Mafia": False,
        "Draw": False,
        "Executioner": False,
        "Jester": False,
        "Survivor": False,
        "Pirate": False,
        "Arsonist": False,
        "Chaos": False,
    }

    day = 1
    no_lynch_streak = 0
    pending_haunts: List[int] = []  # list of jester ids that can haunt once
    log: List[str] = []
    if trace:
        log.append("=== TRACE START ===")
        log.append("Roles by seat:")
        for p in players:
            log.append(f"  P{p.i}: {p.role}")
    stats: SimStats = {}
    if collect_stats:
        stats = {
            "days": 0,
            "lynches": 0,
            "mislynches": 0,
            "night_deaths": 0,
            "doc_saves": 0,
            "roleblocks": 0,
            "gatekeeper_blocks": 0,
            "controls": 0,
            "controls_prevent_ignite": 0,
            "ignites": 0,
        }
    while day <= max_days:
        # Reset night flags
        for pid in list(alive):
            p = players[pid]
            p.framed_tonight = False
            p.roleblocked_tonight = False
            p.protected_tonight = False
            p.on_alert_tonight = False
            p.transported_with = None

        # Mobster promotion (bot-aligned): if Mafia are alive but no Mobster remains, promote a random Mafia.
        mafia_alive = [pid for pid in alive if players[pid].role in MAFIA]
        if mafia_alive and not any(players[pid].role == "Mobster" for pid in mafia_alive):
            new_m = random.choice(mafia_alive)
            players[new_m].role = "Mobster"
            if trace:
                log.append(f"Mafia promotion: P{new_m} is promoted to Mobster.")

        # ---- Night ----
        actions: List[Action] = []
        if trace:
            log.append(f"\n--- Night {day} ---")

        # Jester haunt happens at night after being lynched.
        # In-bot this is player-chosen among eligible voters; in sim we approximate by haunting a random living player.
        if pending_haunts:
            for j_id in pending_haunts[:]:
                if j_id in alive:
                    # If Jester somehow still alive, skip (shouldn't happen).
                    pending_haunts.remove(j_id)
                    continue
                target = pick_alive(players, alive, exclude=set())
                if target is not None:
                    # Treat haunt as unstoppable kill: bypass heals/protect/vest/alert.
                    players[target].alive = False
                    alive.discard(target)
                    # Executioner conversion triggers on "haunt" in the bot; emulate by treating as night death.
                pending_haunts.remove(j_id)

        # Survivors may vest.
        for pid in list(alive):
            if players[pid].role == "Survivor" and survivor_vest_if_needed(players, pid):
                actions.append({"type": "vest", "actor": pid, "role": "Survivor"})
                if trace:
                    log.append(f"Survivor P{pid} uses vest.")

        # Transporter (swap two)
        transport_swap: Optional[Tuple[int, int]] = None
        for pid in list(alive):
            if players[pid].role == "Transporter" and random.random() < 0.25:
                # good: swap two non-self targets; random: may pick weaker/less relevant swaps
                a_good = pick_alive(players, alive, exclude={pid})
                b_good = pick_alive(players, alive, exclude={pid, a_good} if a_good is not None else {pid})
                a_rnd = pick_alive(players, alive, exclude={pid})
                b_rnd = pick_alive(players, alive, exclude={pid, a_rnd} if a_rnd is not None else {pid})
                a = _pick_with_competence(
                    competence=_competence_for_role("Transporter"),
                    good_choice=a_good,
                    random_choice=a_rnd,
                )
                b = _pick_with_competence(
                    competence=_competence_for_role("Transporter"),
                    good_choice=b_good,
                    random_choice=b_rnd,
                )
                if a is not None and b is not None and a != b:
                    transport_swap = (a, b)
                    actions.append({"type": "transport", "actor": pid, "targets": [a, b], "role": "Transporter"})
                    if trace:
                        log.append(f"Transporter P{pid} transports P{a}<->P{b}.")
                break

        # Witch control (redirect a controlled player's target to another)
        witch_id = next((pid for pid in alive if players[pid].role == "Witch"), None)
        controlled_id: Optional[int] = None
        control_target: Optional[int] = None
        if witch_id is not None and random.random() < 0.35:
            controlled_id = pick_alive(players, alive, exclude={witch_id})
            control_target = pick_alive(players, alive, exclude={witch_id, controlled_id} if controlled_id is not None else {witch_id})
            if controlled_id is not None and control_target is not None:
                if players[controlled_id].role not in CONTROL_IMMUNE:
                    actions.append({"type": "control", "actor": witch_id, "targets": [controlled_id, control_target], "role": "Witch"})
                    if collect_stats:
                        stats["controls"] += 1
                    if trace:
                        log.append(f"Witch P{witch_id} controls P{controlled_id} -> P{control_target}.")

        # Roleblocks + Gatekeeper guard
        for pid in list(alive):
            r = players[pid].role
            if r == "Escort":
                good = town_lynch_decision_desperate(players, alive, evidence)
                if good is None or good == pid:
                    good = pick_alive(players, alive, exclude={pid})
                rnd = pick_alive(players, alive, exclude={pid})
                tgt = _pick_with_competence(competence=_competence_for_role("Escort"), good_choice=good, random_choice=rnd)
                if tgt is not None:
                    actions.append({"type": "roleblock", "actor": pid, "target": tgt, "role": r})
                    if trace:
                        log.append(f"Escort P{pid} roleblocks P{tgt}.")
            elif r == "Consort":
                tgt = None
                for prefer in ("Doctor", "Sheriff", "Investigator", "Lookout", "Tracker", "Vigilante", "Transporter"):
                    tgt = next((x for x in alive if players[x].role == prefer), None)
                    if tgt is not None:
                        break
                tgt = tgt if tgt is not None else pick_alive(players, alive, exclude={pid})
                if tgt is not None:
                    actions.append({"type": "roleblock", "actor": pid, "target": tgt, "role": r})
                    if trace:
                        log.append(f"Consort P{pid} roleblocks P{tgt}.")
            elif r == "Gatekeeper" and players[pid].gatekeeper_uses_left > 0:
                tgt = None
                for prefer in ("Sheriff", "Investigator", "Doctor", "Mayor"):
                    tgt = next((x for x in alive if players[x].role == prefer), None)
                    if tgt is not None:
                        break
                tgt = tgt if tgt is not None else pick_alive(players, alive, exclude={pid})
                if tgt is not None:
                    players[pid].gatekeeper_uses_left -= 1
                    actions.append({"type": "guard", "actor": pid, "target": tgt, "role": r})
                    if trace:
                        log.append(f"Gatekeeper P{pid} guards P{tgt}.")

        # Frames / heals / protects / alerts / tailor / gravedigger / hypnotist / arsonist
        for pid in list(alive):
            r = players[pid].role
            if r == "Framer":
                # Bot: only Nights 1 and 2.
                if day > 2:
                    continue
                tgt = None
                for prefer in ("Sheriff", "Investigator", "Lookout", "Tracker"):
                    tgt = next((x for x in alive if players[x].role == prefer), None)
                    if tgt is not None:
                        break
                good = tgt if tgt is not None else pick_alive(players, alive, exclude={pid})
                rnd = pick_alive(players, alive, exclude={pid})
                tgt = _pick_with_competence(competence=_competence_for_role("Framer"), good_choice=good, random_choice=rnd)
                if tgt is not None:
                    actions.append({"type": "frame", "actor": pid, "target": tgt, "role": r})
                    if trace:
                        log.append(f"Framer P{pid} frames P{tgt}.")
            elif r == "Doctor":
                tgt = doctor_heal(players, alive, pid)
                if tgt is not None:
                    actions.append({"type": "heal", "actor": pid, "target": tgt, "role": r})
                    if trace:
                        log.append(f"Doctor P{pid} heals P{tgt}.")
            elif r == "Bodyguard":
                if players[pid].bg_self_protects_left > 0 and sum(1 for x in players if x.alive) <= 5 and random.random() < 0.2:
                    players[pid].bg_self_protects_left -= 1
                    actions.append({"type": "bg_vest", "actor": pid, "role": r})
                    if trace:
                        log.append(f"Bodyguard P{pid} uses vest.")
                elif players[pid].bg_uses_left > 0:
                    tgt = None
                    for prefer in ("Sheriff", "Investigator", "Doctor", "Mayor", "Lookout", "Tracker"):
                        tgt = next((x for x in alive if players[x].role == prefer), None)
                        if tgt is not None:
                            break
                    tgt = tgt if tgt is not None else pick_alive(players, alive, exclude={pid})
                    if tgt is not None:
                        players[pid].bg_uses_left -= 1
                        actions.append({"type": "protect", "actor": pid, "target": tgt, "role": r})
                        if trace:
                            log.append(f"Bodyguard P{pid} protects P{tgt}.")
            elif r == "Scary Grandma" and players[pid].alerts_left > 0:
                if sum(1 for x in players if x.alive) <= 6 or random.random() < 0.25:
                    players[pid].alerts_left -= 1
                    actions.append({"type": "alert", "actor": pid, "role": r})
                    if trace:
                        log.append(f"Scary Grandma P{pid} goes on alert.")
            elif r == "Tailor" and players[pid].tailor_uses_left > 0 and random.random() < 0.35:
                tgt = pick_alive(players, alive, exclude={pid})
                if tgt is not None:
                    players[pid].tailor_uses_left -= 1
                    fake = random.choice(["Sheriff", "Investigator", "Lookout", "Tracker", "Doctor", "Retributionist", "Mobster"])
                    actions.append({"type": "tailor", "actor": pid, "target": tgt, "fake_role": fake, "role": r})
            elif r == "Gravedigger" and players[pid].gravedigger_uses_left > 0 and random.random() < 0.35:
                tgt = pick_alive(players, alive, exclude={pid})
                if tgt is not None:
                    players[pid].gravedigger_uses_left -= 1
                    actions.append({"type": "hide", "actor": pid, "target": tgt, "role": r})
            elif r == "Hypnotist" and random.random() < 0.4:
                tgt = pick_alive(players, alive, exclude={pid})
                if tgt is not None:
                    msg = random.choice(["healed", "roleblocked", "transported", "controlled", "attacked"])
                    actions.append({"type": "hypnotize", "actor": pid, "target": tgt, "msg_type": msg, "role": r})
            elif r == "Arsonist":
                # ToS-like quirk: if the Arsonist is doused, they should typically spend the night cleaning,
                # otherwise igniting can kill them too.
                if pid in doused:
                    actions.append({"type": "clean", "actor": pid, "role": r})
                    if trace:
                        log.append(f"Arsonist P{pid} cleans gasoline off themselves.")
                    continue

                # Avoid artificial stalemates: don't ignite unless at least one living player is doused.
                # In particular, in a 1v1 vs Mafia, Arsonist should douse first, then ignite to win.
                others_alive = [x for x in alive if x != pid]
                living_doused = [x for x in others_alive if x in doused]

                if len(others_alive) == 1:
                    only_other = others_alive[0]
                    if only_other in doused:
                        actions.append({"type": "ignite", "actor": pid, "role": r})
                        if trace:
                            log.append(f"Arsonist P{pid} chooses IGNITE.")
                    else:
                        actions.append({"type": "douse", "actor": pid, "target": only_other, "role": r})
                        if trace:
                            log.append(f"Arsonist P{pid} douses P{only_other}.")
                else:
                    # General policy: ignite late-game if it will kill at least one person,
                    # or once doused population is large.
                    if (living_doused and sum(1 for x in players if x.alive) <= 4) or len(doused) >= 3:
                        actions.append({"type": "ignite", "actor": pid, "role": r})
                        if trace:
                            log.append(f"Arsonist P{pid} chooses IGNITE.")
                    else:
                        tgt = pick_alive(players, alive, exclude={pid})
                        if tgt is not None:
                            actions.append({"type": "douse", "actor": pid, "target": tgt, "role": r})
                            if trace:
                                log.append(f"Arsonist P{pid} douses P{tgt}.")
            elif r == "Chaos" and players[pid].chaos_uses_left > 0:
                # Chaos: 2 uses total, no-kill effect pool, choose 2 targets.
                # Bot-aligned semantics:
                # - pick an effect from the pool
                # - do not consume a use if the action becomes noop/invalid
                # - consume a use only if the Chaos actor is not blocked
                # - once valid + not blocked, consume a use even if the chosen effect ends up doing nothing
                if random.random() < 0.75:
                    good1 = pick_alive(players, alive, exclude={pid})
                    good2 = pick_alive(players, alive, exclude={pid, good1} if good1 is not None else {pid})
                    rnd1 = pick_alive(players, alive, exclude={pid})
                    rnd2 = pick_alive(players, alive, exclude={pid, rnd1} if rnd1 is not None else {pid})
                    t1 = _pick_with_competence(competence=_competence_for_role("Chaos"), good_choice=good1, random_choice=rnd1)
                    t2 = _pick_with_competence(competence=_competence_for_role("Chaos"), good_choice=good2, random_choice=rnd2)
                    if t1 is not None and t2 is not None and t1 != t2:
                        # Live-bot pool (engine/night.py):
                        eff_pool = ["roleblock", "transport", "heal", "protect", "investigate", "watch", "track", "frame", "hide"]
                        eff = random.choice(eff_pool)

                        payload: Action
                        if eff == "roleblock":
                            payload = {"type": "roleblock", "actor": pid, "target": t1, "role": r}
                        elif eff == "transport":
                            payload = {"type": "transport", "actor": pid, "targets": [t1, t2], "role": r}
                        elif eff == "protect":
                            payload = {"type": "protect", "actor": pid, "target": t1, "role": r}
                        elif eff == "investigate":
                            payload = {"type": "investigate", "actor": pid, "target": t1, "role": random.choice(["Sheriff", "Investigator", "Mole"])}
                        elif eff == "watch":
                            payload = {"type": "watch", "actor": pid, "target": t1, "role": r}
                        elif eff == "track":
                            payload = {"type": "track", "actor": pid, "target": t1, "role": r}
                        elif eff == "heal":
                            payload = {"type": "heal", "actor": pid, "target": t1, "role": r}
                        elif eff == "frame":
                            payload = {"type": "frame", "actor": pid, "target": t1, "role": r}
                        else:
                            # hide
                            payload = {"type": "hide", "actor": pid, "target": t1, "role": r}

                        # Mark for later consumption after blocking is known.
                        payload["_from_chaos"] = True  # type: ignore[typeddict-unknown-key]
                        actions.append(payload)

        # Retributionist: 2 uses total; reanimate a dead Town corpse to perform its ability.
        # We implement a simple policy: use corpses after day 1, prefer impactful roles.
        for pid in list(alive):
            if players[pid].role != "Retributionist":
                continue
            if players[pid].retri_uses_left <= 0:
                continue
            if day < 2:
                continue
            # pick an unused corpse (prefer Vigi/BG/Doctor/investigatives/escort/transporter)
            candidates = [(c_pid, c_role) for (c_pid, c_role) in dead_town_corpses if c_pid not in used_corpse_ids]
            if not candidates:
                continue

            pref_order = ["Vigilante", "Bodyguard", "Doctor", "Sheriff", "Investigator", "Lookout", "Tracker", "Escort", "Transporter"]
            candidates.sort(key=lambda cr: pref_order.index(cr[1]) if cr[1] in pref_order else 999)
            good_corpse = candidates[0]
            rnd_corpse = random.choice(candidates) if candidates else good_corpse
            corpse_pid, corpse_role = good_corpse if random.random() < _competence_for_role("Retributionist") else rnd_corpse

            # Choose targets
            t1_good = pick_alive(players, alive, exclude={pid})
            t1_rnd = pick_alive(players, alive, exclude={pid})
            t1 = _pick_with_competence(competence=_competence_for_role("Retributionist"), good_choice=t1_good, random_choice=t1_rnd)
            if t1 is None:
                continue
            if corpse_role == "Transporter":
                t2_good = pick_alive(players, alive, exclude={pid, t1})
                t2_rnd = pick_alive(players, alive, exclude={pid, t1})
                t2 = _pick_with_competence(competence=_competence_for_role("Retributionist"), good_choice=t2_good, random_choice=t2_rnd)
                if t2 is None or t2 == t1:
                    continue
            else:
                t2 = None

            # Spend use and mark corpse used
            players[pid].retri_uses_left -= 1
            used_corpse_ids.add(corpse_pid)

            if corpse_role == "Doctor":
                actions.append({"type": "heal", "actor": pid, "target": t1, "role": "Retributionist"})
                if trace:
                    log.append(f"Retributionist P{pid} reanimates Doctor -> heal P{t1}.")
            elif corpse_role in {"Sheriff", "Investigator", "Mole"}:
                actions.append({"type": "investigate", "actor": pid, "target": t1, "role": corpse_role})
                if trace:
                    log.append(f"Retributionist P{pid} reanimates {corpse_role} -> investigate P{t1}.")
            elif corpse_role == "Lookout":
                actions.append({"type": "watch", "actor": pid, "target": t1, "role": "Lookout"})
                if trace:
                    log.append(f"Retributionist P{pid} reanimates Lookout -> watch P{t1}.")
            elif corpse_role == "Tracker":
                actions.append({"type": "track", "actor": pid, "target": t1, "role": "Tracker"})
                if trace:
                    log.append(f"Retributionist P{pid} reanimates Tracker -> track P{t1}.")
            elif corpse_role == "Escort":
                actions.append({"type": "roleblock", "actor": pid, "target": t1, "role": "Escort"})
                if trace:
                    log.append(f"Retributionist P{pid} reanimates Escort -> roleblock P{t1}.")
            elif corpse_role == "Transporter" and t2 is not None:
                actions.append({"type": "transport", "actor": pid, "targets": [t1, t2], "role": "Transporter"})
                if trace:
                    log.append(f"Retributionist P{pid} reanimates Transporter -> transport P{t1}<->P{t2}.")
            elif corpse_role == "Vigilante":
                # Ret using Vigi corpse: shoot without guilt (we don't model guilt for Ret anyway).
                actions.append({"type": "shoot", "actor": pid, "target": t1, "role": "Vigilante"})
                if trace:
                    log.append(f"Retributionist P{pid} reanimates Vigilante -> shoot P{t1}.")
            elif corpse_role == "Bodyguard":
                # Ret using BG corpse: protect and counterkill attacker, but Ret does not die.
                actions.append({"type": "ret_protect", "actor": pid, "target": t1, "role": "Retributionist"})
                if trace:
                    log.append(f"Retributionist P{pid} reanimates Bodyguard -> protect P{t1} (ret_protect).")

        # Investigations / watch / track
        for pid in list(alive):
            r = players[pid].role
            if r in {"Sheriff", "Investigator"}:
                tgt = pick_alive(players, alive, exclude={pid})
                if tgt is not None:
                    actions.append({"type": "investigate", "actor": pid, "target": tgt, "role": r})
                    if trace:
                        log.append(f"{r} P{pid} investigates P{tgt}.")
            elif r == "Mole" and players[pid].mole_uses_left > 0:
                # Mole is Mafia; it should almost never waste its 1-use reveal on a Mafia teammate.
                non_mafia_choices = [x for x in alive if x != pid and players[x].role not in MAFIA]
                tgt = random.choice(non_mafia_choices) if non_mafia_choices else pick_alive(players, alive, exclude={pid})
                if tgt is not None:
                    players[pid].mole_uses_left -= 1
                    actions.append({"type": "investigate", "actor": pid, "target": tgt, "role": r})
                    if trace:
                        log.append(f"Mole P{pid} investigates P{tgt} (reveal role).")
            elif r == "Lookout":
                actions.append({"type": "watch", "actor": pid, "target": lookout_watch(players, alive, pid), "role": r})
                if trace:
                    log.append(f"Lookout P{pid} watches P{actions[-1]['target']}.")
            elif r == "Tracker":
                actions.append({"type": "track", "actor": pid, "target": tracker_track(players, alive, pid), "role": r})
                if trace:
                    log.append(f"Tracker P{pid} tracks P{actions[-1]['target']}.")

        # Mafia kill (one mafia performs kill for visit logic, but all mafia count for win)
        mafia_ids = _mafia_ids(players, alive)
        mafia_killer_id = mafia_ids[0] if mafia_ids else None
        if mafia_killer_id is not None:
            tgt = mafia_choose_kill(players, alive, mafia_killer_id)
            if tgt is not None:
                actions.append({"type": "kill", "actor": mafia_killer_id, "target": tgt, "role": players[mafia_killer_id].role})
                if trace:
                    log.append(f"Mafia kill by P{mafia_killer_id} targets P{tgt}.")

        # Vigilante shot (strong evidence only)
        for pid in list(alive):
            if players[pid].role == "Vigilante" and players[pid].shots_left > 0:
                tgt = town_lynch_decision(players, alive, evidence)
                if tgt is not None and evidence.get(tgt, 0) >= 4:
                    players[pid].shots_left -= 1
                    actions.append({"type": "shoot", "actor": pid, "target": tgt, "role": "Vigilante"})

        # Pirate plunder
        pirate_id = next((pid for pid in alive if players[pid].role == "Pirate"), None)
        if pirate_id is not None:
            tgt = pick_alive(players, alive, exclude={pirate_id})
            if tgt is not None:
                actions.append({"type": "plunder", "actor": pirate_id, "target": tgt, "role": "Pirate"})
                if trace:
                    log.append(f"Pirate P{pirate_id} plunders P{tgt}.")

        # Apply transport swap to targets
        if transport_swap is not None:
            for act in actions:
                # Self-only actions are not redirected.
                if act.get("type") in {"vest", "bg_vest", "clean"}:
                    continue
                # Mirror bot semantics: Transporter does not redirect Pirate actions.
                if act.get("role") == "Pirate" or act.get("type") == "plunder":
                    continue
                # Mirror bot semantics: do not redirect Transporter itself.
                if act.get("type") == "transport":
                    continue
                if "target" in act:
                    act["target"] = _apply_transport_to_id(act["target"], transport_swap)  # type: ignore[assignment]
                if "targets" in act:
                    act["targets"] = [_apply_transport_to_id(t, transport_swap) or t for t in act.get("targets", [])]  # type: ignore[assignment]

        # Witch redirect if controlling the mafia killer
        if controlled_id is not None and control_target is not None:
            for act in actions:
                # Witch cannot retarget self-only actions like vest/clean.
                if act.get("actor") == controlled_id and act.get("type") in {"vest", "bg_vest", "clean"}:
                    continue
                if act.get("type") == "kill" and act.get("actor") == controlled_id:
                    act["target"] = control_target
                # Witch can prevent Arsonist ignite by forcing a douse instead.
                if act.get("type") == "ignite" and act.get("actor") == controlled_id and players[controlled_id].role == "Arsonist":
                    act["type"] = "douse"
                    act["target"] = control_target
            # Bot-like: if the controlled player submitted no action, Witch can still force certain roles to act.
            if players[controlled_id].role == "Vigilante":
                already = any(a.get("actor") == controlled_id and a.get("type") == "shoot" for a in actions)
                if not already and players[controlled_id].shots_left > 0 and not players[controlled_id].guilty_next_day:
                    # Prevent suicidal forced shots (match bot's "can't target self" guard).
                    if control_target != controlled_id:
                        actions.append({"type": "shoot", "actor": controlled_id, "target": control_target, "role": "Vigilante"})

        # Visits and blocks
        visit_log_raw = _build_visit_log(actions)
        blocked = _resolve_blocking(players, alive, actions, visit_log_raw)
        visit_log = {t: [v for v in vs if v not in blocked] for t, vs in visit_log_raw.items()}
        if trace and blocked:
            log.append("Blocked tonight: " + ", ".join(f"P{pid}({players[pid].role})" for pid in sorted(blocked)))
        if collect_stats:
            # Count any roleblock-like action that actually blocked its target.
            stats["roleblocks"] += sum(1 for pid in blocked if pid in alive)
            # Gatekeeper-specific blocks: those set roleblocked_tonight by guard logic.
            stats["gatekeeper_blocks"] += sum(1 for pid in alive if players[pid].roleblocked_tonight)

        healed_by: Dict[int, int] = {}
        protected_by: Dict[int, List[int]] = {}
        hidden_by_gravedigger: Set[int] = set()

        # Apply misc actions
        for act in actions:
            actor = act.get("actor")
            if actor is None or actor in blocked:
                continue
            t = act.get("type")
            if t == "frame":
                tgt = act.get("target")
                if tgt is not None and tgt in alive:
                    players[tgt].framed_tonight = True
            elif t == "heal":
                tgt = act.get("target")
                if tgt is not None and tgt in alive:
                    # House rule (matches bot): revealed Mayor cannot be healed.
                    if players[tgt].role == "Mayor" and players[tgt].mayor_revealed:
                        continue
                    healed_by[tgt] = actor
                    players[tgt].protected_tonight = True
            elif t == "protect":
                tgt = act.get("target")
                if tgt is not None and tgt in alive:
                    protected_by.setdefault(tgt, []).append(actor)
                    players[tgt].protected_tonight = True
            elif t in {"bg_vest", "vest"}:
                players[actor].protected_tonight = True
                if t == "vest" and players[actor].role == "Survivor":
                    # Consume vest only if not roleblocked (actor not in blocked by this loop).
                    players[actor].vests_left = max(0, players[actor].vests_left - 1)
            elif t == "alert":
                players[actor].on_alert_tonight = True
            elif t == "tailor":
                tgt = act.get("target")
                fake = act.get("fake_role")
                if tgt is not None and fake and tgt in alive:
                    players[tgt].tailored_as = fake
            elif t == "hide":
                tgt = act.get("target")
                if tgt is not None and tgt in alive:
                    hidden_by_gravedigger.add(tgt)
            elif t == "douse":
                tgt = act.get("target")
                if tgt is not None and tgt in alive and tgt not in doused:
                    doused.add(tgt)
                    players[tgt].doused = True
            elif t == "clean":
                # ToS-like: Arsonist can spend the night cleaning gas off themselves.
                if actor in doused:
                    doused.remove(actor)
                players[actor].doused = False

        # Consume Chaos uses only if not blocked AND the expanded action exists (valid),
        # even if it ends up being a no-op (immune target, defended target, etc.).
        # (Matches engine/night.py, not the older sim policy.)
        for act in actions:
            if not act.get("_from_chaos"):
                continue
            actor = act.get("actor")
            if actor is None or actor in blocked:
                continue
            if players[actor].role != "Chaos" or players[actor].chaos_uses_left <= 0:
                continue
            players[actor].chaos_uses_left = max(0, players[actor].chaos_uses_left - 1)

        # Investigative actions -> evidence deltas
        for act in actions:
            actor = act.get("actor")
            if actor is None or actor in blocked:
                continue
            if act.get("type") != "investigate":
                continue
            tgt = act.get("target")
            role = act.get("role")
            if tgt is None or tgt not in alive or role is None:
                continue

            real = players[tgt].role
            # Tailor only affects death reveal in the bot, not investigations.
            apparent = real
            if players[tgt].doused or real == "Arsonist":
                apparent = "Arsonist"
            if players[tgt].framed_tonight:
                apparent = "Framer"

            if role == "Sheriff":
                if apparent in MAFIA or apparent == "Arsonist" or players[tgt].framed_tonight:
                    evidence[tgt] = evidence.get(tgt, 0) + 3
            elif role == "Investigator":
                bucket = _bucket_for_investigator(apparent)
                if any(x in MAFIA for x in bucket):
                    evidence[tgt] = evidence.get(tgt, 0) + 2
                else:
                    evidence[tgt] = max(0, evidence.get(tgt, 0) - 1)
            elif role == "Mole":
                if apparent in MAFIA or apparent == "Arsonist":
                    evidence[tgt] = evidence.get(tgt, 0) + 3

        # Lookout / Tracker evidence from visits
        for act in actions:
            actor = act.get("actor")
            if actor is None or actor in blocked:
                continue
            if act.get("type") == "watch":
                watched = act.get("target")
                if watched is None:
                    continue
                for v in visit_log.get(watched, []):
                    if v in alive and players[v].role in MAFIA:
                        evidence[v] = evidence.get(v, 0) + 2
            elif act.get("type") == "track":
                tracked = act.get("target")
                if tracked is None:
                    continue
                visited_targets = [tgt for tgt, vs in visit_log.items() if tracked in vs]
                if visited_targets and tracked in alive and players[tracked].role in MAFIA:
                    evidence[tracked] = evidence.get(tracked, 0) + 2

        # Hypnotist misinformation (adds random noise / mild misreads)
        for act in actions:
            actor = act.get("actor")
            if actor is None or actor in blocked:
                continue
            if act.get("type") != "hypnotize":
                continue
            tgt = act.get("target")
            if tgt is None or tgt not in alive:
                continue
            msg = act.get("msg_type", "")
            if msg in {"roleblocked", "controlled"}:
                evidence[tgt] = evidence.get(tgt, 0) + 1
            elif msg in {"attacked", "healed"}:
                other = pick_alive(players, alive, exclude={tgt})
                if other is not None:
                    evidence[other] = evidence.get(other, 0) + 1

        # Killing resolution
        deaths: Set[int] = set()

        # Alerts kill visitors
        for pid in list(alive):
            if players[pid].role == "Scary Grandma" and players[pid].on_alert_tonight:
                for v in visit_log.get(pid, []):
                    if v in alive and v != pid:
                        deaths.add(v)

        # Pirate duel
        for act in actions:
            if act.get("type") != "plunder":
                continue
            actor = act.get("actor")
            tgt = act.get("target")
            if actor is None or tgt is None or actor in blocked:
                continue
            if actor not in alive or tgt not in alive:
                continue
            duel_won = random.random() < 0.5
            if duel_won:
                players[actor].pirate_wins += 1
                if players[actor].pirate_wins >= 2:
                    out["Pirate"] = True
                defended = (
                    players[tgt].role == "Arsonist"
                    or players[tgt].protected_tonight
                    or tgt in healed_by
                    or players[tgt].on_alert_tonight
                )
                if not defended:
                    deaths.add(tgt)
            # Bot-aligned: losing the duel does not kill the Pirate; it just means no plunder kill.

        # Ignite
        for act in actions:
            if act.get("type") != "ignite":
                continue
            actor = act.get("actor")
            if actor is None or actor in blocked:
                continue
            if actor not in alive:
                continue
            # Mirrors engine/night.py: ignite kills all doused (treat as unstoppable).
            # ToS-like quirk: igniting while doused kills the Arsonist too.
            if actor in doused:
                deaths.add(actor)
            for pid in list(doused):
                if pid in alive:
                    deaths.add(pid)
            doused.clear()
            if collect_stats:
                stats["ignites"] += 1

        # Mafia kill + Vig shot, with BG counter + Witch N1 shield
        attempted: List[Tuple[int, int, str]] = []
        heal_saved_target: Optional[int] = None
        # protected_by map includes whether protector dies on guard
        protected_by2: Dict[int, List[Tuple[int, bool]]] = {}
        for tgt, prots in protected_by.items():
            protected_by2[tgt] = [(p, True) for p in prots]
        # Convert ret_protect actions into protectors that do not die on guard
        for act in actions:
            if act.get("type") == "ret_protect":
                actor = act.get("actor")
                tgt = act.get("target")
                if actor is not None and tgt is not None and actor not in blocked and actor in alive and tgt in alive:
                    protected_by2.setdefault(tgt, []).append((actor, False))

        for act in actions:
            if act.get("type") not in {"kill", "shoot"}:
                continue
            actor = act.get("actor")
            tgt = act.get("target")
            if actor is None or tgt is None or actor in blocked:
                continue
            if actor not in alive or tgt not in alive:
                continue
            attempted.append((actor, tgt, act["type"]))

            if tgt in protected_by2 and protected_by2[tgt]:
                bg_actor, dies_on_guard = protected_by2[tgt][0]
                if bg_actor in alive and bg_actor not in blocked and bg_actor != tgt:
                    deaths.add(actor)
                    if dies_on_guard:
                        deaths.add(bg_actor)
                    continue

            defended = players[tgt].protected_tonight or tgt in healed_by or players[tgt].on_alert_tonight
            # ToS-like: Arsonist has basic defense (immune to normal kills).
            if players[tgt].role == "Arsonist":
                defended = True
            if day == 1 and players[tgt].role == "Witch" and not players[tgt].witch_shield_used and not defended:
                players[tgt].witch_shield_used = True
                defended = True
            if not defended:
                deaths.add(tgt)
            else:
                if tgt in healed_by:
                    heal_saved_target = tgt

        # Vigilante guilt (bot-aligned): only if the shot actually kills a Town member.
        for actor, tgt, typ in attempted:
            if typ == "shoot" and players[tgt].role in TOWN:
                if tgt in deaths:
                    players[actor].guilty_next_day = True

        # Apply deaths
        if collect_stats:
            # Count deaths that occur during night phase (including alert/BG/plunder/ignite/etc).
            stats["night_deaths"] += sum(1 for pid in deaths if pid in alive)
            if heal_saved_target is not None:
                stats["doc_saves"] += 1
        for pid in sorted(deaths):
            if pid in alive:
                players[pid].alive = False
                alive.discard(pid)
                if trace:
                    log.append(f"Night death: P{pid} ({players[pid].role})")
                # Add to Ret graveyard if Town
                if players[pid].role in TOWN:
                    # Gravedigger hide prevents Retributionist from using the corpse.
                    if pid not in hidden_by_gravedigger:
                        dead_town_corpses.append((pid, players[pid].role))

        # Executioner: if their target dies at night, they become a Jester (bot semantics).
        for p in players:
            if p.alive and p.role == "Executioner" and p.exe_target is not None:
                if p.exe_target in deaths:
                    p.role = "Jester"

        # Arsonist win checks (mirror game.py precedence: check Arsonist before faction wins).
        alive_ids_now = _alive_ids(players)
        arso_ids_now = [pid for pid in alive_ids_now if players[pid].role == "Arsonist"]
        if arso_ids_now:
            arso_id = arso_ids_now[0]
            if len(alive_ids_now) == 1:
                out["Arsonist"] = True
                break
            harmless_neutrals = {"Witch", "Survivor", "Executioner", "Jester", "Chaos"}
            others = [pid for pid in alive_ids_now if pid != arso_id]
            if others:
                other_roles = [players[pid].role for pid in others]
                if (not any(players[pid].role in MAFIA for pid in others)) and (not any(players[pid].role in TOWN for pid in others)) and all(
                    r in harmless_neutrals for r in other_roles
                ):
                    out["Arsonist"] = True
                    break

        # ---- Day: update evidence based on night info ----
        if trace:
            alive_list = ", ".join(f"P{pid}({players[pid].role})" for pid in sorted(alive))
            log.append(f"\n--- Day {day} ---")
            log.append(f"Alive: {alive_list}")
        # Mayor reveal: once the game progresses, Mayor may reveal to push decisive lynches.
        mayor_id = next((pid for pid in alive if players[pid].role == "Mayor"), None)
        if mayor_id is not None and not players[mayor_id].mayor_revealed:
            top = max((evidence.get(pid, 0) for pid in alive if pid != mayor_id), default=0)
            # Reveal when there's meaningful suspicion on the board, or later in the game.
            if day >= 3 or top >= 3:
                if random.random() < 0.5:
                    players[mayor_id].mayor_revealed = True

        # Executioner pressure: tries to get their target lynched by pushing suspicion daily.
        for p in players:
            if p.alive and p.role == "Executioner" and p.exe_target is not None and p.exe_target in alive:
                evidence[p.exe_target] = evidence.get(p.exe_target, 0) + 1

        # Jester behavior: acts scummy / draws heat (increases odds of being lynched).
        for p in players:
            if p.alive and p.role == "Jester":
                evidence[p.i] = evidence.get(p.i, 0) + 1

        # (All investigative evidence + night kills are applied during the night pipeline above.)

        # Check win after daybreak deaths/shots.
        w = check_main_winner(players)
        if w:
            out[w] = True
            break

        # Vigilante guilt deaths happen during the day (bot: "next day").
        for p in players:
            if p.alive and p.role == "Vigilante" and p.guilty_next_day:
                p.alive = False
                alive.discard(p.i)
                p.guilty_next_day = False

        # ---- Tribunal / lynch ----
        # Town day-play competence: even with the same evidence, low-skill lobbies mislynch more.
        town_c = _town_competence(players, alive)
        lynch_good = town_lynch_decision(players, alive, evidence)
        lynch_rnd = pick_alive(players, alive, exclude=set())
        lynch = _pick_with_competence(competence=town_c, good_choice=lynch_good, random_choice=lynch_rnd if no_lynch_streak >= 1 else None)
        if lynch is None:
            no_lynch_streak += 1
        else:
            no_lynch_streak = 0

        # If Mayor is revealed, Town is more decisive: after 1 no-lynch, force a lynch.
        if lynch is None and mayor_id is not None and players[mayor_id].mayor_revealed and no_lynch_streak >= 1:
            lynch = town_lynch_decision_desperate(players, alive, evidence)
            no_lynch_streak = 0

        # Desperation: after repeated no-lynch days, force a lynch to keep games ending.
        if lynch is None and no_lynch_streak >= 2:
            lynch = town_lynch_decision_desperate(players, alive, evidence)
            no_lynch_streak = 0

        if lynch is not None:
            players[lynch].alive = False
            alive.discard(lynch)
            if trace:
                log.append(f"Lynch: P{lynch} ({players[lynch].role})")
            if collect_stats:
                stats["lynches"] += 1
                if faction(players[lynch].role) != "Mafia":
                    # "mislynch" here means Town did not eliminate Mafia.
                    stats["mislynches"] += 1
            # Jester wins if lynched (personal win; game continues).
            if players[lynch].role == "Jester":
                out["Jester"] = True
                pending_haunts.append(lynch)
            # Executioner wins immediately if their target is lynched while Executioner is alive.
            for p in players:
                if p.alive and p.role == "Executioner" and p.exe_target == lynch:
                    out["Executioner"] = True
                    # In this bot ruleset, Executioner win ends the game immediately.
                    day = max_days + 1
                    break
            # If Town lynched a townie, reduce confidence a bit (but keep simple).
            if faction(players[lynch].role) == "Town":
                # Randomly distribute some uncertainty.
                for pid in list(alive):
                    evidence[pid] = max(0, evidence.get(pid, 0) - 1)

        if out["Executioner"]:
            break

        w = check_main_winner(players)
        if w:
            out[w] = True
            break

        day += 1
        if collect_stats:
            stats["days"] += 1

    if not (out["Town"] or out["Mafia"] or out["Executioner"]):
        # If the main game never resolved under our simplified policy, mark draw
        # ONLY if no other personal win condition triggered.
        if not (out.get("Jester") or out.get("Pirate") or out.get("Arsonist")):
            out["Draw"] = True

    # Survivor personal win: if Survivor is alive when the game ends (Town/Mafia/Executioner end condition).
    if out["Town"] or out["Mafia"] or out["Executioner"]:
        alive_ids = _alive_ids(players)
        for p in players:
            if p.role == "Survivor" and p.i in alive_ids:
                out["Survivor"] = True
                break
        for p in players:
            if p.role == "Chaos" and p.i in alive_ids:
                out["Chaos"] = True
                break

    # Pirate personal win: 2 duel wins (we only model single pirate).
    for p in players:
        if p.role == "Pirate" and p.pirate_wins >= 2:
            out["Pirate"] = True
            break

    # Arsonist personal win: arsonist is the only living killer (simplified: last alive OR only neutrals left).
    alive_ids = _alive_ids(players)
    arso_alive = [pid for pid in alive_ids if players[pid].role == "Arsonist"]
    if arso_alive:
        arso = arso_alive[0]
        others = [pid for pid in alive_ids if pid != arso]
        if not others:
            out["Arsonist"] = True
        else:
            # If only neutrals remain besides arso, treat as arso win (stalemate breaker).
            if all(players[pid].role in NEUTRAL and players[pid].role not in {"Pirate", "Arsonist"} for pid in others):
                out["Arsonist"] = True

    if collect_stats:
        return out, stats
    if trace:
        log.append("\nOutcome flags:")
        for k in ["Town", "Mafia", "Executioner", "Jester", "Survivor", "Pirate", "Arsonist", "Chaos", "Draw"]:
            if out.get(k):
                log.append(f"  {k}=True")
        log.append("=== TRACE END ===")
        return out, log
    return out


def run_monte_carlo(roles: List[str], n: int, seed: int) -> Dict[str, float]:
    random.seed(seed)
    counts: Dict[str, int] = {
        "Town": 0,
        "Mafia": 0,
        "Draw": 0,
        "Executioner": 0,
        "Jester": 0,
        "Survivor": 0,
        "Pirate": 0,
        "Arsonist": 0,
        "Chaos": 0,
    }
    for _ in range(n):
        # Reshuffle seat order (matters only if heuristics pick first role, etc.)
        rr = roles[:]
        random.shuffle(rr)
        res = simulate_once(rr)
        for k, v in res.items():
            if v:
                counts[k] = counts.get(k, 0) + 1
    return {k: v / n for k, v in counts.items()}


def audit_against_bot_config() -> None:
    """
    Fast coverage audit: ensure the simulator's role universe matches the bot's config lists.
    This doesn't prove perfect behavioral fidelity, but it prevents silent omissions when roles change.
    """
    # Ensure repo root is importable when executed as scripts/monte_carlo_sim.py
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    import config as bot_config  # noqa: E402

    bot_roles = set(bot_config.TOWN_ROLES) | set(bot_config.ALL_MAFIA_ROLES) | {
        "Survivor",
        "Executioner",
        "Jester",
        "Witch",
        "Pirate",
        "Arsonist",
        "Chaos",
    }
    sim_roles = set(TOWN) | set(MAFIA) | set(NEUTRAL)
    allowed_extra = {"Civilian"}
    missing = sorted(bot_roles - sim_roles)
    extra = sorted((sim_roles - bot_roles) - allowed_extra)
    if missing or extra:
        raise SystemExit(f"SIM AUDIT FAILED. missing={missing} extra={extra}")
    print("SIM AUDIT OK: role sets match config.py")


def _bot_town_weights(player_count: int) -> List[Tuple[str, int]]:
    # Mirrors bot.py (weights change at 7+)
    if player_count <= 6:
        return [
            ("Doctor", 8),
            ("Sheriff", 8),
            ("Investigator", 6),
            ("Lookout", 7),
            ("Tracker", 7),
            ("Escort", 7),
            ("Vigilante", 6),
            ("Retributionist", 3),
        ]
    return [
        ("Doctor", 8),
        ("Sheriff", 8),
        ("Investigator", 6),
        ("Lookout", 7),
        ("Tracker", 7),
        ("Escort", 7),
        ("Bodyguard", 6),
        ("Vigilante", 6),
        ("Scary Grandma", 5),
        ("Transporter", 4),
        ("Mayor", 3),
        ("Retributionist", 3),
    ]


def _bot_mafia_support_weights(player_count: int) -> List[Tuple[str, int]]:
    # Mirrors bot.py (pool changes at 7+)
    if player_count <= 6:
        return [("Gravedigger", 8), ("Consort", 7), ("Framer", 6)]
    return [
        ("Gatekeeper", 8),
        ("Consort", 8),
        ("Framer", 7),
        ("Gravedigger", 6),
        ("Hypnotist", 5),
        ("Mole", 5),
        ("Tailor", 4),
    ]


def _bot_neutral_pool(player_count: int) -> List[str]:
    if player_count <= 6:
        return ["Jester", "Executioner", "Survivor"]
    return ["Jester", "Executioner", "Survivor", "Witch", "Pirate", "Arsonist", "Chaos"]


def _bot_num_mafia_neutral(player_count: int) -> Tuple[int, int]:
    # bot.py:
    # (1,1) if <=6 else (2,1) if <=9 else (3,2) if <=12 else (4,2)
    if player_count <= 6:
        return 1, 1
    if player_count <= 9:
        return 2, 1
    if player_count <= 12:
        return 3, 2
    return 4, 2


def _weighted_without_replacement(pool: List[Tuple[str, int]], count: int) -> List[str]:
    selected: List[str] = []
    names = [r for r, _w in pool]
    weights = [w for _r, w in pool]
    for _ in range(count):
        if not names:
            break
        chosen = random.choices(names, weights=weights, k=1)[0]
        selected.append(chosen)
        i = names.index(chosen)
        names.pop(i)
        weights.pop(i)
    return selected


def _bot_choose_neutrals(player_count: int, neutral_pool: List[str], num_neutral: int, num_mafia: int) -> List[str]:
    # Mirrors bot.py's shuffle + constraints.
    pool = neutral_pool[:]
    if (player_count - num_mafia) <= 1 and "Executioner" in pool:
        pool.remove("Executioner")

    random.shuffle(pool)
    chosen: List[str] = []
    killing_count = 0
    disruptive_count = 0
    for r in pool:
        if len(chosen) == num_neutral:
            break
        if r in ["Arsonist", "Pirate"]:
            if killing_count < 1:
                killing_count += 1
                chosen.append(r)
        elif r in ["Witch", "Executioner"]:
            if disruptive_count < 1:
                disruptive_count += 1
                chosen.append(r)
        else:
            chosen.append(r)
    return chosen


def sample_generator_roles(player_count: int, *, mafia_override: Optional[int] = None, neutral_override: Optional[int] = None) -> List[str]:
    """
    Sample ONE role-set like bot.py does (supports 5p+), enforcing "no duplicate roles"
    via sampling without replacement.
    """
    num_mafia, num_neutral = _bot_num_mafia_neutral(player_count)
    if mafia_override is not None:
        num_mafia = int(mafia_override)
    if neutral_override is not None:
        num_neutral = int(neutral_override)
    num_town = player_count - num_mafia - num_neutral
    if num_town < 0:
        return sample_generator_roles(player_count, mafia_override=mafia_override, neutral_override=neutral_override)

    neutrals = _bot_choose_neutrals(player_count, _bot_neutral_pool(player_count), num_neutral, num_mafia)
    town_roles = _weighted_without_replacement(_bot_town_weights(player_count), num_town)
    mafia_roles = ["Mobster"]
    if num_mafia > 1:
        mafia_roles += _weighted_without_replacement(_bot_mafia_support_weights(player_count), num_mafia - 1)

    roles = neutrals + town_roles + mafia_roles
    if len(roles) != player_count:
        # If pools were too small for some reason, fall back to retry.
        return sample_generator_roles(player_count, mafia_override=mafia_override, neutral_override=neutral_override)
    if len(set(roles)) != len(roles):
        # bot hard-fails on duplicates; we just retry sampling.
        return sample_generator_roles(player_count, mafia_override=mafia_override, neutral_override=neutral_override)
    return roles


def sample_generator_roles_constraints(
    player_count: int,
    *,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_investigative: bool = False,
    require_doctor: bool = False,
    require_protective: bool = False,
    include_roles: Optional[Set[str]] = None,
    exclude_roles: Optional[Set[str]] = None,
    mafia_override: Optional[int] = None,
    neutral_override: Optional[int] = None,
) -> List[str]:
    while True:
        roles = sample_generator_roles(player_count, mafia_override=mafia_override, neutral_override=neutral_override)
        if include_roles and not include_roles.issubset(set(roles)):
            continue
        if exclude_roles and any(r in roles for r in exclude_roles):
            continue
        town_roles = [r for r in roles if r in TOWN]
        inv_count = sum(1 for r in town_roles if r in INVESTIGATIVE)
        if max_investigative is not None and inv_count > max_investigative:
            continue
        if exact_investigative is not None and inv_count != exact_investigative:
            continue
        if require_investigative and inv_count < 1:
            continue
        if require_doctor and "Doctor" not in town_roles:
            continue
        if require_protective and not any(r in PROTECTIVE for r in town_roles):
            continue
        return roles


def run_generator_weighted_trials(
    player_count: int,
    *,
    trials: int,
    seed: int,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_investigative: bool = False,
    require_doctor: bool = False,
    require_protective: bool = False,
    include_roles: Optional[Set[str]] = None,
    exclude_roles: Optional[Set[str]] = None,
    mafia_override: Optional[int] = None,
    neutral_override: Optional[int] = None,
    diagnostics: bool = False,
) -> Dict[str, float]:
    """
    Directly sample from the role generator and simulate once per draw.
    This is generator-weighted by construction and scales to larger player counts.
    """
    random.seed(seed)
    counts: Dict[str, int] = {
        "Town": 0,
        "Mafia": 0,
        "Draw": 0,
        "Executioner": 0,
        "Jester": 0,
        "Survivor": 0,
        "Pirate": 0,
        "Arsonist": 0,
        "Chaos": 0,
    }
    # Diagnostics aggregation (optional)
    diag_sum: SimStats = {
        "days": 0,
        "lynches": 0,
        "mislynches": 0,
        "night_deaths": 0,
        "doc_saves": 0,
        "roleblocks": 0,
        "gatekeeper_blocks": 0,
        "controls": 0,
        "controls_prevent_ignite": 0,
        "ignites": 0,
    }
    town_when_mislynch = 0
    mafia_when_mislynch = 0
    town_when_no_mislynch = 0
    mafia_when_no_mislynch = 0

    total_days = 0
    for _ in range(trials):
        roles = sample_generator_roles_constraints(
            player_count,
            max_investigative=max_investigative,
            exact_investigative=exact_investigative,
            require_investigative=require_investigative,
            require_doctor=require_doctor,
            require_protective=require_protective,
            include_roles=include_roles,
            exclude_roles=exclude_roles,
            mafia_override=mafia_override,
            neutral_override=neutral_override,
        )
        # shuffle seats
        rr = roles[:]
        random.shuffle(rr)
        # Always collect stats so we can report average game length (days).
        res, st = cast(Tuple[Dict[str, bool], SimStats], simulate_once(rr, collect_stats=True))
        total_days += int(st.get("days", 0))
        if diagnostics:
            for k in diag_sum.keys():
                diag_sum[k] += int(st.get(k, 0))
            if st.get("mislynches", 0) > 0:
                town_when_mislynch += 1 if res.get("Town") else 0
                mafia_when_mislynch += 1 if res.get("Mafia") else 0
            else:
                town_when_no_mislynch += 1 if res.get("Town") else 0
                mafia_when_no_mislynch += 1 if res.get("Mafia") else 0
        for k, v in res.items():
            if v:
                counts[k] += 1
    probs = {k: v / trials for k, v in counts.items()}
    probs["avg_days"] = total_days / trials if trials else 0.0

    if diagnostics:
        print("Diagnostics (averages per game):")
        for k in ["days", "lynches", "mislynches", "night_deaths", "doc_saves", "roleblocks", "gatekeeper_blocks", "controls", "ignites"]:
            print(f"  {k}: {diag_sum.get(k, 0) / trials:.3f}")
        print("Conditional faction WR (diagnostic):")
        print(f"  P(Town win | mislynch>=1): {town_when_mislynch / trials:.3f}")
        print(f"  P(Mafia win | mislynch>=1): {mafia_when_mislynch / trials:.3f}")
        print(f"  P(Town win | mislynch=0): {town_when_no_mislynch / trials:.3f}")
        print(f"  P(Mafia win | mislynch=0): {mafia_when_no_mislynch / trials:.3f}")

    return probs

def generator_role_distribution(
    player_count: int,
    *,
    trials: int,
    seed: int,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_investigative: bool = False,
    require_doctor: bool = False,
    require_protective: bool = False,
    include_roles: Optional[Set[str]] = None,
    exclude_roles: Optional[Set[str]] = None,
    mafia_override: Optional[int] = None,
    neutral_override: Optional[int] = None,
) -> Dict[str, object]:
    """
    Sample role-lists from the generator and report role frequencies.
    Returns a dict with:
      - role_counts: role -> total appearances across all sampled lobbies
      - lobby_counts: role_set_key -> count (for the most common sets, typically)
    """
    random.seed(seed)
    role_counts: Dict[str, int] = {}
    lobby_counts: Dict[str, int] = {}
    for _ in range(trials):
        roles = sample_generator_roles_constraints(
            player_count,
            max_investigative=max_investigative,
            exact_investigative=exact_investigative,
            require_investigative=require_investigative,
            require_doctor=require_doctor,
            require_protective=require_protective,
            include_roles=include_roles,
            exclude_roles=exclude_roles,
            mafia_override=mafia_override,
            neutral_override=neutral_override,
        )
        for r in roles:
            role_counts[r] = role_counts.get(r, 0) + 1
        key = ", ".join(sorted(roles))
        lobby_counts[key] = lobby_counts.get(key, 0) + 1

    return {"role_counts": role_counts, "lobby_counts": lobby_counts}

def _stable_seed(base_seed: int, roles: List[str]) -> int:
    s = "|".join(sorted(roles)).encode("utf-8")
    h = hashlib.blake2b(s, digest_size=8).digest()
    mix = int.from_bytes(h, "little", signed=False)
    return (base_seed ^ mix) & 0x7FFFFFFF


def enumerate_role_sets(player_count: int) -> List[List[str]]:
    """
    Enumerate distinct *role compositions* that the bot can generate at 5p/6p
    under the current rules:
      - Mobster always present
      - 1 Neutral from {Jester, Executioner, Survivor}
      - Town roles drawn without duplicates from the 8-town pool
    """
    if player_count not in {5, 6}:
        raise ValueError("enumerate_role_sets currently supports only 5 or 6 players.")

    town_pool = [
        "Doctor",
        "Sheriff",
        "Investigator",
        "Lookout",
        "Tracker",
        "Escort",
        "Vigilante",
        "Retributionist",
    ]
    neutral_pool = ["Jester", "Executioner", "Survivor"]
    num_mafia = 1
    num_neutral = 1
    num_town = player_count - num_mafia - num_neutral

    out: List[List[str]] = []
    for neutral in neutral_pool:
        for town_combo in itertools.combinations(town_pool, num_town):
            roles = [neutral, "Mobster", *town_combo]
            out.append(list(roles))
    return out


INVESTIGATIVE_5_6 = {"Sheriff", "Investigator", "Lookout", "Tracker"}
PROTECTIVE_5_6 = {"Doctor"}


def _extract_town_5_6(roles: List[str]) -> List[str]:
    return [r for r in roles if r not in {"Mobster", "Jester", "Executioner", "Survivor"}]


def enumerate_role_sets_constraints_5_6(
    player_count: int,
    *,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_doctor: bool = False,
) -> List[List[str]]:
    """
    Enumerate 5p/6p role-sets and filter by constraints.
    Constraints apply to town roles only (5–6p pool).
    """
    sets = enumerate_role_sets(player_count)
    out: List[List[str]] = []
    for roles in sets:
        town_roles = _extract_town_5_6(roles)
        inv_count = sum(1 for r in town_roles if r in INVESTIGATIVE_5_6)

        if max_investigative is not None and inv_count > max_investigative:
            continue
        if exact_investigative is not None and inv_count != exact_investigative:
            continue
        if require_doctor and "Doctor" not in town_roles:
            continue

        out.append(roles)
    return out


def _town_weights_5_6() -> List[Tuple[str, int]]:
    # Mirrors bot.py for player_count <= 6
    return [
        ("Doctor", 8),
        ("Sheriff", 8),
        ("Investigator", 6),
        ("Lookout", 7),
        ("Tracker", 7),
        ("Escort", 7),
        ("Vigilante", 6),
        ("Retributionist", 3),
    ]


def _neutral_pool_5_6() -> List[str]:
    # Mirrors bot.py for player_count <= 6
    return ["Jester", "Executioner", "Survivor"]


def _sample_generator_roles_5_6(player_count: int) -> List[str]:
    """
    Sample ONE role-set exactly like bot.py does for 5p/6p:
      - 1 Mobster
      - 1 Neutral uniformly from neutral_pool (shuffle + take 1)
      - Town roles chosen via weighted sampling WITHOUT replacement (random.choices each draw).
      - No duplicates (enforced by construction here).
    """
    if player_count not in {5, 6}:
        raise ValueError("Generator sampler supports only 5 or 6 players.")

    num_mafia, num_neutral = (1, 1)
    num_town = player_count - num_mafia - num_neutral

    neutral_pool = _neutral_pool_5_6()[:]
    random.shuffle(neutral_pool)
    neutral = neutral_pool[0]

    town_pool = _town_weights_5_6()
    names = [r for r, _w in town_pool]
    weights = [w for _r, w in town_pool]
    town_roles: List[str] = []
    for _ in range(num_town):
        chosen = random.choices(names, weights=weights, k=1)[0]
        town_roles.append(chosen)
        i = names.index(chosen)
        names.pop(i)
        weights.pop(i)

    return [neutral, "Mobster", *town_roles]


def _sample_generator_roles_5_6_one_investigative(player_count: int) -> List[str]:
    """
    Sample ONE role-set like bot.py, but with an additional constraint:
    town roles contain <= 1 investigative role among {Sheriff, Investigator, Lookout, Tracker}.
    Rejection-samples until it finds a valid set.
    """
    while True:
        roles = _sample_generator_roles_5_6(player_count)
        town_roles = _extract_town_5_6(roles)
        if sum(1 for r in town_roles if r in INVESTIGATIVE_5_6) <= 1:
            return roles


def estimate_generation_weights(player_count: int, *, samples: int, seed: int) -> Dict[str, float]:
    """
    Estimate P(role_set) under the role generator (not gameplay) via Monte Carlo sampling.
    Returned key is the same string format used in CSV: ', '.join(sorted(roles)).
    """
    random.seed(seed)
    counts: Dict[str, int] = {}
    for _ in range(samples):
        roles = _sample_generator_roles_5_6(player_count)
        key = ", ".join(sorted(roles))
        counts[key] = counts.get(key, 0) + 1
    return {k: v / samples for k, v in counts.items()}


def estimate_generation_weights_one_investigative(player_count: int, *, samples: int, seed: int) -> Dict[str, float]:
    random.seed(seed)
    counts: Dict[str, int] = {}
    for _ in range(samples):
        roles = _sample_generator_roles_5_6_one_investigative(player_count)
        key = ", ".join(sorted(roles))
        counts[key] = counts.get(key, 0) + 1
    return {k: v / samples for k, v in counts.items()}

def _sample_generator_roles_5_6_constraints(
    player_count: int,
    *,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_doctor: bool = False,
) -> List[str]:
    while True:
        roles = _sample_generator_roles_5_6(player_count)
        town_roles = _extract_town_5_6(roles)
        inv_count = sum(1 for r in town_roles if r in INVESTIGATIVE_5_6)
        if max_investigative is not None and inv_count > max_investigative:
            continue
        if exact_investigative is not None and inv_count != exact_investigative:
            continue
        if require_doctor and "Doctor" not in town_roles:
            continue
        return roles


def estimate_generation_weights_constraints(
    player_count: int,
    *,
    samples: int,
    seed: int,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_doctor: bool = False,
) -> Dict[str, float]:
    random.seed(seed)
    counts: Dict[str, int] = {}
    for _ in range(samples):
        roles = _sample_generator_roles_5_6_constraints(
            player_count,
            max_investigative=max_investigative,
            exact_investigative=exact_investigative,
            require_doctor=require_doctor,
        )
        key = ", ".join(sorted(roles))
        counts[key] = counts.get(key, 0) + 1
    return {k: v / samples for k, v in counts.items()}


def run_enumeration(
    player_count: int,
    *,
    n_per: int,
    seed: int,
    out_csv: Path,
    one_investigative: bool = False,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_doctor: bool = False,
) -> None:
    if one_investigative:
        sets = enumerate_role_sets_constraints_5_6(player_count, max_investigative=1)
    elif max_investigative is not None or exact_investigative is not None or require_doctor:
        sets = enumerate_role_sets_constraints_5_6(
            player_count,
            max_investigative=max_investigative,
            exact_investigative=exact_investigative,
            require_doctor=require_doctor,
        )
    else:
        sets = enumerate_role_sets(player_count)

    # Estimate generation weights (how often your role generator produces each set).
    gen_samples = 200_000 if player_count == 5 else 300_000
    gen_seed = seed ^ 0xA5A5A5A5
    if one_investigative:
        gen_w = estimate_generation_weights_constraints(player_count, samples=gen_samples, seed=gen_seed, max_investigative=1)
    elif max_investigative is not None or exact_investigative is not None or require_doctor:
        gen_w = estimate_generation_weights_constraints(
            player_count,
            samples=gen_samples,
            seed=gen_seed,
            max_investigative=max_investigative,
            exact_investigative=exact_investigative,
            require_doctor=require_doctor,
        )
    else:
        gen_w = estimate_generation_weights(player_count, samples=gen_samples, seed=gen_seed)

    rows: List[Dict[str, object]] = []
    for roles in sets:
        key = ", ".join(sorted(roles))
        probs = run_monte_carlo(roles, n_per, _stable_seed(seed, roles))
        rows.append(
            {
                "player_count": player_count,
                "n_per": n_per,
                "roles": key,
                "gen_weight": gen_w.get(key, 0.0),
                "town": probs.get("Town", 0.0),
                "mafia": probs.get("Mafia", 0.0),
                "draw": probs.get("Draw", 0.0),
                "exe": probs.get("Executioner", 0.0),
                "jester": probs.get("Jester", 0.0),
                "survivor": probs.get("Survivor", 0.0),
                "pirate": probs.get("Pirate", 0.0),
                "arsonist": probs.get("Arsonist", 0.0),
            }
        )

    # Overall averages (simple average across compositions).
    avg_town = sum(float(r["town"]) for r in rows) / len(rows)
    avg_mafia = sum(float(r["mafia"]) for r in rows) / len(rows)
    avg_draw = sum(float(r["draw"]) for r in rows) / len(rows)
    avg_exe = sum(float(r["exe"]) for r in rows) / len(rows)
    avg_jester = sum(float(r["jester"]) for r in rows) / len(rows)
    avg_surv = sum(float(r["survivor"]) for r in rows) / len(rows)
    avg_pirate = sum(float(r["pirate"]) for r in rows) / len(rows)
    avg_arso = sum(float(r["arsonist"]) for r in rows) / len(rows)

    # Weighted averages by generator probability.
    total_w = sum(float(r["gen_weight"]) for r in rows) or 1.0
    w_town = sum(float(r["gen_weight"]) * float(r["town"]) for r in rows) / total_w
    w_mafia = sum(float(r["gen_weight"]) * float(r["mafia"]) for r in rows) / total_w
    w_draw = sum(float(r["gen_weight"]) * float(r["draw"]) for r in rows) / total_w
    w_exe = sum(float(r["gen_weight"]) * float(r["exe"]) for r in rows) / total_w
    w_jester = sum(float(r["gen_weight"]) * float(r["jester"]) for r in rows) / total_w
    w_surv = sum(float(r["gen_weight"]) * float(r["survivor"]) for r in rows) / total_w
    w_pirate = sum(float(r["gen_weight"]) * float(r["pirate"]) for r in rows) / total_w
    w_arso = sum(float(r["gen_weight"]) * float(r["arsonist"]) for r in rows) / total_w

    rows_sorted = sorted(rows, key=lambda r: float(r["town"]), reverse=True)
    top10 = rows_sorted[:10]
    bot10 = list(reversed(rows_sorted[-10:]))

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "player_count",
                "n_per",
                "roles",
                "gen_weight",
                "town",
                "mafia",
                "draw",
                "exe",
                "jester",
                "survivor",
                "pirate",
                "arsonist",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    suffix = " (<=1 investigative town role)" if one_investigative else ""
    print(f"Enumerated {len(rows)} role-sets for {player_count} players{suffix}.")
    print(f"Rollouts per set: {n_per}")
    print(
        "Overall avg (unweighted): "
        f"Town={avg_town:.3f} Mafia={avg_mafia:.3f} Draw={avg_draw:.3f} "
        f"Exe={avg_exe:.3f} Jester={avg_jester:.3f} Survivor={avg_surv:.3f}"
        f" Pirate={avg_pirate:.3f} Arsonist={avg_arso:.3f}"
    )
    print(
        "Overall avg (weighted by generator): "
        f"Town={w_town:.3f} Mafia={w_mafia:.3f} Draw={w_draw:.3f} "
        f"Exe={w_exe:.3f} Jester={w_jester:.3f} Survivor={w_surv:.3f}"
        f" Pirate={w_pirate:.3f} Arsonist={w_arso:.3f}"
    )
    print(f"Saved CSV: {out_csv}")

    print("\nTop 10 Town-favored sets (by Town winrate):")
    for r in top10:
        print(
            f"  Town={float(r['town']):.3f}  Mafia={float(r['mafia']):.3f}  Draw={float(r['draw']):.3f}  "
            f"Exe={float(r['exe']):.3f}  Jes={float(r['jester']):.3f}  Surv={float(r['survivor']):.3f}  "
            f"Pir={float(r['pirate']):.3f}  Arso={float(r['arsonist']):.3f}  :: {r['roles']}"
        )

    print("\nTop 10 Mafia-favored sets (by Town winrate, ascending):")
    for r in bot10:
        print(
            f"  Town={float(r['town']):.3f}  Mafia={float(r['mafia']):.3f}  Draw={float(r['draw']):.3f}  "
            f"Exe={float(r['exe']):.3f}  Jes={float(r['jester']):.3f}  Surv={float(r['survivor']):.3f}  "
            f"Pir={float(r['pirate']):.3f}  Arso={float(r['arsonist']):.3f}  :: {r['roles']}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000, help="Rollouts for a single fixed role list (use with --roles).")
    ap.add_argument("--enumerate", type=int, choices=[5, 6], help="Enumerate all role-sets for this player count.")
    ap.add_argument("--generator-trials", type=int, default=0, help="If set, sample from the role generator this many times (supports 5p+).")
    ap.add_argument("--player-count", type=int, default=0, help="Player count for --generator-trials mode.")
    ap.add_argument(
        "--generator-distribution",
        action="store_true",
        help="In generator-trials mode, print role frequency distribution instead of win rates.",
    )
    ap.add_argument("--n-per", type=int, default=1000, help="Rollouts per role-set when using --enumerate.")
    ap.add_argument("--seed", type=int, default=20260429)
    ap.add_argument("--roles", nargs="+", help="Explicit roles for a single simulation run.")
    ap.add_argument("--audit", action="store_true", help="Fail fast if simulator role sets diverge from config.py.")
    ap.add_argument(
        "--lobby-skill",
        type=float,
        default=0.5,
        help="Difficulty normalization knob in [0,1]. 0=average play, 1=competent play. Default 0.5 (mid).",
    )
    ap.add_argument("--no-difficulty", action="store_true", help="Disable the role difficulty layer (treat decisions as competent/optimal).")
    ap.add_argument(
        "--gatekeeper-blocks-one",
        action="store_true",
        help="Balance toggle: Gatekeeper guard blocks only 1 random eligible non-mafia visitor (instead of all).",
    )
    ap.add_argument("--out-csv", default="", help="CSV output path for --enumerate (optional).")
    ap.add_argument(
        "--one-investigative",
        action="store_true",
        help="For 5p/6p enumeration: allow <= 1 investigative town role (Sheriff/Investigator/Lookout/Tracker).",
    )
    ap.add_argument("--max-investigative", type=int, default=None, help="For 5p/6p enumeration: allow <= N investigative town roles.")
    ap.add_argument("--exact-investigative", type=int, default=None, help="For 5p/6p enumeration: require exactly N investigative town roles.")
    ap.add_argument("--require-investigative", action="store_true", help="Require at least one investigative Town role.")
    ap.add_argument("--require-doctor", action="store_true", help="For 5p/6p enumeration: require Doctor to be present (protective role).")
    ap.add_argument(
        "--require-protective",
        action="store_true",
        help="Require at least one protective Town role (Doctor or Bodyguard). Intended for 7p+ generator trials.",
    )
    ap.add_argument("--include-role", action="append", default=[], help="In generator-trials mode: require this role to be present (repeatable).")
    ap.add_argument("--exclude-role", action="append", default=[], help="In generator-trials mode: forbid this role (repeatable).")
    ap.add_argument("--mafia-override", type=int, default=None, help="Force the generator to use this many Mafia (for testing).")
    ap.add_argument("--neutral-override", type=int, default=None, help="Force the generator to use this many Neutrals (for testing).")
    ap.add_argument("--diagnostics", action="store_true", help="In generator-trials mode: print attribution diagnostics (mislynches, blocks, saves, etc.).")
    ap.add_argument("--trace-one", action="store_true", help="Run exactly one sampled game and print a day-by-day trace (generator-trials mode only).")
    args = ap.parse_args()
    global LOBBY_SKILL
    LOBBY_SKILL = _clamp01(float(args.lobby_skill))
    global USE_DIFFICULTY_LAYER
    USE_DIFFICULTY_LAYER = not bool(args.no_difficulty)
    global GATEKEEPER_BLOCKS_ONE
    GATEKEEPER_BLOCKS_ONE = bool(args.gatekeeper_blocks_one)

    if args.audit:
        audit_against_bot_config()
        return

    if args.generator_trials:
        if not args.player_count:
            raise SystemExit("Provide --player-count with --generator-trials.")
        max_inv = args.max_investigative if args.max_investigative is not None else (1 if args.one_investigative else None)
        include_roles = set(args.include_role or [])
        exclude_roles = set(args.exclude_role or [])
        if args.trace_one:
            roles = sample_generator_roles_constraints(
                args.player_count,
                max_investigative=max_inv,
                exact_investigative=args.exact_investigative,
                require_investigative=bool(args.require_investigative),
                require_doctor=bool(args.require_doctor),
                require_protective=bool(args.require_protective),
                include_roles=include_roles,
                exclude_roles=exclude_roles,
                mafia_override=args.mafia_override,
                neutral_override=args.neutral_override,
            )
            rr = roles[:]
            random.seed(args.seed)
            random.shuffle(rr)
            out, trace_log = cast(Tuple[Dict[str, bool], List[str]], simulate_once(rr, trace=True))
            print("\n".join(trace_log))
            print("\nSummary:", {k: v for k, v in out.items() if v})
            return

        if args.generator_distribution:
            dist = generator_role_distribution(
                args.player_count,
                trials=args.generator_trials,
                seed=args.seed,
                max_investigative=max_inv,
                exact_investigative=args.exact_investigative,
                require_investigative=bool(args.require_investigative),
                require_doctor=bool(args.require_doctor),
                require_protective=bool(args.require_protective),
                include_roles=include_roles,
                exclude_roles=exclude_roles,
                mafia_override=args.mafia_override,
                neutral_override=args.neutral_override,
            )
            role_counts: Dict[str, int] = dist["role_counts"]  # type: ignore[assignment]
            lobby_counts: Dict[str, int] = dist["lobby_counts"]  # type: ignore[assignment]
            print(f"Generator distribution: player_count={args.player_count} trials={args.generator_trials}")
            # Role appearance rate per lobby (count / trials).
            for role, cnt in sorted(role_counts.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"{role}: {cnt / args.generator_trials:.4f}")
            # Also print top 10 most common exact role-sets.
            top_sets = sorted(lobby_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
            print("\nTop 10 role-sets:")
            for key, cnt in top_sets:
                print(f"{cnt / args.generator_trials:.4f} :: {key}")
        else:
            probs = run_generator_weighted_trials(
                args.player_count,
                trials=args.generator_trials,
                seed=args.seed,
                max_investigative=max_inv,
                exact_investigative=args.exact_investigative,
                require_investigative=bool(args.require_investigative),
                require_doctor=bool(args.require_doctor),
                require_protective=bool(args.require_protective),
                include_roles=include_roles,
                exclude_roles=exclude_roles,
                mafia_override=args.mafia_override,
                neutral_override=args.neutral_override,
                diagnostics=bool(args.diagnostics),
            )
            print(f"Generator-weighted trials: player_count={args.player_count} trials={args.generator_trials}")
            print(
                f"avg_days={probs.get('avg_days', 0.0):.2f} "
                f"Town={probs.get('Town', 0.0):.3f} Mafia={probs.get('Mafia', 0.0):.3f} Draw={probs.get('Draw', 0.0):.3f} "
                f"Exe={probs.get('Executioner', 0.0):.3f} Jester={probs.get('Jester', 0.0):.3f} Survivor={probs.get('Survivor', 0.0):.3f} "
                f"Pirate={probs.get('Pirate', 0.0):.3f} Arsonist={probs.get('Arsonist', 0.0):.3f} Chaos={probs.get('Chaos', 0.0):.3f}"
            )
        return

    if args.enumerate:
        out_csv = Path(args.out_csv) if args.out_csv else Path(f"scripts/monte_carlo_{args.enumerate}p.csv")
        run_enumeration(
            args.enumerate,
            n_per=args.n_per,
            seed=args.seed,
            out_csv=out_csv,
            one_investigative=bool(args.one_investigative),
            max_investigative=args.max_investigative,
            exact_investigative=args.exact_investigative,
            require_doctor=bool(args.require_doctor),
        )
        return

    if not args.roles:
        raise SystemExit("Provide --roles (single run) or --enumerate (bulk run).")

    # For fixed-role runs, also report average game length (days).
    random.seed(args.seed)
    total_days = 0
    counts: Dict[str, int] = {
        "Town": 0,
        "Mafia": 0,
        "Draw": 0,
        "Executioner": 0,
        "Jester": 0,
        "Survivor": 0,
        "Pirate": 0,
        "Arsonist": 0,
        "Chaos": 0,
    }
    for _ in range(args.n):
        rr = args.roles[:]
        random.shuffle(rr)
        res, st = cast(Tuple[Dict[str, bool], SimStats], simulate_once(rr, collect_stats=True))
        total_days += int(st.get("days", 0))
        for k, v in res.items():
            if v:
                counts[k] = counts.get(k, 0) + 1
    probs = {k: v / args.n for k, v in counts.items()}
    probs["avg_days"] = total_days / args.n if args.n else 0.0
    print("Roles:", ", ".join(args.roles))
    print(f"avg_days: {probs.get('avg_days', 0.0):.2f}")
    for k in ["Town", "Mafia", "Executioner", "Jester", "Survivor", "Pirate", "Arsonist", "Chaos", "Draw"]:
        print(f"{k}: {probs.get(k, 0.0):.3f}")


if __name__ == "__main__":
    main()
