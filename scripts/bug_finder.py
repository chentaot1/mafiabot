from __future__ import annotations

"""
Unified bug-finder runner.

This script intentionally reuses existing harnesses:
- scripts/sim_test.py: real night-engine pipeline fuzz + systematic coverage + repro writing
- scripts/monte_bug_sim.py: generator-weighted Monte Carlo runner
- scripts/property_test.py: Hypothesis "never throw" property for night pipeline

Goal: one command to run the whole "self-learning" loop:
  - explore lots of randomized scenarios (seeded, reproducible)
  - save minimal repro artifacts under tests/repros on failure
  - keep specialized scripts available as building blocks

Night-first workflow (engine + smoke gate): use `--night-focus` to skip persistence
lanes, and `--smoke-with-night-sim` so startup/periodic smoke runs
`smoke_test.py --with-night-sim`. Or run `python scripts/night_smoke_gate.py` once.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "tests" / "bug_finder_logs"
REPRO_LANES: Dict[str, Path] = {
    "engine": ROOT / "tests" / "repros",
    "state": ROOT / "tests" / "repros_state",
    "rehydrate": ROOT / "tests" / "repros_rehydrate",
    "phase": ROOT / "tests" / "repros_phase",
    "monte": ROOT / "tests" / "repros_monte",
    "persistence": ROOT / "tests" / "repros_persist",
    "tribunal": ROOT / "tests" / "repros_tribunal",
}


def _append_jsonl(path: Path, obj: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, sort_keys=True) + "\n")


def _latest_engine_repro() -> Optional[Path]:
    repro_dir = ROOT / "tests" / "repros"
    if not repro_dir.exists():
        return None
    files = sorted(repro_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("night_actions"), dict):
                return p
        except Exception:
            continue
    return None


def _lane_mtime_map() -> Dict[str, float]:
    out: Dict[str, float] = {}
    for lane, d in REPRO_LANES.items():
        try:
            if not d.exists():
                out[lane] = 0.0
                continue
            files = [p for p in d.glob("*.json") if p.is_file()]
            out[lane] = max((p.stat().st_mtime for p in files), default=0.0)
        except Exception:
            out[lane] = 0.0
    return out


def _new_repros_since(before: Dict[str, float], after: Dict[str, float]) -> Dict[str, float]:
    created: Dict[str, float] = {}
    for lane, t2 in after.items():
        t1 = before.get(lane, 0.0)
        if t2 > t1:
            created[lane] = t2
    return created


def _pytest_repro_suites_for_lanes(lanes: List[str]) -> List[str]:
    mapping = {
        "engine": "tests/test_repros.py",
        "state": "tests/test_state_repros.py",
        "rehydrate": "tests/test_rehydrate_repros.py",
        "phase": "tests/test_phase_repros.py",
        "monte": "tests/test_monte_repros.py",
        "persistence": "tests/test_persist_repros.py",
        "tribunal": "tests/test_tribunal_repros.py",
    }
    out: List[str] = []
    for lane in lanes:
        p = mapping.get(lane)
        if p:
            out.append(p)
    return out


def _all_pytest_repro_suites() -> List[str]:
    # Explicit file list (no shell globbing assumptions on Windows).
    suites: List[str] = []
    for p in [
        "tests/test_repros.py",
        "tests/test_state_repros.py",
        "tests/test_rehydrate_repros.py",
        "tests/test_phase_repros.py",
        "tests/test_monte_repros.py",
        "tests/test_persist_repros.py",
        "tests/test_tribunal_repros.py",
    ]:
        if (ROOT / p).exists():
            suites.append(p)
    return suites


def main() -> None:
    ap = argparse.ArgumentParser(description="Unified seeded bug-finder runner.")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--player-count", type=int, default=7)

    ap.add_argument("--loop", action="store_true", help="Continuously run bug finding with varying seeds.")
    ap.add_argument("--max-iterations", type=int, default=0, help="Stop after N iterations (0 = unlimited).")
    ap.add_argument("--max-seconds", type=int, default=0, help="Stop after N seconds (0 = unlimited).")
    ap.add_argument("--log-jsonl", action="store_true", help="Append iteration results to tests/bug_finder_logs/runs.jsonl.")
    ap.add_argument("--smoke-every", type=int, default=0, help="If looping, run smoke_test.py every N iterations (0 = never).")
    ap.add_argument("--smoke-once", action="store_true", help="Run smoke_test.py once at startup (before loop).")

    ap.add_argument("--skip-sim", action="store_true", help="Skip scripts/sim_test.py run.")
    ap.add_argument("--skip-monte", action="store_true", help="Skip scripts/monte_bug_sim.py run.")
    ap.add_argument("--skip-property", action="store_true", help="Skip scripts/property_test.py run.")
    ap.add_argument("--skip-state-fuzz", action="store_true", help="Skip scripts/state_fuzz.py run.")
    ap.add_argument("--skip-rehydrate-fuzz", action="store_true", help="Skip scripts/rehydrate_fuzz.py run.")
    ap.add_argument("--skip-phase-fuzz", action="store_true", help="Skip scripts/phase_fuzz.py run.")
    ap.add_argument("--skip-persist-fuzz", action="store_true", help="Skip scripts/persist_file_fuzz.py run.")
    ap.add_argument("--skip-tribunal-fuzz", action="store_true", help="Skip scripts/tribunal_fuzz.py run.")
    ap.add_argument(
        "--night-focus",
        action="store_true",
        help="Skip state/rehydrate/phase/persist/tribunal fuzzers (sim/monte/property only).",
    )
    ap.add_argument(
        "--smoke-with-night-sim",
        action="store_true",
        help="When running smoke_test.py, pass --with-night-sim (night sim_test follow-up).",
    )

    # sim_test controls
    ap.add_argument("--sim-skip-exhaustive", action="store_true", help="Pass --skip-exhaustive to sim_test.")
    ap.add_argument("--sim-systematic-actions", action="store_true", help="Enable sim_test systematic action coverage.")
    ap.add_argument("--sim-systematic-role-sets", type=int, default=0, help="Limit systematic role-sets (0 = all).")
    ap.add_argument("--sim-systematic-pair-samples", type=int, default=1)
    ap.add_argument("--sim-systematic-every", type=int, default=0, help="If looping, run systematic actions every N iterations (0 = never).")
    ap.add_argument("--sim-scenarios-once", action="store_true", help="When looping, run deterministic scenarios once at startup.")
    ap.add_argument("--sim-fuzz-iterations", type=int, default=200)
    ap.add_argument("--sim-skip-fuzz", action="store_true", help="When running sim_test in loop, skip fuzz (systematic-only).")
    ap.add_argument("--adaptive", action="store_true", help="Adapt workloads based on recent lack of findings.")

    # monte_bug_sim controls
    ap.add_argument("--monte-trials", type=int, default=5000)
    ap.add_argument("--monte-max-days", type=int, default=20)
    ap.add_argument("--monte-every", type=int, default=1, help="If looping, run monte every N iterations (default 1).")

    # property_test controls (runs are relatively expensive; default every 10 iterations when looping)
    ap.add_argument("--property-every", type=int, default=10, help="If looping, run property_test every N iterations (0 = never).")
    ap.add_argument("--state-fuzz-iterations", type=int, default=2000)
    ap.add_argument("--state-fuzz-every", type=int, default=1, help="If looping, run state_fuzz every N iterations.")
    ap.add_argument("--rehydrate-fuzz-iterations", type=int, default=2000)
    ap.add_argument("--rehydrate-fuzz-every", type=int, default=1, help="If looping, run rehydrate_fuzz every N iterations.")
    ap.add_argument("--phase-fuzz-iterations", type=int, default=2000)
    ap.add_argument("--phase-fuzz-every", type=int, default=1, help="If looping, run phase_fuzz every N iterations.")
    ap.add_argument("--persist-fuzz-iterations", type=int, default=1000)
    ap.add_argument("--persist-fuzz-every", type=int, default=3, help="If looping, run persist_file_fuzz every N iterations.")
    ap.add_argument("--tribunal-fuzz-iterations", type=int, default=2000)
    ap.add_argument("--tribunal-fuzz-every", type=int, default=2, help="If looping, run tribunal_fuzz every N iterations.")

    ap.add_argument("--replay-repros-on-failure", action="store_true", help="On failure, replay all tests/repros/*.json for confirmation.")
    ap.add_argument("--minimize-repro-on-failure", action="store_true", help="On failure, run scripts/minimize_repro.py on newest engine repro.")
    ap.add_argument(
        "--minimize-state-like-repro-on-failure",
        action="store_true",
        help="On failure, try to minimize the newest state/rehydrate/phase repro (best-effort).",
    )
    ap.add_argument("--pytest-repros-on-failure", action="store_true", help="On failure, run pytest replay suite for all repro lanes.")

    args = ap.parse_args()

    if args.night_focus:
        args.skip_state_fuzz = True
        args.skip_rehydrate_fuzz = True
        args.skip_phase_fuzz = True
        args.skip_persist_fuzz = True
        args.skip_tribunal_fuzz = True

    start = time.time()
    log_path = LOG_DIR / "runs.jsonl"
    no_find_streak = 0
    systematic_every = int(args.sim_systematic_every)
    systematic_role_sets = int(args.sim_systematic_role_sets)
    fuzz_iterations = int(args.sim_fuzz_iterations)
    monte_trials = int(args.monte_trials)

    def _should_stop(i: int) -> bool:
        if args.max_iterations and i >= int(args.max_iterations):
            return True
        if args.max_seconds and (time.time() - start) >= int(args.max_seconds):
            return True
        return False

    deadline_ts: Optional[float] = (start + int(args.max_seconds)) if int(args.max_seconds) else None

    def _deadline_exceeded() -> bool:
        return deadline_ts is not None and time.time() >= deadline_ts

    def _smoke_cmd() -> List[str]:
        cmd: List[str] = [sys.executable, "smoke_test.py"]
        if args.smoke_with_night_sim:
            cmd.append("--with-night-sim")
        return cmd

    def _run(cmd: List[str]) -> None:
        print(f"\n$ {' '.join(cmd)}", flush=True)
        timeout_sec: Optional[float] = None
        if deadline_ts is not None:
            # Avoid tiny subprocess timeouts (e.g. 10ms) that only produce flaky failures.
            timeout_sec = max(1.0, deadline_ts - time.time())
        try:
            subprocess.run(cmd, cwd=str(ROOT), check=True, timeout=timeout_sec)
        except subprocess.TimeoutExpired as e:
            raise subprocess.CalledProcessError(124, cmd, None, str(e)) from e

    def _one_iteration(i: int, seed: int) -> bool:
        nonlocal no_find_streak, systematic_every, systematic_role_sets, fuzz_iterations, monte_trials
        iter_started = time.time()
        before_mtimes = _lane_mtime_map()
        iter_rec: Dict[str, object] = {
            "iteration": int(i),
            "seed": int(seed),
            "started_at": int(iter_started),
            "modes": [],
        }

        # Simple adaptivity: if nothing found for a while, gently increase pressure.
        if args.loop and args.adaptive and i > 0 and (no_find_streak % 20 == 0) and no_find_streak > 0:
            systematic_every = 1 if systematic_every <= 0 else max(1, systematic_every - 1)
            systematic_role_sets = min(200, max(systematic_role_sets, 20) + 10)
            fuzz_iterations = min(2000, fuzz_iterations + 100)
            monte_trials = min(20000, monte_trials + 2000)
        iter_rec["adaptive"] = {
            "no_find_streak": int(no_find_streak),
            "systematic_every": int(systematic_every),
            "systematic_role_sets": int(systematic_role_sets),
            "fuzz_iterations": int(fuzz_iterations),
            "monte_trials": int(monte_trials),
        }
        if _deadline_exceeded():
            iter_rec["modes"].append("time_budget_exceeded")
            iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
            if args.log_jsonl:
                _append_jsonl(log_path, iter_rec)
            return True
        # 1) Real-engine simulation harness (writes JSON repros on failure)
        if not args.skip_sim:
            sim_cmd = [
                sys.executable,
                "scripts/sim_test.py",
                "--player-count",
                str(int(args.player_count)),
                "--seed",
                str(int(seed)),
                "--fuzz-iterations",
                str(int(fuzz_iterations)),
            ]
            if args.sim_skip_exhaustive:
                sim_cmd.append("--skip-exhaustive")
            # In a tight loop, skipping scenarios saves a lot of time.
            if args.loop:
                sim_cmd.append("--skip-scenarios")
                if args.sim_skip_fuzz:
                    sim_cmd.append("--skip-fuzz")

            run_systematic = bool(args.sim_systematic_actions)
            if args.loop and (not run_systematic) and int(systematic_every) > 0:
                run_systematic = (i % int(systematic_every) == 0)
            if run_systematic:
                sim_cmd.append("--systematic-actions")
                sim_cmd += ["--systematic-role-sets", str(int(systematic_role_sets))]
                sim_cmd += ["--systematic-pair-samples", str(int(args.sim_systematic_pair_samples))]
            if _deadline_exceeded():
                iter_rec["modes"].append("time_budget_exceeded")
                iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
                if args.log_jsonl:
                    _append_jsonl(log_path, iter_rec)
                return True
            _run(sim_cmd)
            iter_rec["modes"].append("sim_systematic" if run_systematic else "sim_fuzz")

        # 2) Generator-weighted Monte Carlo day/night simulator (writes repros on failure)
        if not args.skip_monte:
            if (not args.loop) or (int(args.monte_every) > 0 and (i % int(args.monte_every) == 0)):
                if _deadline_exceeded():
                    iter_rec["modes"].append("time_budget_exceeded")
                    iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
                    if args.log_jsonl:
                        _append_jsonl(log_path, iter_rec)
                    return True
                _run(
                    [
                        sys.executable,
                        "scripts/monte_bug_sim.py",
                        "--player-count",
                        str(int(args.player_count)),
                        "--trials",
                        str(int(monte_trials)),
                        "--seed",
                        str(int(seed)),
                        "--max-days",
                        str(int(args.monte_max_days)),
                        "--write-repro",
                    ]
                )
                iter_rec["modes"].append("monte")

        # 3) Hypothesis property check for night pipeline "never throw" (periodic)
        if not args.skip_property:
            run_prop = True
            if args.loop:
                if int(args.property_every) <= 0:
                    run_prop = False
                else:
                    run_prop = (i % int(args.property_every) == 0)
            if run_prop:
                if _deadline_exceeded():
                    iter_rec["modes"].append("time_budget_exceeded")
                    iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
                    if args.log_jsonl:
                        _append_jsonl(log_path, iter_rec)
                    return True
                _run([sys.executable, "scripts/property_test.py"])
                iter_rec["modes"].append("property")

        # 4) Persisted-state fuzz (restart/corruption tolerance)
        if not args.skip_state_fuzz:
            if (not args.loop) or (int(args.state_fuzz_every) > 0 and (i % int(args.state_fuzz_every) == 0)):
                if _deadline_exceeded():
                    iter_rec["modes"].append("time_budget_exceeded")
                    iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
                    if args.log_jsonl:
                        _append_jsonl(log_path, iter_rec)
                    return True
                _run(
                    [
                        sys.executable,
                        "scripts/state_fuzz.py",
                        "--seed",
                        str(int(seed)),
                        "--iterations",
                        str(int(args.state_fuzz_iterations)),
                    ]
                )
                iter_rec["modes"].append("state_fuzz")

        # 5) Restart rehydration fuzz
        if not args.skip_rehydrate_fuzz:
            if (not args.loop) or (int(args.rehydrate_fuzz_every) > 0 and (i % int(args.rehydrate_fuzz_every) == 0)):
                if _deadline_exceeded():
                    iter_rec["modes"].append("time_budget_exceeded")
                    iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
                    if args.log_jsonl:
                        _append_jsonl(log_path, iter_rec)
                    return True
                _run(
                    [
                        sys.executable,
                        "scripts/rehydrate_fuzz.py",
                        "--seed",
                        str(int(seed)),
                        "--iterations",
                        str(int(args.rehydrate_fuzz_iterations)),
                    ]
                )
                iter_rec["modes"].append("rehydrate_fuzz")

        # 6) Phase transition fuzz (day/night/persist roundtrip)
        if not args.skip_phase_fuzz:
            if (not args.loop) or (int(args.phase_fuzz_every) > 0 and (i % int(args.phase_fuzz_every) == 0)):
                if _deadline_exceeded():
                    iter_rec["modes"].append("time_budget_exceeded")
                    iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
                    if args.log_jsonl:
                        _append_jsonl(log_path, iter_rec)
                    return True
                _run(
                    [
                        sys.executable,
                        "scripts/phase_fuzz.py",
                        "--seed",
                        str(int(seed)),
                        "--iterations",
                        str(int(args.phase_fuzz_iterations)),
                    ]
                )
                iter_rec["modes"].append("phase_fuzz")

        # 7) Persistence file fuzz (load_state should never throw)
        if not args.skip_persist_fuzz:
            if (not args.loop) or (int(args.persist_fuzz_every) > 0 and (i % int(args.persist_fuzz_every) == 0)):
                if _deadline_exceeded():
                    iter_rec["modes"].append("time_budget_exceeded")
                    iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
                    if args.log_jsonl:
                        _append_jsonl(log_path, iter_rec)
                    return True
                _run(
                    [
                        sys.executable,
                        "scripts/persist_file_fuzz.py",
                        "--seed",
                        str(int(seed)),
                        "--iterations",
                        str(int(args.persist_fuzz_iterations)),
                    ]
                )
                iter_rec["modes"].append("persist_fuzz")

        # 8) Tribunal/vote persistence fuzz
        if not args.skip_tribunal_fuzz:
            if (not args.loop) or (int(args.tribunal_fuzz_every) > 0 and (i % int(args.tribunal_fuzz_every) == 0)):
                if _deadline_exceeded():
                    iter_rec["modes"].append("time_budget_exceeded")
                    iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
                    if args.log_jsonl:
                        _append_jsonl(log_path, iter_rec)
                    return True
                _run(
                    [
                        sys.executable,
                        "scripts/tribunal_fuzz.py",
                        "--seed",
                        str(int(seed)),
                        "--iterations",
                        str(int(args.tribunal_fuzz_iterations)),
                        "--players",
                        str(int(args.player_count)),
                    ]
                )
                iter_rec["modes"].append("tribunal_fuzz")

        iter_rec["elapsed_ms"] = int((time.time() - iter_started) * 1000)
        after_mtimes = _lane_mtime_map()
        created = _new_repros_since(before_mtimes, after_mtimes)
        if created:
            no_find_streak = 0
            iter_rec["finding"] = {"lanes": sorted(created.keys())}
        else:
            no_find_streak += 1

        if args.log_jsonl:
            _append_jsonl(log_path, iter_rec)
        return False

    if not args.loop:
        if _one_iteration(0, int(args.seed)):
            print("\nbug_finder.py: stopped (time budget exhausted)", flush=True)
            return
        print("\nbug_finder.py: OK", flush=True)
        return

    i = 0
    base_seed = int(args.seed)

    # Optional startup sanity: run deterministic scenarios once.
    if args.sim_scenarios_once and (not args.skip_sim):
        _run(
            [
                sys.executable,
                "scripts/sim_test.py",
                "--player-count",
                str(int(args.player_count)),
                "--seed",
                str(int(base_seed)),
                "--fuzz-iterations",
                "1",
                "--skip-exhaustive",
            ]
        )

    # Optional startup smoke gate.
    if args.smoke_once:
        _run(_smoke_cmd())
    while True:
        if _should_stop(i):
            break
        seed = base_seed + i
        print(f"\n=== bug_finder loop iteration={i} seed={seed} ===", flush=True)
        loop_before_mtimes = _lane_mtime_map()
        try:
            if _one_iteration(i, seed):
                break
            if int(args.smoke_every) > 0 and (i % int(args.smoke_every) == 0):
                _run(_smoke_cmd())
        except subprocess.CalledProcessError as e:
            # Exit 124: subprocess hit --max-seconds wall (see _run timeout). Not a harness failure.
            if e.returncode == 124 and int(args.max_seconds) > 0:
                print("\nbug_finder.py: stopped (time budget exhausted)", flush=True)
                break
            if args.replay_repros_on_failure:
                try:
                    _run([sys.executable, "scripts/replay_repros.py"])
                except Exception:
                    pass
            if args.pytest_repros_on_failure:
                try:
                    _run([sys.executable, "-m", "pytest", "-q", *_all_pytest_repro_suites()])
                except Exception:
                    pass
            # Best-effort targeted replay: only suites for lanes that were newly created recently.
            try:
                loop_after_mtimes = _lane_mtime_map()
                created_recent = _new_repros_since(loop_before_mtimes, loop_after_mtimes)
                suites = _pytest_repro_suites_for_lanes(sorted(created_recent.keys()))
                if suites:
                    _run([sys.executable, "-m", "pytest", "-q", *suites])
            except Exception:
                pass
            if args.minimize_repro_on_failure:
                p = _latest_engine_repro()
                if p is not None:
                    try:
                        _run([sys.executable, "scripts/minimize_repro.py", str(p)])
                    except Exception:
                        pass
            if args.minimize_state_like_repro_on_failure:
                for lane in ["phase", "rehydrate", "state", "tribunal"]:
                    d = REPRO_LANES.get(lane)
                    if not d or not d.exists():
                        continue
                    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if not files:
                        continue
                    try:
                        _run([sys.executable, "scripts/minimize_state_like_repro.py", str(files[0]), "--in-place"])
                    except Exception:
                        pass
            raise
        i += 1

    print(f"\nbug_finder.py: OK  iterations={i}", flush=True)


if __name__ == "__main__":
    main()
