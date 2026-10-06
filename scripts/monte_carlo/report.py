"""Human-readable Monte Carlo trial summaries (main vs personal-win outcomes)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, MutableMapping, Optional, Set, Tuple

from scripts.monte_carlo.config import ALL_ROLES, MAFIA, NEUTRAL, TOWN

# All neutrals in the 7p+ generator pool (game_roles._bracket_neutral_display).
PERSONAL_WIN_OUTCOMES: List[str] = [
    "Jester",
    "Executioner",
    "Survivor",
    "Guardian Angel",
    "Witch",
    "Pirate",
    "Arsonist",
    "Chaos",
    "Serial Killer",
]

MAIN_FACTION_OUTCOMES: List[str] = ["Town", "Mafia", "Draw"]

# Killing neutrals that can be the primary endgame winner (unconditional pooled rate).
SOLO_NEUTRAL_MAIN_OUTCOMES: List[str] = ["Arsonist", "Serial Killer"]

# Backward-compatible alias for batch counters.
MAIN_OUTCOMES: List[str] = MAIN_FACTION_OUTCOMES

_PERSONAL_SHORT = {
    "Executioner": "Exe",
    "Jester": "Jes",
    "Survivor": "Surv",
    "Guardian Angel": "GA",
    "Pirate": "Pir",
    "Arsonist": "Arso",
    "Chaos": "Chaos",
    "Serial Killer": "SK",
    "Witch": "Witch",
}


@dataclass(frozen=True)
class GeneratorTrialResults:
    """Aggregated generator-weighted trial stats."""

    unconditional: Dict[str, float]
    presence_rate: Dict[str, float]
    conditional_win: Dict[str, float]
    role_presence_rate: Dict[str, float] = field(default_factory=dict)
    role_conditional_win: Dict[str, float] = field(default_factory=dict)
    role_pooled_win: Dict[str, float] = field(default_factory=dict)
    role_win_median: Dict[str, float] = field(default_factory=dict)
    role_win_range: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    outcome_pooled_median: Dict[str, float] = field(default_factory=dict)
    outcome_pooled_range: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    outcome_cond_median: Dict[str, float] = field(default_factory=dict)
    outcome_cond_range: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    neutral_any_win_pooled: float = 0.0
    neutral_any_win_when_present: float = 0.0

    def get(self, key: str, default: float = 0.0) -> float:
        return float(self.unconditional.get(key, default))


def init_personal_win_counters() -> tuple[Dict[str, int], Dict[str, int]]:
    presence: Dict[str, int] = {r: 0 for r in PERSONAL_WIN_OUTCOMES}
    wins: Dict[str, int] = {r: 0 for r in PERSONAL_WIN_OUTCOMES}
    return presence, wins


def accumulate_personal_win_stats(
    roles: List[str],
    res: Mapping[str, bool],
    presence: MutableMapping[str, int],
    wins_when_present: MutableMapping[str, int],
) -> None:
    role_set = set(roles)
    for outcome in PERSONAL_WIN_OUTCOMES:
        if outcome not in role_set:
            continue
        presence[outcome] = int(presence.get(outcome, 0)) + 1
        if res.get(outcome):
            wins_when_present[outcome] = int(wins_when_present.get(outcome, 0)) + 1


def finalize_personal_win_rates(
    *,
    trials: int,
    presence: Mapping[str, int],
    wins_when_present: Mapping[str, int],
) -> tuple[Dict[str, float], Dict[str, float]]:
    if trials <= 0:
        return {}, {}
    presence_rate = {r: int(presence.get(r, 0)) / trials for r in PERSONAL_WIN_OUTCOMES}
    conditional: Dict[str, float] = {}
    for r in PERSONAL_WIN_OUTCOMES:
        n = int(presence.get(r, 0))
        conditional[r] = (int(wins_when_present.get(r, 0)) / n) if n else 0.0
    return presence_rate, conditional


def _role_win(res: Mapping[str, bool], role: str) -> bool:
    if role in MAFIA:
        return bool(res.get("Mafia"))
    if role in TOWN:
        return bool(res.get("Town"))
    return bool(res.get(role))


def init_role_win_counters() -> tuple[Dict[str, int], Dict[str, int], Dict[str, int], Dict[str, int]]:
    """Per-role presence/wins plus neutral-lobby aggregate counters."""
    return {}, {}, {}, {}


def accumulate_role_win_stats(
    roles: List[str],
    res: Mapping[str, bool],
    role_presence: MutableMapping[str, int],
    role_wins: MutableMapping[str, int],
    *,
    trials_with_neutral: MutableMapping[str, int],
    trials_any_neutral_win: MutableMapping[str, int],
) -> None:
    has_neutral = False
    any_neutral_win = False
    for role in set(roles):
        role_presence[role] = int(role_presence.get(role, 0)) + 1
        won = _role_win(res, role)
        if won:
            role_wins[role] = int(role_wins.get(role, 0)) + 1
        if role in NEUTRAL:
            has_neutral = True
            if res.get(role):
                any_neutral_win = True
    if has_neutral:
        trials_with_neutral["n"] = int(trials_with_neutral.get("n", 0)) + 1
    if any_neutral_win:
        trials_any_neutral_win["n"] = int(trials_any_neutral_win.get("n", 0)) + 1


def finalize_role_win_rates(
    *,
    trials: int,
    role_presence: Mapping[str, int],
    role_wins: Mapping[str, int],
    trials_with_neutral: int,
    trials_any_neutral_win: int,
) -> tuple[Dict[str, float], Dict[str, float], float, float]:
    if trials <= 0:
        return {}, {}, 0.0, 0.0
    presence_rate = {r: int(role_presence.get(r, 0)) / trials for r in role_presence}
    conditional: Dict[str, float] = {}
    for r, n in role_presence.items():
        conditional[r] = (int(role_wins.get(r, 0)) / n) if n else 0.0
    neutral_pooled = trials_any_neutral_win / trials
    neutral_when = (
        trials_any_neutral_win / trials_with_neutral if trials_with_neutral else 0.0
    )
    return presence_rate, conditional, neutral_pooled, neutral_when


def _role_faction_label(role: str) -> str:
    if role in MAFIA:
        return "Maf"
    if role in TOWN:
        return "Town"
    return "Neu"


def _sorted_roles_for_report(roles: Set[str]) -> List[str]:
    order = {"Town": 0, "Maf": 1, "Neu": 2}
    return sorted(roles, key=lambda r: (order[_role_faction_label(r)], r))


def all_roles_report_order() -> List[str]:
    """Every sim role: Town, then Mafia, then Neutral (alphabetical within faction)."""
    town = sorted(r for r in ALL_ROLES if r in TOWN)
    mafia = sorted(r for r in ALL_ROLES if r in MAFIA)
    neutral = sorted(r for r in ALL_ROLES if r in NEUTRAL)
    return town + mafia + neutral


def merge_chunk_role_win_spreads(chunks: List[Mapping[str, object]]) -> Tuple[Dict[str, float], Dict[str, Tuple[float, float]]]:
    """
    Per-role median and (min, max) of win|in across parallel chunks (chunk had >=1 presence).
    """
    by_role: Dict[str, List[float]] = {}
    for chunk in chunks:
        pres = chunk.get("role_presence") or {}
        wins = chunk.get("role_wins") or {}
        if not isinstance(pres, dict) or not isinstance(wins, dict):
            continue
        for role, n_raw in pres.items():
            n = int(n_raw)
            if n <= 0:
                continue
            rate = int(wins.get(role, 0)) / n
            by_role.setdefault(str(role), []).append(rate)
    medians: Dict[str, float] = {}
    ranges: Dict[str, Tuple[float, float]] = {}
    for role, rates in by_role.items():
        if not rates:
            continue
        medians[role] = float(statistics.median(rates))
        ranges[role] = (min(rates), max(rates))
    return medians, ranges


def merge_chunk_outcome_spreads(
    chunks: List[Mapping[str, object]],
) -> Tuple[
    Dict[str, float],
    Dict[str, Tuple[float, float]],
    Dict[str, float],
    Dict[str, Tuple[float, float]],
]:
    """
    Median/min-max across parallel chunks for pooled faction rates and neutral win|in.
    """
    pooled_by: Dict[str, List[float]] = {k: [] for k in MAIN_FACTION_OUTCOMES}
    for role in PERSONAL_WIN_OUTCOMES:
        pooled_by[role] = []
    cond_by: Dict[str, List[float]] = {r: [] for r in PERSONAL_WIN_OUTCOMES}

    for chunk in chunks:
        trials = int(chunk.get("trials", 0))
        if trials <= 0:
            continue
        counts = chunk.get("counts") or {}
        if not isinstance(counts, dict):
            continue
        for outcome in MAIN_FACTION_OUTCOMES:
            pooled_by[outcome].append(int(counts.get(outcome, 0)) / trials)
        for role in PERSONAL_WIN_OUTCOMES:
            pooled_by[role].append(int(counts.get(role, 0)) / trials)
        pres = chunk.get("presence_counts") or {}
        wins = chunk.get("wins_when_present") or {}
        if isinstance(pres, dict) and isinstance(wins, dict):
            for role in PERSONAL_WIN_OUTCOMES:
                n = int(pres.get(role, 0))
                if n <= 0:
                    continue
                cond_by[role].append(int(wins.get(role, 0)) / n)

    def _finalize(by: Dict[str, List[float]]) -> Tuple[Dict[str, float], Dict[str, Tuple[float, float]]]:
        medians: Dict[str, float] = {}
        ranges: Dict[str, Tuple[float, float]] = {}
        for key, rates in by.items():
            if not rates:
                continue
            medians[key] = float(statistics.median(rates))
            ranges[key] = (min(rates), max(rates))
        return medians, ranges

    pooled_med, pooled_rng = _finalize(pooled_by)
    cond_med, cond_rng = _finalize(cond_by)
    return pooled_med, pooled_rng, cond_med, cond_rng


def _fmt_spread(
    med: Optional[float],
    rng: Optional[Tuple[float, float]],
) -> str:
    if med is None or rng is None:
        return ""
    return f"  median={med:.3f}  range={rng[0]:.3f}-{rng[1]:.3f}"


def print_full_trial_summary(
    *,
    header: str,
    trials: int,
    results: GeneratorTrialResults,
    avg_lynches: Optional[float] = None,
    avg_days: Optional[float] = None,
    lynch_prob_per: Optional[float] = None,
    lynch_prob_cap: Optional[float] = None,
    extra_line: str = "",
) -> None:
    """Faction + neutral-personal summary, then every role with in / win|in / pooled / median / range."""
    print_trial_summary(
        header=header,
        trials=trials,
        results=results,
        avg_lynches=avg_lynches,
        avg_days=avg_days,
        lynch_prob_per=lynch_prob_per,
        lynch_prob_cap=lynch_prob_cap,
        extra_line=extra_line,
    )

    u = results.unconditional
    print(
        "Per-role WR (win|in = faction or personal win when role is in lineup; "
        "pooled = wins / all trials; median/range = across parallel worker chunks):",
        flush=True,
    )
    print(
        f"  {'Role':<20} {'Fac':<5} {'in':>7} {'win|in':>8} {'pooled':>8} {'median':>8} {'range':>17}",
        flush=True,
    )
    for role in all_roles_report_order():
        fac = _role_faction_label(role)
        in_pct = results.role_presence_rate.get(role, 0.0)
        cond = results.role_conditional_win.get(role, 0.0)
        pooled = results.role_pooled_win.get(role, 0.0)
        med = results.role_win_median.get(role)
        rng = results.role_win_range.get(role)
        if in_pct <= 0:
            win_in_s = "   n/a"
            pooled_s = "   n/a"
            med_s = "   n/a"
            rng_s = "n/a"
        else:
            win_in_s = f"{cond:7.1%}"
            pooled_s = f"{pooled:7.3f}"
            med_s = f"{med:7.1%}" if med is not None else "   n/a"
            rng_s = f"{rng[0]:6.1%}-{rng[1]:6.1%}" if rng else "n/a"
        print(
            f"  {role:<20} {fac:<5} {in_pct:6.1%} {win_in_s:>8} {pooled_s:>8} {med_s:>8} {rng_s:>17}",
            flush=True,
        )


def print_trial_summary(
    *,
    header: str,
    trials: int,
    results: GeneratorTrialResults,
    avg_lynches: Optional[float] = None,
    avg_days: Optional[float] = None,
    lynch_prob_per: Optional[float] = None,
    lynch_prob_cap: Optional[float] = None,
    extra_line: str = "",
) -> None:
    print(header, flush=True)
    meta_parts: List[str] = []
    if lynch_prob_per is not None and lynch_prob_cap is not None:
        meta_parts.append(f"lynch_prob_per={lynch_prob_per:.2f} cap={lynch_prob_cap:.2f}")
    if avg_lynches is not None:
        meta_parts.append(f"avg_lynches={avg_lynches:.2f}")
    if avg_days is not None:
        meta_parts.append(f"avg_days={avg_days:.2f}")
    if meta_parts:
        print(" ".join(meta_parts), flush=True)
    if extra_line:
        print(extra_line, flush=True)

    u = results.unconditional
    print("Main outcomes (faction; pooled = wins / all trials; median/range = across worker chunks):", flush=True)
    for outcome in MAIN_FACTION_OUTCOMES:
        pooled = u.get(outcome, 0.0)
        spread = _fmt_spread(
            results.outcome_pooled_median.get(outcome),
            results.outcome_pooled_range.get(outcome),
        )
        print(f"  {outcome:5s} pooled={pooled:.3f}{spread}", flush=True)

    solo_parts: List[str] = []
    for role in SOLO_NEUTRAL_MAIN_OUTCOMES:
        rate = u.get(role, 0.0)
        short = _PERSONAL_SHORT.get(role, role[:4])
        med = results.outcome_pooled_median.get(role)
        rng = results.outcome_pooled_range.get(role)
        extra = _fmt_spread(med, rng) if med is not None and rng else ""
        solo_parts.append(f"{short}={rate:.3f}{extra}")
    if solo_parts:
        print(
            "Neutral solo main wins (Arso/SK endgame; can overlap personal wins): "
            + " ".join(solo_parts),
            flush=True,
        )

    print(
        "Neutral personal wins (win|in when role in lineup; pooled = wins / all trials; "
        "median/range on pooled and win|in = across worker chunks):",
        flush=True,
    )
    print(
        f"  {'Role':<6} {'in':>7} {'win|in':>8} {'pooled':>8} {'pooled_med':>10} {'pooled_rng':>17} "
        f"{'win|in_med':>10} {'win|in_rng':>17}",
        flush=True,
    )
    for role in PERSONAL_WIN_OUTCOMES:
        in_pct = results.presence_rate.get(role, 0.0)
        cond = results.conditional_win.get(role, 0.0)
        pooled = u.get(role, 0.0)
        short = _PERSONAL_SHORT.get(role, role[:4])
        p_med = results.outcome_pooled_median.get(role)
        p_rng = results.outcome_pooled_range.get(role)
        c_med = results.outcome_cond_median.get(role)
        c_rng = results.outcome_cond_range.get(role)
        p_med_s = f"{p_med:9.3f}" if p_med is not None else "      n/a"
        p_rng_s = f"{p_rng[0]:6.3f}-{p_rng[1]:6.3f}" if p_rng else "n/a"
        if in_pct <= 0:
            c_med_s = "      n/a"
            c_rng_s = "n/a"
        else:
            c_med_s = f"{c_med:9.3f}" if c_med is not None else "      n/a"
            c_rng_s = f"{c_rng[0]:6.3f}-{c_rng[1]:6.3f}" if c_rng else "n/a"
        print(
            f"  {short:<6} {in_pct:6.1%} {cond:7.1%} {pooled:8.3f} {p_med_s:>10} {p_rng_s:>17} "
            f"{c_med_s:>10} {c_rng_s:>17}",
            flush=True,
        )

    if results.neutral_any_win_pooled or results.neutral_any_win_when_present:
        print(
            "Neutral aggregate: "
            f"any_neutral_win pooled={results.neutral_any_win_pooled:.3f} "
            f"win|neutral_in_lobby={results.neutral_any_win_when_present:.3f}",
            flush=True,
        )
