"""Parallel generator-weighted Monte Carlo trials."""
from __future__ import annotations

import os

from scripts.monte_carlo.generator import (
    merge_generator_trial_chunks,
    run_generator_weighted_trials,
    run_generator_weighted_trials_chunk,
    run_generator_weighted_trials_parallel,
)
from scripts.monte_carlo.runtime import close_async_loop, configure_quiet_logging, default_trial_workers

configure_quiet_logging()


def test_default_trial_workers_capped_by_trials() -> None:
    cpu = os.cpu_count() or 4
    assert default_trial_workers(1) == 1
    assert default_trial_workers(4) == min(4, cpu)
    assert default_trial_workers(1_000_000) == cpu


def test_parallel_workers_produce_valid_rates() -> None:
    try:
        results = run_generator_weighted_trials(
            7,
            trials=80,
            seed=99,
            role_set="tos-rt-dupe",
            workers=2,
            no_engine_invariants=True,
        )
    finally:
        close_async_loop()
    town = float(results.unconditional["Town"])
    mafia = float(results.unconditional["Mafia"])
    assert 0.0 <= town <= 1.0
    assert 0.0 <= mafia <= 1.0
    assert town + mafia < 1.05


def test_parallel_merge_matches_single_chunk() -> None:
    try:
        chunk = run_generator_weighted_trials_chunk(
            7,
            trials=50,
            seed=7,
            role_set="tos-rt-dupe",
        )
    finally:
        close_async_loop()
    merged = merge_generator_trial_chunks([chunk])
    assert merged.unconditional["Town"] == chunk["counts"]["Town"] / chunk["trials"]


def test_default_uses_parallel_when_workers_unset() -> None:
    cpu = os.cpu_count() or 4
    if cpu < 2:
        return
    try:
        results = run_generator_weighted_trials(
            7,
            trials=24,
            seed=1,
            role_set="tos-rt-dupe",
            no_engine_invariants=True,
        )
    finally:
        close_async_loop()
    assert 0.0 <= float(results.unconditional["Town"]) <= 1.0


def test_run_generator_weighted_trials_parallel_smoke() -> None:
    try:
        results = run_generator_weighted_trials_parallel(
            7,
            trials=40,
            seed=1,
            workers=2,
            role_set="tos-rt-dupe",
            no_engine_invariants=True,
        )
    finally:
        close_async_loop()
    assert results.unconditional["Draw"] >= 0.0
