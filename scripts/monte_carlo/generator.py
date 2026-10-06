"""Role generation, batch trials, and enumeration."""
from __future__ import annotations

import csv
import hashlib
import itertools
import os
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, cast

import game_roles as bot_game_roles

from scripts.monte_carlo.config import (
    ALL_ROLES,
    INVESTIGATIVE,
    PROTECTIVE,
    ROLE_META,
    ROLE_OPTIMAL_DIFFICULTY,
    TOWN,
    SimStats,
    _bot_role_universe,
)
from scripts.monte_carlo.diagnostics_report import (
    ConditionalStats,
    accumulate_conditionals,
    merge_conditional_stats,
    print_avg_diagnostics,
    print_conditional_report,
)
from scripts.monte_carlo.report import (
    GeneratorTrialResults,
    accumulate_personal_win_stats,
    accumulate_role_win_stats,
    finalize_personal_win_rates,
    finalize_role_win_rates,
    init_personal_win_counters,
    init_role_win_counters,
    merge_chunk_outcome_spreads,
    merge_chunk_role_win_spreads,
)
from scripts.monte_carlo.simulate import simulate_once

_OUTCOME_COUNT_KEYS: Tuple[str, ...] = (
    "Town",
    "Mafia",
    "Draw",
    "Executioner",
    "Jester",
    "Survivor",
    "Guardian Angel",
    "Pirate",
    "Arsonist",
    "Chaos",
    "Serial Killer",
    "Witch",
)


def _empty_outcome_counts() -> Dict[str, int]:
    return {k: 0 for k in _OUTCOME_COUNT_KEYS}


def _generator_trial_kwargs(
    *,
    max_investigative: Optional[int],
    exact_investigative: Optional[int],
    require_investigative: bool,
    require_doctor: bool,
    require_protective: bool,
    include_roles: Optional[Set[str]],
    exclude_roles: Optional[Set[str]],
    mafia_override: Optional[int],
    neutral_override: Optional[int],
    role_set: str,
) -> Dict[str, Any]:
    return {
        "max_investigative": max_investigative,
        "exact_investigative": exact_investigative,
        "require_investigative": require_investigative,
        "require_doctor": require_doctor,
        "require_protective": require_protective,
        "include_roles": include_roles,
        "exclude_roles": exclude_roles,
        "mafia_override": mafia_override,
        "neutral_override": neutral_override,
        "role_set": role_set,
    }


def run_generator_weighted_trials_chunk(
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
    role_set: str = "default",
    collect_stats: bool = True,
) -> Dict[str, Any]:
    """Run a trial sub-batch; return mergeable counters (for parallel workers)."""
    batch_rng = random.Random(seed)
    counts = _empty_outcome_counts()
    presence_counts, wins_when_present = init_personal_win_counters()
    role_presence, role_wins, trials_with_neutral, trials_any_neutral_win = init_role_win_counters()
    gen_kw = _generator_trial_kwargs(
        max_investigative=max_investigative,
        exact_investigative=exact_investigative,
        require_investigative=require_investigative,
        require_doctor=require_doctor,
        require_protective=require_protective,
        include_roles=include_roles,
        exclude_roles=exclude_roles,
        mafia_override=mafia_override,
        neutral_override=neutral_override,
        role_set=role_set,
    )
    total_days = 0
    total_lynches = 0
    for _ in range(trials):
        roles = sample_generator_roles_constraints(player_count, rng=batch_rng, **gen_kw)
        rr = roles[:]
        batch_rng.shuffle(rr)
        if collect_stats:
            res, st = cast(Tuple[Dict[str, bool], SimStats], simulate_once(rr, collect_stats=True))
            total_days += int(st.get("days", 0))
            total_lynches += int(st.get("lynches", 0))
        else:
            res = cast(Dict[str, bool], simulate_once(rr))
        accumulate_personal_win_stats(rr, res, presence_counts, wins_when_present)
        accumulate_role_win_stats(
            rr,
            res,
            role_presence,
            role_wins,
            trials_with_neutral=trials_with_neutral,
            trials_any_neutral_win=trials_any_neutral_win,
        )
        for k, v in res.items():
            if v and k in counts:
                counts[k] += 1
    return {
        "trials": trials,
        "counts": counts,
        "presence_counts": dict(presence_counts),
        "wins_when_present": dict(wins_when_present),
        "role_presence": dict(role_presence),
        "role_wins": dict(role_wins),
        "trials_with_neutral": int(trials_with_neutral.get("n", 0)),
        "trials_any_neutral_win": int(trials_any_neutral_win.get("n", 0)),
        "total_days": total_days,
        "total_lynches": total_lynches,
    }


