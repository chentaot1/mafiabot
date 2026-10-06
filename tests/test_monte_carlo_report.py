"""Monte Carlo summary: conditional personal-win rates."""

from __future__ import annotations

from scripts.monte_carlo.report import (
    accumulate_personal_win_stats,
    finalize_personal_win_rates,
    init_personal_win_counters,
)


def test_conditional_survivor_win_rate() -> None:
    presence, wins = init_personal_win_counters()
    roles_surv = ["Survivor", "Mobster", "Doctor", "Sheriff", "Vigilante", "Jester", "Escort"]
    roles_no_surv = ["Mobster", "Doctor", "Sheriff", "Vigilante", "Jester", "Escort", "Pirate"]
    for _ in range(4):
        accumulate_personal_win_stats(roles_surv, {"Survivor": True}, presence, wins)
    for _ in range(6):
        accumulate_personal_win_stats(roles_surv, {"Survivor": False}, presence, wins)
    for _ in range(10):
        accumulate_personal_win_stats(roles_no_surv, {}, presence, wins)

    in_rate, cond = finalize_personal_win_rates(trials=20, presence=presence, wins_when_present=wins)
    assert in_rate["Survivor"] == 0.5
    assert abs(cond["Survivor"] - 4 / 10) < 1e-9
