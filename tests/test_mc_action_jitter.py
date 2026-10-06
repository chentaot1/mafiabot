"""Monte Carlo per-action jitter."""
from __future__ import annotations

from scripts.monte_carlo.config import MC_ACTION_JITTER, _pick_with_competence, mc_roll


def test_pick_with_competence_jitter_prefers_random_branch(monkeypatch) -> None:
    calls = {"n": 0}

    def _always_jitter() -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr("scripts.monte_carlo.config.mc_action_jitter", _always_jitter)
    pick = _pick_with_competence(competence=1.0, good_choice=1, random_choice=9)
    assert pick == 9
    assert calls["n"] >= 1


def test_mc_roll_jitter_is_configured() -> None:
    assert 0.0 < MC_ACTION_JITTER < 0.15