def merge_generator_trial_chunks(chunks: List[Dict[str, Any]]) -> GeneratorTrialResults:
    """Merge parallel worker chunks into one GeneratorTrialResults."""
    total_trials = sum(int(c.get("trials", 0)) for c in chunks)
    if total_trials <= 0:
        return GeneratorTrialResults(unconditional={}, presence_rate={}, conditional_win={})
    counts = _empty_outcome_counts()
    presence_counts, wins_when_present = init_personal_win_counters()
    role_presence: Dict[str, int] = {}
    role_wins: Dict[str, int] = {}
    trials_with_neutral = 0
    trials_any_neutral_win = 0
    total_days = 0
    total_lynches = 0
    for chunk in chunks:
        for k, v in chunk["counts"].items():
            counts[k] = counts.get(k, 0) + int(v)
        for outcome in presence_counts:
            presence_counts[outcome] += int(chunk["presence_counts"].get(outcome, 0))
            wins_when_present[outcome] += int(chunk["wins_when_present"].get(outcome, 0))
        for role, n in (chunk.get("role_presence") or {}).items():
            role_presence[role] = role_presence.get(role, 0) + int(n)
        for role, w in (chunk.get("role_wins") or {}).items():
            role_wins[role] = role_wins.get(role, 0) + int(w)
        trials_with_neutral += int(chunk.get("trials_with_neutral", 0))
        trials_any_neutral_win += int(chunk.get("trials_any_neutral_win", 0))
        total_days += int(chunk.get("total_days", 0))
        total_lynches += int(chunk.get("total_lynches", 0))
    probs = {k: v / total_trials for k, v in counts.items()}
    probs["avg_days"] = total_days / total_trials
    probs["avg_lynches"] = total_lynches / total_trials
    presence_rate, conditional_win = finalize_personal_win_rates(
        trials=total_trials,
        presence=presence_counts,
        wins_when_present=wins_when_present,
    )
    role_presence_rate, role_conditional_win, neutral_pooled, neutral_when = finalize_role_win_rates(
        trials=total_trials,
        role_presence=role_presence,
        role_wins=role_wins,
        trials_with_neutral=trials_with_neutral,
        trials_any_neutral_win=trials_any_neutral_win,
    )
    role_pooled_win = {r: int(role_wins.get(r, 0)) / total_trials for r in role_presence}
    role_win_median, role_win_range = merge_chunk_role_win_spreads(chunks)
    (
        outcome_pooled_median,
        outcome_pooled_range,
        outcome_cond_median,
        outcome_cond_range,
    ) = merge_chunk_outcome_spreads(chunks)
    return GeneratorTrialResults(
        unconditional=probs,
        presence_rate=presence_rate,
        conditional_win=conditional_win,
        role_presence_rate=role_presence_rate,
        role_conditional_win=role_conditional_win,
        role_pooled_win=role_pooled_win,
        role_win_median=role_win_median,
        role_win_range=role_win_range,
        outcome_pooled_median=outcome_pooled_median,
        outcome_pooled_range=outcome_pooled_range,
        outcome_cond_median=outcome_cond_median,
        outcome_cond_range=outcome_cond_range,
        neutral_any_win_pooled=neutral_pooled,
        neutral_any_win_when_present=neutral_when,
    )


