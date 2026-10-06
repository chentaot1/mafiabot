"""
Monte Carlo balance simulator for MafiaSalem.

Implementation: scripts/monte_carlo/ (engine-backed nights via scripts/monte_carlo/bridge.py).
Aligned with scripts/sim_test.py: realistic nights (all roles act when able), engine invariants.

Run: python scripts/monte_carlo_sim.py
     (default: 2000 generator-weighted trials at 7p via game_roles.py, parallel workers)
     Clean output: --quiet --generator-trials 2000 --player-count 7 --seed 42
     Fixed default lobby (7p prod): --fixed-lobby --n 2000
     Custom lineup: --roles ... --generator-trials 0 --n 2000
     Single-process trials: --serial (or --workers 1)
     Parity lock first: --parity-nights --generator-trials 500
     Legacy sparse nights: --no-realistic-nights
     Per-action noise: MC_ACTION_JITTER in scripts/monte_carlo/config.py (default 4%)
     Avoid piping live to Select-String (buffers all output); use --quiet or tail after.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.monte_carlo.audit import audit_against_bot_config
from scripts.monte_carlo.cli import main
from scripts.monte_carlo.config import (
    ALL_ROLES,
    DEFAULT_LOBBY_ROLES,
    ENGINE_NIGHT_INVARIANTS,
    GATEKEEPER_BLOCKS_ONE,
    LOBBY_SKILL,
    REALISTIC_N1_VIG_SHOOT,
    REALISTIC_NIGHT_ACTIONS,
    ROLE_META,
    ROLE_OPTIMAL_DIFFICULTY,
    ROLE_TARGETING_DIFFICULTY,
    ROLE_USAGE_DIFFICULTY,
    ROLE_DAY_DIFFICULTY,
    USE_DIFFICULTY_LAYER,
    Action,
    _competence_for_role,
)
from scripts.monte_carlo.generator import (
    enumerate_role_sets,
    run_enumeration,
    run_generator_weighted_trials,
    run_monte_carlo,
    sample_generator_roles,
    sample_generator_roles_constraints,
)
from scripts.monte_carlo.simulate import simulate_once
from scripts.monte_carlo.state import Player

__all__ = [
    "ALL_ROLES",
    "DEFAULT_LOBBY_ROLES",
    "LOBBY_SKILL",
    "USE_DIFFICULTY_LAYER",
    "GATEKEEPER_BLOCKS_ONE",
    "REALISTIC_NIGHT_ACTIONS",
    "REALISTIC_N1_VIG_SHOOT",
    "ENGINE_NIGHT_INVARIANTS",
    "ROLE_OPTIMAL_DIFFICULTY",
    "_competence_for_role",
    "ROLE_META",
    "Player",
    "Action",
    "simulate_once",
    "run_monte_carlo",
    "run_generator_weighted_trials",
    "run_enumeration",
    "enumerate_role_sets",
    "sample_generator_roles",
    "sample_generator_roles_constraints",
    "audit_against_bot_config",
    "main",
]

if __name__ == "__main__":
    main()
