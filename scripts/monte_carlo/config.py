"""Role universe, competence model, and action types (synced with config.py)."""
from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Dict, List, Literal, Optional, Sequence, Set, Tuple, TypedDict

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as bot_config  # noqa: E402

CompetenceAxis = Literal["targeting", "usage", "day"]

TOWN: Set[str] = set(bot_config.TOWN_ROLES)
MAFIA: Set[str] = set(bot_config.ALL_MAFIA_ROLES)
NEUTRAL: Set[str] = {
    "Survivor",
    "Executioner",
    "Jester",
    "Witch",
    "Pirate",
    "Arsonist",
    "Chaos",
    "Serial Killer",
    *bot_config.NEUTRAL_BENIGN_ROLES,
}


def _bot_role_universe() -> Set[str]:
    return (
        set(bot_config.TOWN_ROLES)
        | set(bot_config.ALL_MAFIA_ROLES)
        | set(bot_config.NEUTRAL_BENIGN_ROLES)
        | {
            "Jester",
            "Executioner",
            "Witch",
            "Pirate",
            "Arsonist",
            "Chaos",
            "Serial Killer",
        }
    )


ALL_ROLES: Set[str] = _bot_role_universe()
INVESTIGATIVE = {"Sheriff", "Investigator", "Lookout", "Tracker", "Mole"}
PROTECTIVE = {"Doctor", "Bodyguard"}
ROLEBLOCK_IMMUNE: Set[str] = set(bot_config.ROLEBLOCK_IMMUNE_ROLES)
CONTROL_IMMUNE: Set[str] = set(bot_config.CONTROL_IMMUNE_ROLES)
DEPUTY_GUN_EVIL_NEUTRALS: Set[str] = set(bot_config.DEPUTY_GUN_EVIL_NEUTRALS)
PSYCHIC_ODD_EVIL_NEUTRALS: Set[str] = set(bot_config.PSYCHIC_ODD_EVIL_NEUTRALS)
SEER_HOSTILE_NEUTRAL_ROLES: Set[str] = set(bot_config.SEER_HOSTILE_NEUTRAL_ROLES)
SEER_NEUTRAL_KILLING_ROLES: Set[str] = set(bot_config.SEER_NEUTRAL_KILLING_ROLES)
SEER_FRIENDLY_EXTRA: Set[str] = set(bot_config.SEER_FRIENDLY_EXTRA_ROLES)

# --- Competence model (average pub @ LOBBY_SKILL=0.5) ---
#
# Three axes per role:
#   targeting — who to act on (heal, kill, RB, transport pair, investigate, …)
#   usage     — when/whether to use a limited ability (vest, alert, ignite, duel, reveal)
#   day       — tribunal votes, Mayor reveal, Deputy timing, haunt picks
#
# P(optimal) = clamp(
#     skill * (1 - OPTIMAL_DIFFICULTY_WEIGHT * difficulty[axis]),
#     MIN_ROLE_COMPETENCE, 1.0,
# )
#   skill = AVERAGE_PLAYER_COMPETENCE + (LOBBY_SKILL - 0.5) * LOBBY_SKILL_SPREAD

AVERAGE_PLAYER_COMPETENCE: float = 0.52
LOBBY_SKILL_SPREAD: float = 0.36
OPTIMAL_DIFFICULTY_WEIGHT: float = 0.58
MIN_ROLE_COMPETENCE: float = 0.12

# Targeting difficulty (0 easy … 1 hard).
ROLE_TARGETING_DIFFICULTY: Dict[str, float] = {
    "Psychic": 0.10,
    "Scary Grandma": 0.32,
    "Sheriff": 0.48,
    "Investigator": 0.52,
    "Lookout": 0.62,
    "Tracker": 0.58,
    "Seer": 0.62,
    "Doctor": 0.65,
    "Bodyguard": 0.48,
    "Vigilante": 0.72,
    "Deputy": 0.50,
    "Mayor": 0.52,
    "Escort": 0.45,
    "Transporter": 0.88,
    "Retributionist": 0.78,
    "Mobster": 0.48,
    "Consort": 0.42,
    "Gatekeeper": 0.58,
    "Framer": 0.48,
    "Mole": 0.62,
    "Hypnotist": 0.58,
    "Tailor": 0.55,
    "Gravedigger": 0.52,
    "Survivor": 0.22,
    "Executioner": 0.30,
    "Jester": 0.45,
    "Guardian Angel": 0.40,
    "Witch": 0.78,
    "Pirate": 0.48,
    "Arsonist": 0.68,
    "Chaos": 0.82,
    "Serial Killer": 0.52,
}