def _parallel_trial_worker(work: Dict[str, Any]) -> Dict[str, Any]:
    """ProcessPool entrypoint — each worker owns its asyncio loop and seed stream."""
    import sys

    root = str(work["root"])
    if root not in sys.path:
        sys.path.insert(0, root)
    import game as game_module

    from scripts.monte_carlo import config as mc_config
    from scripts.monte_carlo.runtime import close_async_loop, configure_quiet_logging

    configure_quiet_logging()
    if work.get("no_engine_invariants", False):
        mc_config.ENGINE_NIGHT_INVARIANTS = False

    async def _noop_persist_flush(_self: object) -> None:
        return

    game_module.Game.persist_flush = _noop_persist_flush  # type: ignore[method-assign]
    try:
        return run_generator_weighted_trials_chunk(
            int(work["player_count"]),
            trials=int(work["trials"]),
            seed=int(work["seed"]),
            collect_stats=bool(work.get("collect_stats", True)),
            **_generator_trial_kwargs(
                max_investigative=work.get("max_investigative"),
                exact_investigative=work.get("exact_investigative"),
                require_investigative=bool(work.get("require_investigative", False)),
                require_doctor=bool(work.get("require_doctor", False)),
                require_protective=bool(work.get("require_protective", False)),
                include_roles=work.get("include_roles"),
                exclude_roles=work.get("exclude_roles"),
                mafia_override=work.get("mafia_override"),
                neutral_override=work.get("neutral_override"),
                role_set=str(work.get("role_set", "default")),
            ),
        )
    finally:
        close_async_loop()


