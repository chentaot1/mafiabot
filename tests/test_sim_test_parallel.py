"""Parallel defaults for sim_test systematic action coverage."""
from __future__ import annotations

import asyncio
import itertools
import os

import pytest

from scripts.monte_carlo.runtime import default_trial_workers
from scripts.sim_test import enumerate_all_role_sets, systematic_action_coverage


def _two_role_sets() -> list[list[str]]:
    return list(itertools.islice(enumerate_all_role_sets(7), 2))


@pytest.mark.asyncio
async def test_systematic_parallel_matches_serial() -> None:
    role_sets = _two_role_sets()
    kw = dict(
        seed=1,
        per_roleset_pair_samples=1,
        save_repros=False,
        dedupe_actions=True,
        tuple_size=2,
        all_roles_act=False,
    )
    serial = await systematic_action_coverage(role_sets=role_sets, jobs=1, **kw)
    parallel = await systematic_action_coverage(role_sets=role_sets, jobs=2, **kw)
    assert parallel.nights_executed == serial.nights_executed
    assert parallel.nights_deduped == serial.nights_deduped
    assert parallel.nights_executed > 0


@pytest.mark.asyncio
async def test_systematic_default_jobs_uses_parallel_when_applicable() -> None:
    cpu = os.cpu_count() or 4
    if cpu < 2:
        pytest.skip("needs 2+ CPUs")
    role_sets = _two_role_sets()
    assert default_trial_workers(len(role_sets)) >= 2
    stats = await systematic_action_coverage(
        role_sets=role_sets,
        seed=1,
        per_roleset_pair_samples=1,
        save_repros=False,
        dedupe_actions=True,
        jobs=None,
        tuple_size=2,
        all_roles_act=False,
    )
    assert stats.nights_executed > 0
