"""CLI for Monte Carlo balance simulator."""
from __future__ import annotations

import argparse
import logging
import random
from pathlib import Path
from typing import Dict, List, Tuple, cast

from scripts.monte_carlo import config
from scripts.monte_carlo.audit import audit_against_bot_config
from scripts.monte_carlo.config import SimStats, _clamp01, ALL_ROLES
from scripts.monte_carlo.report import (
    GeneratorTrialResults,
    accumulate_personal_win_stats,
    finalize_personal_win_rates,
    init_personal_win_counters,
    print_trial_summary,
)
from scripts.monte_carlo.generator import (
    generator_role_distribution,
    run_enumeration,
    run_generator_weighted_trials,
    sample_generator_roles_constraints,
)
from scripts.monte_carlo.diagnostics_report import (
    ConditionalStats,
    accumulate_conditionals,
    merge_conditional_stats,
    print_avg_diagnostics,
    print_conditional_report,
)
from scripts.monte_carlo.simulate import simulate_once


def print_effective_competence_table() -> None:
    """Print per-axis difficulty and computed P(optimal) for current lobby settings."""
    mode = "off (expert)" if not config.USE_DIFFICULTY_LAYER else "on"
    print(f"Competence model ({mode}) lobby_skill={config.LOBBY_SKILL:.2f}")
    print(
        f"{'Role':<18} {'TgtDiff':>8} {'P(tgt)':>7} {'UseDiff':>8} {'P(use)':>7} {'DayDiff':>8} {'P(day)':>7}"
    )
    for role in sorted(ALL_ROLES):
        td = config.ROLE_TARGETING_DIFFICULTY.get(role, 0.50)
        ud = config.ROLE_USAGE_DIFFICULTY.get(role, 0.50)
        dd = config.ROLE_DAY_DIFFICULTY.get(role, 0.50)
        pt = config._competence_for_axis(role, "targeting")
        pu = config._competence_for_axis(role, "usage")
        pd = config._competence_for_axis(role, "day")
        print(f"{role:<18} {td:>8.2f} {pt:>7.2f} {ud:>8.2f} {pu:>7.2f} {dd:>8.2f} {pd:>7.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000, help="Rollouts for a single fixed role list (use with --roles).")
    ap.add_argument("--enumerate", type=int, choices=[5, 6], help="Enumerate all role-sets for this player count.")
    ap.add_argument(
        "--generator-trials",
        type=int,
        default=2000,
        help="Sample from game_roles.py generator (default 2000). Set 0 to disable when using --roles.",
    )
    ap.add_argument(
        "--player-count",
        type=int,
        default=7,
        help="Player count for generator mode (default 7).",
    )
    ap.add_argument(
        "--role-set",
        choices=("default", "tos-classic", "tos-rt-dupe"),
        default="default",
        help=(
            "Role generator: default, tos-classic, or tos-rt-dupe "
            "(5p/6p/7p TI+TP+RT slot manifests; neutrals 25%% per bucket)."
        ),
    )
    ap.add_argument(
        "--generator-distribution",
        action="store_true",
        help="In generator-trials mode, print role frequency distribution instead of win rates.",
    )
    ap.add_argument("--n-per", type=int, default=1000, help="Rollouts per role-set when using --enumerate.")
    ap.add_argument("--seed", type=int, default=20260429)
    ap.add_argument(
        "--roles",
        nargs="+",
        default=None,
        help="Fixed role list (--n rollouts). Disables default generator sampling.",
    )
    ap.add_argument(
        "--fixed-lobby",
        action="store_true",
        help="Shortcut for the 7p default lobby in config.DEFAULT_LOBBY_ROLES (1 Mafia, 2 Neutral, 4 Town).",
    )
    ap.add_argument("--audit", action="store_true", help="Fail fast if simulator role sets diverge from config.py.")
    ap.add_argument(
        "--lobby-skill",
        type=float,
        default=0.5,
        help="Table execution skill in [0,1]. 0=low-skill pub, 0.5=average pub, 1.0=high-skill stack.",
    )
    ap.add_argument(
        "--show-competence",
        action="store_true",
        help="Print theoretical role difficulty and effective P(optimal action) for current lobby-skill, then exit.",
    )
    ap.add_argument("--no-difficulty", action="store_true", help="Expert mode: always take optimal targets (disables difficulty layer).")
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
    ap.add_argument(
        "--lynch-prob-per",
        type=float,
        default=None,
        help="P(lynch attempt) per suspicion point on unique top suspect (default from config, 0.22).",
    )
    ap.add_argument(
        "--lynch-prob-cap",
        type=float,
        default=None,
        help="Max P(lynch attempt) from suspicion scaling (default 0.95).",
    )
    ap.add_argument("--diagnostics", action="store_true", help="In generator-trials mode: print attribution diagnostics (mislynches, blocks, saves, etc.).")
    ap.add_argument(
        "--trace-one",
        action="store_true",
        help="Run one game with day-by-day trace (fixed default/--roles lobby or generator-trials).",
    )
    ap.add_argument(
        "--no-realistic-nights",
        action="store_true",
        help="Legacy night AI: probabilistic skips (Witch 35%%, Chaos 75%%, N1 Vig 10%%, etc.).",
    )
    ap.add_argument(
        "--no-engine-invariants",
        action="store_true",
        help="Skip post-night invariant checks in the engine bridge (faster, less safe).",
    )
    ap.add_argument(
        "--parity-nights",
        action="store_true",
        help="Run scripts/n1_golden_nights parity locks before trials (engine outcome checks).",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress delivery/game logging noise during trials (clean summary output).",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="Parallel trial worker processes (default: CPU count). Use 1 or --serial for single-process.",
    )
    ap.add_argument(
        "--serial",
        action="store_true",
        help="Run generator trials in a single process (same as --workers 1).",
    )
    args = ap.parse_args()

    if args.quiet:
        from scripts.monte_carlo.runtime import configure_quiet_logging

        configure_quiet_logging()

    config.LOBBY_SKILL = _clamp01(float(args.lobby_skill))
    config.USE_DIFFICULTY_LAYER = not bool(args.no_difficulty)
    config.GATEKEEPER_BLOCKS_ONE = bool(args.gatekeeper_blocks_one)
    config.REALISTIC_NIGHT_ACTIONS = not bool(args.no_realistic_nights)
    config.REALISTIC_N1_VIG_SHOOT = config.REALISTIC_NIGHT_ACTIONS
    config.ENGINE_NIGHT_INVARIANTS = not bool(args.no_engine_invariants)
    if args.lynch_prob_per is not None:
        config.LYNCH_PROB_PER_SUSPICION = float(args.lynch_prob_per)
    if args.lynch_prob_cap is not None:
        config.LYNCH_PROB_CAP = float(args.lynch_prob_cap)

    if args.audit:
        audit_against_bot_config()
        return

    if args.parity_nights:
        import asyncio

        from scripts.n1_golden_nights import run_all_golden_nights

        fails = asyncio.run(run_all_golden_nights())
        if fails:
            raise SystemExit(f"parity nights failed: {fails} scenario(s)")
        print("parity nights: OK", flush=True)

    if args.show_competence:
        print_effective_competence_table()
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

    if args.roles or args.fixed_lobby:
        roles = list(args.roles) if args.roles else list(config.DEFAULT_LOBBY_ROLES)
        trials = int(args.n)
        random.seed(args.seed)
        if args.trace_one:
            rr = roles[:]
            random.shuffle(rr)
            out, trace_log = cast(Tuple[Dict[str, bool], List[str]], simulate_once(rr, trace=True))
            print("\n".join(trace_log))
            print("\nSummary:", {k: v for k, v in out.items() if v})
            return
        _run_fixed_role_batch(roles, trials, args)
        return

    if args.generator_trials:
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
                role_set=args.role_set,
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
            for role, cnt in sorted(role_counts.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"{role}: {cnt / args.generator_trials:.4f}")
            top_sets = sorted(lobby_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
            print("\nTop 10 role-sets:")
            for key, cnt in top_sets:
                print(f"{cnt / args.generator_trials:.4f} :: {key}")
        else:
            trial_workers = 1 if args.serial else args.workers
            results = run_generator_weighted_trials(
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
                role_set=args.role_set,
                diagnostics=bool(args.diagnostics),
                workers=trial_workers,
            )
            role_set_label = args.role_set
            workers_note = "serial" if trial_workers == 1 else f"workers={trial_workers or 'auto'}"
            print_trial_summary(
                header=(
                    f"Generator-weighted trials: player_count={args.player_count} "
                    f"trials={args.generator_trials} role_set={role_set_label} {workers_note}"
                ),
                trials=args.generator_trials,
                results=results,
                avg_lynches=results.get("avg_lynches"),
                avg_days=results.get("avg_days"),
                lynch_prob_per=config.LYNCH_PROB_PER_SUSPICION,
                lynch_prob_cap=config.LYNCH_PROB_CAP,
            )
        return

    raise SystemExit(
        "No mode selected. Default is generator sampling; use --generator-trials 0 with --roles for a fixed lobby."
    )


def _run_fixed_role_batch(roles: List[str], trials: int, args: argparse.Namespace) -> None:
    total_days = 0
    total_lynches = 0
    counts: Dict[str, int] = {
        "Town": 0,
        "Mafia": 0,
        "Draw": 0,
        "Executioner": 0,
        "Jester": 0,
        "Survivor": 0,
        "Guardian Angel": 0,
        "Pirate": 0,
        "Arsonist": 0,
        "Chaos": 0,
        "Serial Killer": 0,
        "Witch": 0,
    }
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
        "ignites": 0,
    }
    cond = ConditionalStats()
    presence_counts, wins_when_present = init_personal_win_counters()

    for _ in range(trials):
        rr = roles[:]
        random.shuffle(rr)
        res, st = cast(Tuple[Dict[str, bool], SimStats], simulate_once(rr, collect_stats=True))
        total_days += int(st.get("days", 0))
        total_lynches += int(st.get("lynches", 0))
        if args.diagnostics:
            for k in diag_sum.keys():
                diag_sum[k] += int(st.get(k, 0))
            merge_conditional_stats(cond, accumulate_conditionals(res, st, rr))
        accumulate_personal_win_stats(rr, res, presence_counts, wins_when_present)
        for k, v in res.items():
            if v:
                counts[k] = counts.get(k, 0) + 1
    probs = {k: v / trials for k, v in counts.items()}
    probs["avg_days"] = total_days / trials if trials else 0.0
    probs["avg_lynches"] = total_lynches / trials if trials else 0.0
    presence_rate, conditional_win = finalize_personal_win_rates(
        trials=trials,
        presence=presence_counts,
        wins_when_present=wins_when_present,
    )
    results = GeneratorTrialResults(
        unconditional=probs,
        presence_rate=presence_rate,
        conditional_win=conditional_win,
    )

    print("Roles:", ", ".join(roles))
    print_trial_summary(
        header=f"Fixed lineup trials={trials} seed={args.seed}",
        trials=trials,
        results=results,
        avg_lynches=results.get("avg_lynches"),
        avg_days=results.get("avg_days"),
        lynch_prob_per=config.LYNCH_PROB_PER_SUSPICION,
        lynch_prob_cap=config.LYNCH_PROB_CAP,
        extra_line=(
            f"difficulty={'off' if not config.USE_DIFFICULTY_LAYER else 'on'} "
            f"lobby_skill={config.LOBBY_SKILL:.2f}"
        ),
    )
    if args.diagnostics:
        print_avg_diagnostics(diag_sum, trials)
        print_conditional_report(cond)


if __name__ == "__main__":
    main()