# Usage difficulty — timing / whether to act (vest, alert, ignite, duel, corpse pick).
ROLE_USAGE_DIFFICULTY: Dict[str, float] = {
    "Psychic": 0.05,
    "Scary Grandma": 0.38,
    "Sheriff": 0.35,
    "Investigator": 0.35,
    "Lookout": 0.40,
    "Tracker": 0.40,
    "Seer": 0.45,
    "Doctor": 0.50,
    "Bodyguard": 0.45,
    "Vigilante": 0.55,
    "Deputy": 0.48,
    "Mayor": 0.45,
    "Escort": 0.40,
    "Transporter": 0.75,
    "Retributionist": 0.78,
    "Mobster": 0.40,
    "Consort": 0.38,
    "Gatekeeper": 0.52,
    "Framer": 0.42,
    "Mole": 0.48,
    "Hypnotist": 0.50,
    "Tailor": 0.48,
    "Gravedigger": 0.50,
    "Survivor": 0.28,
    "Executioner": 0.25,
    "Jester": 0.40,
    "Guardian Angel": 0.35,
    "Witch": 0.72,
    "Pirate": 0.52,
    "Arsonist": 0.75,
    "Chaos": 0.80,
    "Serial Killer": 0.48,
}

# Day difficulty — reads, lynch votes, reveal / revolver / haunt.
ROLE_DAY_DIFFICULTY: Dict[str, float] = {
    "Psychic": 0.10,
    "Scary Grandma": 0.35,
    "Sheriff": 0.45,
    "Investigator": 0.48,
    "Lookout": 0.55,
    "Tracker": 0.55,
    "Seer": 0.58,
    "Doctor": 0.55,
    "Bodyguard": 0.45,
    "Vigilante": 0.65,
    "Deputy": 0.50,
    "Mayor": 0.48,
    "Escort": 0.42,
    "Transporter": 0.70,
    "Retributionist": 0.72,
    "Mobster": 0.38,
    "Consort": 0.40,
    "Gatekeeper": 0.52,
    "Framer": 0.45,
    "Mole": 0.55,
    "Hypnotist": 0.50,
    "Tailor": 0.50,
    "Gravedigger": 0.48,
    "Survivor": 0.30,
    "Executioner": 0.30,
    "Jester": 0.42,
    "Guardian Angel": 0.38,
    "Witch": 0.70,
    "Pirate": 0.45,
    "Arsonist": 0.68,
    "Chaos": 0.75,
    "Serial Killer": 0.55,
}

# Backward-compatible alias (targeting axis).
ROLE_OPTIMAL_DIFFICULTY: Dict[str, float] = ROLE_TARGETING_DIFFICULTY

ROLE_META: Dict[str, str] = {
    "Doctor": "night: heal (+ self-heal limit)",
    "Sheriff": "night: investigate",
    "Investigator": "night: investigate (bucket)",
    "Lookout": "night: watch",
    "Tracker": "night: track",
    "Escort": "night: roleblock",
    "Vigilante": "night: shoot; guilt next day (realistic: always when ammo)",
    "Retributionist": "night: reanimate town corpse",
    "Bodyguard": "night: protect / vest",
    "Transporter": "night: transport (sim: every night)",
    "Mayor": "day: reveal; cannot be healed when revealed",
    "Scary Grandma": "night: alert",
    "Psychic": "night: passive visions (evidence)",
    "Deputy": "day: revolver (evil/neutral defense rules)",
    "Seer": "night: gaze Friends/Enemies",
    "Mobster": "night: faction kill",
    "Gravedigger": "night: hide corpse",
    "Consort": "night: roleblock",
    "Framer": "night: frame (N1–N2)",
    "Gatekeeper": "night: guard visitors",
    "Hypnotist": "night: fake message",
    "Mole": "night: investigate reveal",
    "Tailor": "night: fake death role",
    "Survivor": "night: vest; win if alive at end",
    "Executioner": "win: lynch target",
    "Jester": "win: lynched; haunt",
    "Guardian Angel": "night: ward bind; joint faction win",
    "Witch": "night: control each night (realistic); N1 shield",
    "Pirate": "night: plunder/duel; 2 kill-gated plunder wins",
    "Arsonist": "night: douse/ignite/clean",
    "Chaos": "night: chaos each night while uses remain (realistic)",
    "Serial Killer": "night: stab; solo win; RB counter",
}

# 0.0 = low-skill pub, 0.5 = average pub, 1.0 = high-skill stack (±0.10 competence shift).
LOBBY_SKILL: float = 0.5
USE_DIFFICULTY_LAYER: bool = True
GATEKEEPER_BLOCKS_ONE: bool = False