def run_generator_weighted_trials_parallel(
    player_count: int,
    *,
    trials: int,
    seed: int,
    workers: Optional[int] = None,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_investigative: bool = False,
    require_doctor: bool = False,
    require_protective: bool = False,
    include_roles: Optional[Set[str]] = None,
    exclude_roles: Optional[Set[str]] = None,
    mafia_override: Optional[int] = None,
    neutral_override: Optional[int] = None,
    role_set: str = "default",
    no_engine_invariants: bool = False,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> GeneratorTrialResults:
    """
    Same as run_generator_weighted_trials but splits trials across CPU workers.

    Each worker uses seed + worker_id * 10_000_019 (statistically equivalent, not
    bit-identical to a single-process seed=seed run).
    """
    cpu = os.cpu_count() or 4
    n_workers = max(1, min(int(workers or cpu), trials))
    base = trials // n_workers
    rem = trials % n_workers
    root = str(Path(__file__).resolve().parents[2])
    gen_base = {
        "root": root,
        "player_count": player_count,
        "no_engine_invariants": no_engine_invariants,
        "collect_stats": True,
        **_generator_trial_kwargs(
            max_investigative=max_investigative,
            exact_investigative=exact_investigative,
            require_investigative=require_investigative,
            require_doctor=require_doctor,
            require_protective=require_protective,
            include_roles=include_roles,
            exclude_roles=exclude_roles,
            mafia_override=mafia_override,
            neutral_override=neutral_override,
            role_set=role_set,
        ),
    }
    jobs: List[Dict[str, Any]] = []
    for i in range(n_workers):
        n = base + (1 if i < rem else 0)
        if n <= 0:
            continue
        jobs.append({**gen_base, "worker_id": i, "trials": n, "seed": seed + i * 10_000_019})

    chunks: List[Dict[str, Any]] = []
    done = 0
    with ProcessPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(_parallel_trial_worker, job): job for job in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            chunks.append(fut.result())
            done += int(job["trials"])
            if on_progress:
                on_progress(done, trials)
    return merge_generator_trial_chunks(chunks)


def run_monte_carlo(roles: List[str], n: int, seed: int) -> Dict[str, float]:
    random.seed(seed)
    counts = _empty_outcome_counts()
    for _ in range(n):
        rr = roles[:]
        random.shuffle(rr)
        res = simulate_once(rr)
        for k, v in res.items():
            if v and k in counts:
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

    bot_roles = _bot_role_universe()
    if ALL_ROLES != bot_roles:
        missing = sorted(bot_roles - ALL_ROLES)
        extra = sorted(ALL_ROLES - bot_roles)
        raise SystemExit(f"SIM AUDIT FAILED role universe. missing={missing} extra={extra}")

    missing_meta = sorted(bot_roles - set(ROLE_META))
    extra_meta = sorted(set(ROLE_META) - bot_roles)
    missing_diff = sorted(bot_roles - set(ROLE_OPTIMAL_DIFFICULTY))
    extra_diff = sorted(set(ROLE_OPTIMAL_DIFFICULTY) - bot_roles)
    if missing_meta or extra_meta or missing_diff or extra_diff:
        raise SystemExit(
            "SIM AUDIT FAILED coverage. "
            f"missing_meta={missing_meta} extra_meta={extra_meta} "
            f"missing_difficulty={missing_diff} extra_difficulty={extra_diff}"
        )
    if "Civilian" in ROLE_OPTIMAL_DIFFICULTY or "Civilian" in ROLE_META:
        raise SystemExit("SIM AUDIT FAILED: Civilian must not appear in sim role tables")
    print(f"SIM AUDIT OK: {len(bot_roles)} roles, optimal_difficulty + ROLE_META complete")


def _start_pool(player_count: int) -> bot_game_roles.StartPool:
    """Current role generator snapshot from game_roles.py."""
    return bot_game_roles.start_pool_for_player_count(player_count, rng=random.Random())


def _bot_town_weights(player_count: int) -> List[Tuple[str, int]]:
    return list(_start_pool(player_count).town_weights)


def _bot_mafia_support_weights(player_count: int) -> List[Tuple[str, int]]:
    return list(_start_pool(player_count).mafia_support_weights)


def _bot_neutral_pool(player_count: int) -> List[str]:
    return list(_start_pool(player_count).neutral_pool_for_draw)


def _bot_num_mafia_neutral(player_count: int) -> Tuple[int, int]:
    return bot_game_roles.mafia_neutral_counts(player_count)


def _filter_role_weights(
    pool: List[Tuple[str, int]],
    exclude_roles: Optional[Set[str]],
) -> List[Tuple[str, int]]:
    if not exclude_roles:
        return pool
    filtered = [(r, w) for r, w in pool if r not in exclude_roles]
    if not filtered:
        raise ValueError(f"exclude_roles removed all entries from weighted pool: {exclude_roles}")
    return filtered


def _filter_role_list(pool: List[str], exclude_roles: Optional[Set[str]]) -> List[str]:
    if not exclude_roles:
        return pool
    filtered = [r for r in pool if r not in exclude_roles]
    if not filtered:
        raise ValueError(f"exclude_roles removed all entries from role list: {exclude_roles}")
    return filtered


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


def sample_generator_roles(
    player_count: int,
    *,
    mafia_override: Optional[int] = None,
    neutral_override: Optional[int] = None,
    exclude_roles: Optional[Set[str]] = None,
    role_set: str = "default",
    rng: Optional[random.Random] = None,
) -> List[str]:
    """
    Sample ONE role-set like bot.py does (supports 5p+), allowing dupes except globally unique roles.
    """
    draw_rng = rng or random.Random()
    if role_set == "tos-classic":
        import game_roles_tos

        if player_count != 7:
            raise ValueError("role_set tos-classic is implemented for 7p only")
        roles = game_roles_tos.sample_tos_classic_roles(player_count, rng=draw_rng)
        if exclude_roles and any(r in roles for r in exclude_roles):
            return sample_generator_roles(
                player_count,
                mafia_override=mafia_override,
                neutral_override=neutral_override,
                exclude_roles=exclude_roles,
                role_set=role_set,
                rng=draw_rng,
            )
        return roles

    if role_set == "tos-rt-dupe":
        import game_roles_tos

        if player_count not in (5, 6, 7):
            raise ValueError("role_set tos-rt-dupe is implemented for 5p/6p/7p only")
        roles = game_roles_tos.sample_tos_rt_dupe_roles(player_count, rng=draw_rng)
        if exclude_roles and any(r in roles for r in exclude_roles):
            return sample_generator_roles(
                player_count,
                mafia_override=mafia_override,
                neutral_override=neutral_override,
                exclude_roles=exclude_roles,
                role_set=role_set,
                rng=draw_rng,
            )
        return roles

    _5P_NO_NEUTRAL_SAMPLERS = {
        "5p-random-town": "sample_5p_no_neutral_random_town",
        "5p-ti-tp-rt2": "sample_5p_no_neutral_ti_tp_rt2",
        "5p-ti2-tp-rt": "sample_5p_no_neutral_ti2_tp_rt",
    }
    if role_set in _5P_NO_NEUTRAL_SAMPLERS:
        import game_roles_tos

        if player_count != 5:
            raise ValueError(f"role_set {role_set!r} is implemented for 5p only")
        fn = getattr(game_roles_tos, _5P_NO_NEUTRAL_SAMPLERS[role_set])
        roles = fn(rng=draw_rng)
        if exclude_roles and any(r in roles for r in exclude_roles):
            return sample_generator_roles(
                player_count,
                mafia_override=mafia_override,
                neutral_override=neutral_override,
                exclude_roles=exclude_roles,
                role_set=role_set,
                rng=draw_rng,
            )
        return roles

    if (
        role_set == "default"
        and player_count in (5, 6, 7)
        and mafia_override is None
        and neutral_override is None
    ):
        roles = bot_game_roles.draw_roles_for_startgame(player_count, rng=draw_rng)
        if exclude_roles and any(r in roles for r in exclude_roles):
            return sample_generator_roles(
                player_count,
                mafia_override=mafia_override,
                neutral_override=neutral_override,
                exclude_roles=exclude_roles,
                role_set=role_set,
                rng=draw_rng,
            )
        if len(roles) != player_count:
            return sample_generator_roles(
                player_count,
                mafia_override=mafia_override,
                neutral_override=neutral_override,
                exclude_roles=exclude_roles,
                role_set=role_set,
                rng=draw_rng,
            )
        return roles

    pool = _start_pool(player_count)
    num_mafia, num_neutral = pool.num_mafia, pool.num_neutral
    if mafia_override is not None:
        num_mafia = int(mafia_override)
    if neutral_override is not None:
        num_neutral = int(neutral_override)
    num_town = player_count - num_mafia - num_neutral
    if num_town < 0:
        return sample_generator_roles(player_count, mafia_override=mafia_override, neutral_override=neutral_override)

    town_weights = _filter_role_weights(pool.town_weights, exclude_roles)
    mafia_support_weights = _filter_role_weights(pool.mafia_support_weights, exclude_roles)
    neutral_pool_for_draw = _filter_role_list(pool.neutral_pool_for_draw, exclude_roles)

    pool_draw = bot_game_roles.StartPool(
        town_weights=town_weights,
        mafia_support_weights=mafia_support_weights,
        neutral_pool_for_draw=neutral_pool_for_draw,
        neutral_pool_for_display=pool.neutral_pool_for_display,
        num_town=num_town,
        num_mafia=num_mafia,
        num_neutral=num_neutral,
        player_count=player_count,
    )
    rng = draw_rng
    neutrals = bot_game_roles.draw_neutrals(pool_draw, rng=rng)
    roles: List[str] = list(neutrals)
    try:
        roles.extend(
            bot_game_roles.get_weighted_roles(town_weights, num_town, rng=rng, roles_so_far=roles)
        )
        mafia_roles = ["Mobster"]
        if num_mafia > 1:
            mafia_roles.extend(
                bot_game_roles.get_weighted_roles(
                    mafia_support_weights,
                    num_mafia - 1,
                    rng=rng,
                    roles_so_far=roles + mafia_roles,
                )
            )
        roles.extend(mafia_roles)
    except ValueError:
        return sample_generator_roles(
            player_count,
            mafia_override=mafia_override,
            neutral_override=neutral_override,
            exclude_roles=exclude_roles,
            role_set=role_set,
            rng=draw_rng,
        )

    if len(roles) != player_count:
        # If pools were too small for some reason, fall back to retry.
        return sample_generator_roles(
            player_count,
            mafia_override=mafia_override,
            neutral_override=neutral_override,
            exclude_roles=exclude_roles,
            role_set=role_set,
            rng=draw_rng,
        )
    if bot_game_roles.lobby_duplicate_violations(roles):
        return sample_generator_roles(
            player_count,
            mafia_override=mafia_override,
            neutral_override=neutral_override,
            exclude_roles=exclude_roles,
            role_set=role_set,
            rng=draw_rng,
        )
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
    role_set: str = "default",
    rng: Optional[random.Random] = None,
) -> List[str]:
    draw_rng = rng or random.Random()
    while True:
        roles = sample_generator_roles(
            player_count,
            mafia_override=mafia_override,
            neutral_override=neutral_override,
            exclude_roles=exclude_roles,
            role_set=role_set,
            rng=draw_rng,
        )
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
    role_set: str = "default",
    diagnostics: bool = False,
    on_progress: Optional[Callable[[int, int], None]] = None,
    progress_every: int = 200,
    workers: Optional[int] = None,
    no_engine_invariants: Optional[bool] = None,
) -> GeneratorTrialResults:
    """
    Directly sample from the role generator and simulate once per draw.
    This is generator-weighted by construction and scales to larger player counts.

    By default uses parallel workers (one process per CPU core). Pass ``workers=1``
    or ``--serial`` on the CLI for a single-process run. ``diagnostics=True`` always
    runs single-process (conditional stats need a serial loop).
    """
    gen_kw = _generator_trial_kwargs(
        max_investigative=max_investigative,
        exact_investigative=exact_investigative,
        require_investigative=require_investigative,
        require_doctor=require_doctor,
        require_protective=require_protective,
        include_roles=include_roles,
        exclude_roles=exclude_roles,
        mafia_override=mafia_override,
        neutral_override=neutral_override,
        role_set=role_set,
    )
    if diagnostics:
        random.seed(seed)
        batch_rng = random.Random(seed)
        diag_sum: SimStats = {
            "days": 0,
            "lynches": 0,
            "mislynches": 0,
            "mislynches_incl_neutrals": 0,
            "lynches_neutral": 0,
            "night_deaths": 0,
            "doc_saves": 0,
            "roleblocks": 0,
            "gatekeeper_blocks": 0,
            "controls": 0,
            "controls_prevent_ignite": 0,
            "ignites": 0,
        }
        cond = ConditionalStats()
        counts = _empty_outcome_counts()
        presence_counts, wins_when_present = init_personal_win_counters()
        total_days = 0
        total_lynches = 0
        for trial_idx in range(trials):
            roles = sample_generator_roles_constraints(player_count, rng=batch_rng, **gen_kw)
            rr = roles[:]
            batch_rng.shuffle(rr)
            res, st = cast(Tuple[Dict[str, bool], SimStats], simulate_once(rr, collect_stats=True))
            total_days += int(st.get("days", 0))
            total_lynches += int(st.get("lynches", 0))
            for k in diag_sum.keys():
                diag_sum[k] += int(st.get(k, 0))
            merge_conditional_stats(cond, accumulate_conditionals(res, st, rr))
            accumulate_personal_win_stats(rr, res, presence_counts, wins_when_present)
            for k, v in res.items():
                if v and k in counts:
                    counts[k] += 1
            if on_progress and progress_every > 0 and (trial_idx + 1) % progress_every == 0:
                on_progress(trial_idx + 1, trials)
        probs = {k: v / trials for k, v in counts.items()}
        probs["avg_days"] = total_days / trials if trials else 0.0
        probs["avg_lynches"] = total_lynches / trials if trials else 0.0
        presence_rate, conditional_win = finalize_personal_win_rates(
            trials=trials,
            presence=presence_counts,
            wins_when_present=wins_when_present,
        )
        print_avg_diagnostics(diag_sum, trials)
        print_conditional_report(cond)
        return GeneratorTrialResults(
            unconditional=probs,
            presence_rate=presence_rate,
            conditional_win=conditional_win,
        )

    from scripts.monte_carlo import config as mc_config
    from scripts.monte_carlo.runtime import default_trial_workers

    skip_invariants = (
        not mc_config.ENGINE_NIGHT_INVARIANTS
        if no_engine_invariants is None
        else bool(no_engine_invariants)
    )
    n_workers = 1 if workers == 1 else max(1, min(int(workers or default_trial_workers(trials)), trials))
    if n_workers > 1:
        return run_generator_weighted_trials_parallel(
            player_count,
            trials=trials,
            seed=seed,
            workers=n_workers,
            max_investigative=max_investigative,
            exact_investigative=exact_investigative,
            require_investigative=require_investigative,
            require_doctor=require_doctor,
            require_protective=require_protective,
            include_roles=include_roles,
            exclude_roles=exclude_roles,
            mafia_override=mafia_override,
            neutral_override=neutral_override,
            role_set=role_set,
            no_engine_invariants=skip_invariants,
            on_progress=on_progress,
        )

    chunk = run_generator_weighted_trials_chunk(
        player_count,
        trials=trials,
        seed=seed,
        collect_stats=True,
        **gen_kw,
    )
    if on_progress and progress_every > 0:
        on_progress(trials, trials)
    return merge_generator_trial_chunks([chunk])

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


