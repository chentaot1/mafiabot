from __future__ import annotations

import argparse
import json
import logging
import os
import random
import string
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import persistence  # noqa: E402

REPRO_DIR = ROOT / "tests" / "repros_persist"


def _rand_ascii(rng: random.Random, n: int) -> str:
    alphabet = string.ascii_letters + string.digits + " \n\t{}[],:\"'\\/"
    return "".join(rng.choice(alphabet) for _ in range(n))


def _write_bytes(path: Path, b: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(b)


def _mutate_bytes(rng: random.Random, b: bytes) -> bytes:
    if not b:
        return b
    bb = bytearray(b)
    mode = rng.choice(["flip", "delete", "insert", "truncate"])
    if mode == "flip":
        for _ in range(rng.randint(1, 8)):
            i = rng.randrange(len(bb))
            bb[i] ^= rng.randrange(1, 255)
    elif mode == "delete":
        for _ in range(rng.randint(1, 8)):
            if not bb:
                break
            i = rng.randrange(len(bb))
            del bb[i]
    elif mode == "insert":
        for _ in range(rng.randint(1, 8)):
            i = rng.randrange(len(bb) + 1)
            bb.insert(i, rng.randrange(0, 256))
    elif mode == "truncate":
        new_len = rng.randrange(0, len(bb))
        bb = bb[:new_len]
    return bytes(bb)


def _save_repro(kind: str, seed: int, iteration: int, payload: Dict[str, Any]) -> Path:
    REPRO_DIR.mkdir(parents=True, exist_ok=True)
    p = REPRO_DIR / f"{int(time.time())}_{kind}_seed{seed}_i{iteration}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return p


def _try_load(guild_id: int) -> Optional[Dict[str, Any]]:
    return persistence.load_state(guild_id)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fuzz persistence.load_state file contents (truncation/garbling).")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--iterations", type=int, default=2000)
    ap.add_argument("--guild-id", type=int, default=123)
    args = ap.parse_args()

    rng = random.Random(int(args.seed))
    guild_id = int(args.guild_id)

    old_state_dir = persistence.STATE_DIR
    base_obj = {"guild_id": guild_id, "phase": "day", "player_ids": [], "living_ids": []}
    base_bytes = json.dumps(base_obj, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            persistence.STATE_DIR = Path(td)  # type: ignore[assignment]
            path = persistence.STATE_DIR / f"{guild_id}.json"

            logging.disable(logging.CRITICAL)
            try:
                for i in range(int(args.iterations)):
                    kind = rng.choice(["empty", "random_ascii", "random_bytes", "mutate_valid", "truncate_valid"])
                    if kind == "empty":
                        b = b""
                    elif kind == "random_ascii":
                        b = _rand_ascii(rng, rng.randint(0, 4096)).encode("utf-8", errors="ignore")
                    elif kind == "random_bytes":
                        b = os.urandom(rng.randint(0, 4096))
                    elif kind == "mutate_valid":
                        b = _mutate_bytes(rng, base_bytes)
                    elif kind == "truncate_valid":
                        n = rng.randrange(0, len(base_bytes) + 1)
                        b = base_bytes[:n]
                    else:
                        b = base_bytes

                    _write_bytes(path, b)
                    try:
                        _try_load(guild_id)
                    except Exception as e:
                        repro = _save_repro(
                            kind=kind,
                            seed=int(args.seed),
                            iteration=int(i),
                            payload={
                                "guild_id": guild_id,
                                "kind": kind,
                                "bytes_hex": b.hex(),
                                "exception_type": type(e).__name__,
                                "exception": str(e),
                            },
                        )
                        raise RuntimeError(f"persist_file_fuzz found crash; repro={repro}") from e
            finally:
                logging.disable(logging.NOTSET)
    finally:
        persistence.STATE_DIR = old_state_dir  # type: ignore[assignment]


if __name__ == "__main__":
    main()
