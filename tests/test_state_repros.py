from __future__ import annotations

import json
from pathlib import Path

import pytest

from game import Game


ROOT = Path(__file__).resolve().parents[1]
REPRO_DIR = ROOT / "tests" / "repros_state"


def _repro_files() -> list[Path]:
    if not REPRO_DIR.exists():
        return []
    return sorted([p for p in REPRO_DIR.glob("*.json") if p.is_file()])


@pytest.mark.parametrize("path", _repro_files(), ids=lambda p: p.name)
def test_state_repro_from_persisted_does_not_crash(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("kind") == "state_fuzz"
    payload = data.get("payload")
    assert isinstance(payload, dict)

    g = Game.from_persisted(payload)  # type: ignore[arg-type]
    # Roundtrip should also not crash.
    _ = g.to_persisted()