def enumerate_role_sets(
    player_count: int,
    *,
    max_unique: int = 8000,
    seed: int = 0,
) -> List[List[str]]:
    """
    Sample unique role lineups from prod ``draw_roles_for_startgame`` (5p–7p manifests).

    With RT dupes the full cross-product is huge; this Monte Carlo-enumerates unique draws.
    """
    if player_count not in {5, 6, 7}:
        raise ValueError("enumerate_role_sets supports 5, 6, or 7 players only.")

    rng = random.Random(seed)
    seen: set[tuple[str, ...]] = set()
    out: List[List[str]] = []
    attempts = 0
    attempt_limit = max(max_unique * 100, 1000)
    while len(out) < max_unique and attempts < attempt_limit:
        attempts += 1
        roles = bot_game_roles.draw_roles_for_startgame(player_count, rng=rng)
        key = tuple(sorted(roles))
        if key in seen:
            continue
        seen.add(key)
        out.append(roles)
    return out


def _all_neutral_role_names() -> Set[str]:
    return {r for pool in bot_game_roles.NEUTRAL_BUCKET_POOLS.values() for r, _w in pool}


def _extract_town_manifest(roles: List[str], player_count: int) -> List[str]:
    non_town: Set[str] = {"Mobster"}
    if player_count != 5:
        non_town |= _all_neutral_role_names()
    return [r for r in roles if r not in non_town]


