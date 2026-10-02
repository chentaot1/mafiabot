from __future__ import annotations

import json
from pathlib import Path

import pytest

from repro_replay_helpers import run_phase_repro_payload


ROOT = Path(__file__).resolve().parents[1]
REPRO_DIR = ROOT / "tests" / "repros_phase"


def _repro_files() -> list[Path]:
    if not REPRO_DIR.exists():
        return []
    return sorted([pp for pp in REPRO_DIR.glob("*.json") if pp.is_file()])


@pytest.mark.parametrize("path", _repro_files(), ids=lambda pp: pp.name)
def test_phase_repro_steps_do_not_crash(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("kind") == "phase_fuzz"
    payload = data.get("payload")
    assert isinstance(payload, dict)
    steps = payload.get("steps")
    assert isinstance(steps, list)

    run_phase_repro_payload(payload)
