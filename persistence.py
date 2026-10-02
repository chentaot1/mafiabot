import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional


STATE_DIR = Path(__file__).resolve().parent / "state"


def _state_path(guild_id: int) -> Path:
    return STATE_DIR / f"{guild_id}.json"

def _stats_path(guild_id: int) -> Path:
    return STATE_DIR / f"{guild_id}.stats.json"


def load_state(guild_id: int) -> Optional[Dict[str, Any]]:
    path = _state_path(guild_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Failed to load persisted state from %s; treating as no state.", str(path))
        # If the file is corrupted, fail closed (treat as no state).
        return None


def save_state(guild_id: int, data: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(guild_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


async def save_state_async(guild_id: int, data: Dict[str, Any]) -> None:
    await asyncio.to_thread(save_state, guild_id, data)


def delete_state(guild_id: int) -> None:
    path = _state_path(guild_id)
    try:
        path.unlink(missing_ok=True)  # py3.8+ on Windows supports missing_ok
    except TypeError:
        # Fallback for older runtimes.
        if path.exists():
            path.unlink()


def load_stats(guild_id: int) -> Optional[Dict[str, Any]]:
    path = _stats_path(guild_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Failed to load stats from %s; treating as no stats.", str(path))
        return None


def save_stats(guild_id: int, data: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _stats_path(guild_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


async def save_stats_async(guild_id: int, data: Dict[str, Any]) -> None:
    await asyncio.to_thread(save_stats, guild_id, data)