def _investigative_manifest() -> Set[str]:
    from game_roles_tos import TOWN_INVESTIGATIVE

    return {r for r, _ in TOWN_INVESTIGATIVE}


def _protective_manifest() -> Set[str]:
    from game_roles_tos import TOWN_PROTECTIVE

    return {r for r, _ in TOWN_PROTECTIVE}


# Backward-compatible aliases for older MC scripts.
INVESTIGATIVE_5_6 = _investigative_manifest()
PROTECTIVE_5_6 = _protective_manifest()
_extract_town_5_6 = _extract_town_manifest


def enumerate_role_sets_constraints(
    player_count: int,
    *,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_doctor: bool = False,
    max_unique: int = 8000,
    seed: int = 0,
) -> List[List[str]]:
    """Enumerate 5p–7p role-sets (sampled) and filter by town constraints."""
    if player_count not in {5, 6, 7}:
        raise ValueError("enumerate_role_sets_constraints supports 5, 6, or 7 players only.")

    investigative = _investigative_manifest()
    protective = _protective_manifest()
    sets = enumerate_role_sets(player_count, max_unique=max_unique, seed=seed)
    out: List[List[str]] = []
    for roles in sets:
        town_roles = _extract_town_manifest(roles, player_count)
        inv_count = sum(1 for r in town_roles if r in investigative)

        if max_investigative is not None and inv_count > max_investigative:
            continue
        if exact_investigative is not None and inv_count != exact_investigative:
            continue
        if require_doctor and not any(r in protective for r in town_roles):
            continue

        out.append(roles)
    return out


