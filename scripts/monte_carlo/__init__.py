"""Monte Carlo balance simulator (engine-backed night resolution)."""
from scripts.monte_carlo.audit import audit_against_bot_config
from scripts.monte_carlo.config import (
    ALL_ROLES,
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
    _competence_for_axis,
    _competence_for_role,
)
from scripts.monte_carlo.generator import (
    enumerate_role_sets,
    run_enumeration,
    run_generator_weighted_trials,
    run_generator_weighted_trials_parallel,
    run_monte_carlo,
    sample_generator_roles,
    sample_generator_roles_constraints,
)
from scripts.monte_carlo.runtime import default_trial_workers
from scripts.monte_carlo.simulate import simulate_once

__all__ = [
    "ALL_ROLES",
    "LOBBY_SKILL",
    "USE_DIFFICULTY_LAYER",
    "GATEKEEPER_BLOCKS_ONE",
    "REALISTIC_NIGHT_ACTIONS",
    "REALISTIC_N1_VIG_SHOOT",
    "ENGINE_NIGHT_INVARIANTS",
    "ROLE_OPTIMAL_DIFFICULTY",
    "ROLE_TARGETING_DIFFICULTY",
    "ROLE_USAGE_DIFFICULTY",
    "ROLE_DAY_DIFFICULTY",
    "_competence_for_axis",
    "_competence_for_role",
    "ROLE_META",
    "simulate_once",
    "run_monte_carlo",
    "run_generator_weighted_trials",
    "run_generator_weighted_trials_parallel",
    "default_trial_workers",
    "run_enumeration",
    "enumerate_role_sets",
    "sample_generator_roles",
    "sample_generator_roles_constraints",
    "audit_against_bot_config",
]