# When True, every role with a night command and remaining uses acts each night (pub-like).
# Target selection still uses the competence model; only skip/pass rates are removed.
REALISTIC_NIGHT_ACTIONS: bool = True
# N1 Vigilante shoots when REALISTIC_NIGHT_ACTIONS (sim N1 harness parity); False = legacy 10% N1.
REALISTIC_N1_VIG_SHOOT: bool = True
# Assert invariants + deaths/causes consistency after each engine night (aligns with sim_test).
ENGINE_NIGHT_INVARIANTS: bool = True

# When True, Sheriff uses true role only (no Arsonist/douse apparent overlay). Investigator unchanged.
# Default False matches live bot (Sheriff flags doused Arsonist). Set True only for ablation sims.
ARSONIST_SHERIFF_DETECTION_IMMUNE: bool = False

# Default fixed lobby for `monte_carlo_sim.py` when --roles / --generator-trials omitted.
DEFAULT_LOBBY_ROLES: List[str] = [
    "Psychic",
    "Sheriff",
    "Doctor",
    "Vigilante",
    "Survivor",
    "Jester",
    "Mobster",
]

# Day lynch: P(attempt trial) = min(cap, suspicion * per_point). 0 suspicion => no lynch.
LYNCH_PROB_PER_SUSPICION: float = 0.22
LYNCH_PROB_CAP: float = 0.95

# Per MC decision: ignore optimal heuristics and behave uniformly at random (~pub variance).
MC_ACTION_JITTER: float = 0.04


def mc_action_jitter() -> bool:
    return random.random() < MC_ACTION_JITTER


def mc_roll(probability: float) -> bool:
    """Bernoulli trial; jitter sometimes flips to ~50/50."""
    p = _clamp01(probability)
    if mc_action_jitter():
        return random.random() < 0.5
    return random.random() < p


def mc_pick_from(
    candidates: Sequence[int],
    *,
    preferred: Optional[int] = None,
    competence: float = 1.0,
) -> Optional[int]:
    """Pick a player id; jitter forces uniform choice over ``candidates``."""
    pool = [int(c) for c in candidates]
    if not pool:
        return None
    if mc_action_jitter():
        return random.choice(pool)
    if preferred is not None and preferred in pool:
        return preferred if random.random() < _clamp01(competence) else random.choice(pool)
    return random.choice(pool)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _lobby_execution_skill() -> float:
    """Table-wide execution skill before role difficulty is applied."""
    return AVERAGE_PLAYER_COMPETENCE + (LOBBY_SKILL - 0.5) * LOBBY_SKILL_SPREAD


def _difficulty_for_axis(role: str, axis: CompetenceAxis) -> float:
    if axis == "targeting":
        return float(ROLE_TARGETING_DIFFICULTY.get(role, 0.50))
    if axis == "usage":
        return float(ROLE_USAGE_DIFFICULTY.get(role, 0.50))
    return float(ROLE_DAY_DIFFICULTY.get(role, 0.50))


def _competence_for_axis(role: str, axis: CompetenceAxis = "targeting") -> float:
    """P(optimal) on the given axis: targeting, usage, or day."""
    if not USE_DIFFICULTY_LAYER:
        return 1.0
    difficulty = _difficulty_for_axis(role, axis)
    skill = _lobby_execution_skill()
    rate = skill * (1.0 - OPTIMAL_DIFFICULTY_WEIGHT * difficulty)
    return _clamp01(max(MIN_ROLE_COMPETENCE, rate))


def _competence_for_role(role: str) -> float:
    """Backward-compatible alias — targeting axis."""
    return _competence_for_axis(role, "targeting")


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
    if mc_action_jitter():
        return random_choice
    return good_choice if random.random() < competence else random_choice


ActionType = Literal[
    "kill",
    "shoot",
    "sk_kill",
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
    "guard",
    "control",
    "transport",
    "tailor",
    "hide",
    "investigate",
    "watch",
    "track",
    "gaze",
    "ward",
    "hypnotize",
    "ret_protect",
    "reanimate",
    "chaos",
]


class Action(TypedDict, total=False):
    type: ActionType
    actor: int
    target: int
    targets: List[int]
    role: str
    duel_won: bool
    duel_finished: bool
    fake_role: str
    msg_type: str
    corpse_role: str
    corpse_player_id: int


class SimStats(TypedDict, total=False):
    days: int
    lynches: int
    mislynches: int  # Town-role lynch only (diagnostic)
    mislynches_incl_neutrals: int  # legacy: any non-Mafia lynch
    lynches_neutral: int
    night_deaths: int
    doc_saves: int
    roleblocks: int
    gatekeeper_blocks: int
    controls: int
    controls_prevent_ignite: int
    ignites: int


# Arsonist stalemate breaker — canonical set in faction_win_logic.py
from faction_win_logic import ARSONIST_HARMLESS_NEUTRALS as _ARSO_HARMLESS  # noqa: E402

ARSO_HARMLESS_NEUTRALS: Set[str] = set(_ARSO_HARMLESS)