def enumerate_role_sets_constraints_5_6(
    player_count: int,
    *,
    max_investigative: Optional[int] = None,
    exact_investigative: Optional[int] = None,
    require_doctor: bool = False,
) -> List[List[str]]:
    """Backward-compatible wrapper (5p–7p; name kept for existing callers)."""
    return enumerate_role_sets_constraints(
        player_count,
        max_investigative=max_investigative,
        exact_investigative=exact_investigative,
        require_doctor=require_doctor,
    )


def _town_weights_5_6(player_count: int) -> List[Tuple[str, int]]:
    return _bot_town_weights(player_count)


def _neutral_pool_5_6(player_count: int) -> List[str]:
    return list(_start_pool(player_count).neutral_pool_for_display)


def _sample_generator_roles_manifest(player_count: int, *, rng: random.Random | None = None) -> List[str]:
    """Sample one 5p–7p role-set via prod ``draw_roles_for_startgame``."""
    if player_count not in {5, 6, 7}:
        raise ValueError("Generator sampler supports only 5, 6, or 7 players.")
    return bot_game_roles.draw_roles_for_startgame(player_count, rng=rng or random.Random())


def _sample_generator_roles_5_6(player_count: int) -> List[str]:
    """Backward-compatible alias for ``_sample_generator_roles_manifest``."""
    return _sample_generator_roles_manifest(player_count)


def _sample_generator_roles_5_6_one_investigative(player_count: int) -> List[str]:
    """Sample one role-set with <= 1 investigative town role (manifest TI pool)."""
    investigative = _investigative_manifest()
    while True:
        roles = _sample_generator_roles_manifest(player_count)
        town_roles = _extract_town_manifest(roles, player_count)
        if sum(1 for r in town_roles if r in investigative) <= 1:
            return roles


def estimate_generation_weights(player_count: int, *, samples: int, seed: int) -> Dict[str, float]:
    """
    Estimate P(role_set) under the role generator (not gameplay) via Monte Carlo sampling.
    Returned key is the same string format used in CSV: ', '.join(sorted(roles)).
    """
    random.seed(seed)
    counts: Dict[str, int] = {}
    for _ in range(samples):
        roles = _sample_generator_roles_manifest(player_count)
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
        town_roles = _extract_town_5_6(roles, player_count)
        inv_count = sum(1 for r in town_roles if r in INVESTIGATIVE_5_6)
        if max_investigative is not None and inv_count > max_investigative:
            continue
        if exact_investigative is not None and inv_count != exact_investigative:
            continue
        if require_doctor and not any(r in _protective_manifest() for r in town_roles):
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
