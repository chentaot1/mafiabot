import json
from pathlib import Path

import persistence


ROOT = Path(__file__).resolve().parents[1]
REPRO_DIR = ROOT / "tests" / "repros_persist"


def test_persist_repros_load_state_never_throws(tmp_path: Path) -> None:
    if not REPRO_DIR.exists():
        return

    # We isolate persistence.STATE_DIR to a temp folder to avoid test-order coupling.
    old_dir = persistence.STATE_DIR
    persistence.STATE_DIR = tmp_path
    try:
        for p in sorted(REPRO_DIR.glob("*.json")):
            payload = json.loads(p.read_text(encoding="utf-8"))
            guild_id = int(payload.get("guild_id", 123))
            hex_bytes = payload.get("bytes_hex") or payload.get("bytes_b64", "")
            b = bytes.fromhex(hex_bytes) if isinstance(hex_bytes, str) else b""
            (tmp_path / f"{guild_id}.json").write_bytes(b)
            persistence.load_state(guild_id)
    finally:
        persistence.STATE_DIR = old_dir
