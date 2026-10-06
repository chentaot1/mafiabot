"""
Fixed 7p N1 nights with expected deaths/causes (ToS parity locks).

Used by tests/test_n1_golden_nights.py, mc_preflight, and monte_carlo CLI (--parity-nights).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import engine.night as nm
from config import CHAOS_EFFECT_POOL
from scripts.sim_outcomes import assert_golden_outcome
from scripts.sim_test import make_game, reset_night_game_state, run_night_pipeline


@dataclass(frozen=True)
class GoldenNight:
    id: str
    description: str
    roles_by_seat: Dict[int, str]
    night_actions: Dict[int, Dict[str, Any]]
    role_states: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    chaos_effect: Optional[str] = None
    expected_deaths: frozenset[int] = frozenset()
    expected_causes: Dict[int, str] = field(default_factory=dict)
    expected_blocked: frozenset[int] = frozenset()


GOLDEN_NIGHTS: List[GoldenNight] = [
    GoldenNight(
        id="canonical_chaos_transport_sg",
        description="Canonical: Chaos transport + TP swap + SG alert; Mob redirected",
        roles_by_seat={
            1: "Chaos",
            2: "Survivor",
            3: "Mobster",
            4: "Vigilante",
            5: "Scary Grandma",
            6: "Transporter",
            7: "Bodyguard",
        },
        role_states={
            1: {"uses_remaining": 2},
            2: {"vests_remaining": 2},
            4: {"shots_remaining": 1},
            5: {"alerts_remaining": 2},
            7: {"uses_remaining": 1, "self_protects_remaining": 1},
        },
        night_actions={
            1: {"type": "chaos", "actor": 1, "targets": [2, 5]},
            2: {"type": "vest", "actor": 2, "target": 2},
            3: {"type": "kill", "actor": 3, "target": 5},
            5: {"type": "alert", "actor": 5, "target": 5},
            6: {"type": "transport", "actor": 6, "targets": [2, 5]},
            7: {"type": "protect", "actor": 7, "target": 6},
        },
        chaos_effect="transport",
        expected_deaths=frozenset({1, 6}),
        expected_causes={1: "scary_grandma", 6: "scary_grandma"},
        expected_blocked=frozenset(),
    ),
    GoldenNight(
        id="pirate_rb_tp_sg_alert",
        description="Pirate loses on TP; Escort RB Chaos; no swap; Mob+BG die at SG alert",
        roles_by_seat={
            1: "Chaos",
            2: "Pirate",
            3: "Mobster",
            4: "Escort",
            5: "Scary Grandma",
            6: "Transporter",
            7: "Bodyguard",
        },
        role_states={
            1: {"uses_remaining": 2},
            5: {"alerts_remaining": 2},
            7: {"uses_remaining": 1, "self_protects_remaining": 1},
        },
        night_actions={
            1: {"type": "chaos", "actor": 1, "targets": [2, 5]},
            2: {"type": "plunder", "actor": 2, "target": 6, "duel_won": False, "duel_finished": True},
            3: {"type": "kill", "actor": 3, "target": 5},
            4: {"type": "roleblock", "actor": 4, "target": 1},
            5: {"type": "alert", "actor": 5, "target": 5},
            6: {"type": "transport", "actor": 6, "targets": [2, 5]},
            7: {"type": "protect", "actor": 7, "target": 5},
        },
        chaos_effect="transport",
        expected_deaths=frozenset({3, 7}),
        expected_causes={3: "bodyguard", 7: "bodyguard_guard"},
        expected_blocked=frozenset({1, 6}),
    ),
    GoldenNight(
        id="pirate_win_escort_rb_bg",
        description="Pirate wins on TP; Escort RB BG; transport on; Chaos+Pirate+TP die",
        roles_by_seat={
            1: "Chaos",
            2: "Pirate",
            3: "Mobster",
            4: "Escort",
            5: "Scary Grandma",
            6: "Transporter",
            7: "Bodyguard",
        },
        role_states={
            1: {"uses_remaining": 2},
            5: {"alerts_remaining": 2},
            7: {"uses_remaining": 1, "self_protects_remaining": 1},
        },
        night_actions={
            1: {"type": "chaos", "actor": 1, "targets": [2, 5]},
            2: {"type": "plunder", "actor": 2, "target": 6, "duel_won": True, "duel_finished": True},
            3: {"type": "kill", "actor": 3, "target": 2},
            4: {"type": "roleblock", "actor": 4, "target": 7},
            5: {"type": "alert", "actor": 5, "target": 5},
            6: {"type": "transport", "actor": 6, "targets": [2, 5]},
        },
        chaos_effect="transport",
        # Under this submission, Pirate wins the duel and kills the Transporter; Chaos + Mobster die to SG alert.
        expected_deaths=frozenset({1, 3, 6}),
        expected_causes={1: "scary_grandma", 3: "scary_grandma", 6: "pirate_plunder"},
        expected_blocked=frozenset({6, 7}),
    ),
    GoldenNight(
        id="chaos_guard_pirate_lives",
        description="Chaos guard; Pirate wins; Escort RB BG; no transport deaths on alert stack",
        roles_by_seat={
            1: "Chaos",
            2: "Pirate",
            3: "Mobster",
            4: "Escort",
            5: "Scary Grandma",
            6: "Transporter",
            7: "Bodyguard",
        },
        role_states={
            1: {"uses_remaining": 2},
            5: {"alerts_remaining": 2},
            7: {"uses_remaining": 1, "self_protects_remaining": 1},
        },
        night_actions={
            1: {"type": "chaos", "actor": 1, "targets": [2, 5]},
            2: {"type": "plunder", "actor": 2, "target": 6, "duel_won": True, "duel_finished": True},
            3: {"type": "kill", "actor": 3, "target": 2},
            4: {"type": "roleblock", "actor": 4, "target": 7},
            5: {"type": "alert", "actor": 5, "target": 5},
        },
        chaos_effect="guard",
        # Under this submission, Mobster kill succeeds on Pirate; Pirate wins plunder and kills Transporter;
        # Chaos dies to SG alert.
        expected_deaths=frozenset({1, 2, 6}),
        expected_causes={1: "scary_grandma", 2: "mafia", 6: "pirate_plunder"},
        expected_blocked=frozenset({6, 7}),
    ),
]


class _ForcedChaosRandom:
    def __init__(self, effect: str, base_random_cls, seed: Optional[int] = None) -> None:
        self._effect = effect
        self._r = base_random_cls(seed)

    def choice(self, seq):
        if set(seq) == set(CHAOS_EFFECT_POOL):
            return self._effect
        return self._r.choice(seq)


async def run_golden_night(golden: GoldenNight) -> Dict[str, Any]:
    n = max(golden.roles_by_seat.keys())
    game, guild, members = make_game(seed=hash(golden.id) & 0x7FFFFFFF, n=n)
    game.day_number = 1
    game.player_slots = {i: i for i in range(1, n + 1)}
    reset_night_game_state(game, members=members, roles_by_seat=dict(golden.roles_by_seat))
    for seat, st in golden.role_states.items():
        game.role_states.setdefault(int(seat), {}).update(dict(st))

    actions = {int(k): dict(v) for k, v in golden.night_actions.items()}
    for aid, act in actions.items():
        act.setdefault("actor", aid)
        act.setdefault("role", golden.roles_by_seat[aid])
    game.night_actions = actions

    old_rng = nm.random.Random
    base_random_cls = nm.random.Random
    if golden.chaos_effect:
        nm.random.Random = lambda seed=None: _ForcedChaosRandom(golden.chaos_effect, base_random_cls, seed)  # type: ignore[assignment,misc]
    try:
        out = await run_night_pipeline(game, guild)
    finally:
        nm.random.Random = old_rng

    assert_golden_outcome(
        game,
        out,
        expected_deaths=golden.expected_deaths,
        expected_causes=golden.expected_causes,
        expected_blocked=golden.expected_blocked,
        label=golden.id,
    )
    return out


async def run_all_golden_nights() -> int:
    failures = 0
    for golden in GOLDEN_NIGHTS:
        try:
            await run_golden_night(golden)
            print(f"  OK {golden.id}: {golden.description}", flush=True)
        except Exception as e:
            failures += 1
            print(f"  FAIL {golden.id}: {e}", flush=True)
    return failures
