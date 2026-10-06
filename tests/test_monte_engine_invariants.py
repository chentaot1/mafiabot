"""Monte Carlo full-game path uses engine nights with sim-aligned invariant checks."""

from __future__ import annotations

import random

from scripts.monte_carlo import config as mc_config
from scripts.monte_carlo.simulate import simulate_once


def test_monte_carlo_simulate_once_with_engine_invariants() -> None:
    mc_config.REALISTIC_NIGHT_ACTIONS = True
    mc_config.ENGINE_NIGHT_INVARIANTS = True
    roles = [
        "Chaos",
        "Pirate",
        "Mobster",
        "Escort",
        "Scary Grandma",
        "Transporter",
        "Bodyguard",
    ]
    random.seed(42)
    out = simulate_once(roles, max_days=3, collect_stats=False, trace=False)
    assert isinstance(out, dict)
