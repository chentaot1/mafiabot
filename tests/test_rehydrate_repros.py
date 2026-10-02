from __future__ import annotations

import json
from pathlib import Path

import pytest

from repro_replay_helpers import run_rehydrate_fuzz_payload


ROOT = Path(__file__).resolve().parents[1]
REPRO_DIR = ROOT / "tests" / "repros_rehydrate"


def _repro_files() -> list[Path]:
    if not REPRO_DIR.exists():
        return []
    return sorted([p for p in REPRO_DIR.glob("*.json") if p.is_file()])


@pytest.mark.parametrize("path", _repro_files(), ids=lambda p: p.name)
def test_rehydrate_repro_does_not_crash(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("kind") == "rehydrate_fuzz"
    payload = data.get("payload")
    assert isinstance(payload, dict)

    run_rehydrate_fuzz_payload(payload)
