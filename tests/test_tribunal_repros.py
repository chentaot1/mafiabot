from __future__ import annotations

import json
from pathlib import Path

import pytest

from repro_replay_helpers import run_tribunal_fuzz_payload


ROOT = Path(__file__).resolve().parents[1]
REPRO_DIR = ROOT / "tests" / "repros_tribunal"


def _repro_files() -> list[Path]:
    if not REPRO_DIR.exists():
        return []
    return sorted([p for p in REPRO_DIR.glob("*.json") if p.is_file()])


@pytest.mark.parametrize("path", _repro_files(), ids=lambda p: p.name)
def test_tribunal_repro_replay(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("kind") == "tribunal_fuzz"
    payload = data.get("payload")
    assert isinstance(payload, dict)

    if not isinstance(payload.get("initial"), dict):
        pytest.skip("Legacy tribunal repro without initial snapshot; cannot replay")

    run_tribunal_fuzz_payload(payload)


def test_tribunal_corrupted_fields_roundtrip() -> None:
    """Conservative guard: tribunal-related persisted fields coerce without crash."""
    from game import Game

    for variant in [
        {"phase": "day", "in_progress": True, "vote_in_progress": True, "tribunal_muted": True, "tribunal_defendant_id": 1},
        {"phase": "day", "in_progress": True, "vote_in_progress": "false", "tribunal_muted": 0, "tribunal_defendant_id": "NaN"},
        {"phase": "day", "in_progress": True, "vote_in_progress": None, "tribunal_muted": {}, "tribunal_defendant_id": []},
    ]:
        g = Game.from_persisted(variant)
        g.to_persisted()
