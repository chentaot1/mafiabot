from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from scripts import monte_carlo_sim as sim


ROOT = Path(__file__).resolve().parents[1]
REPRO_DIR = ROOT / "tests" / "repros_monte"


def _repro_files() -> list[Path]:
    if not REPRO_DIR.exists():
        return []
    return sorted([p for p in REPRO_DIR.glob("*.json") if p.is_file()])


@pytest.mark.parametrize("path", _repro_files(), ids=lambda p: p.name)
def test_monte_repro_replays_without_throwing(path: Path) -> None:
    """
    Monte repros are replayed using the Monte Carlo simulator (not the real night engine).
    These repros exist because simulate_once previously threw; after fixes, they should not.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert isinstance(data.get("roles"), list) and data["roles"], "Expected roles list"
    roles = list(data["roles"])
    seed = int(data.get("seed", 1))
    max_days = int(data.get("max_days", 20))

    random.seed(seed)
    sim.simulate_once(roles, max_days=max_days, collect_stats=False, trace=False)
