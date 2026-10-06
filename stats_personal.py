"""Canonical personal-win keys, labels, and JSON migration (stats / !stats / leaderboard)."""

from __future__ import annotations

from typing import Dict, Mapping, MutableMapping

# Canonical personal-win stat keys (SQLite player_personal_stats.key).
PERSONAL_WIN_KEYS: tuple[str, ...] = (
    "pirate_win",
    "exe_win",
    "jester_win",
    "survivor_survived",
    "chaos_survived",
    "witch_town_loses",
    "arsonist_win",
    "guardian_angel_win",
    "serial_killer_win",
)

PERSONAL_WIN_LABELS: Dict[str, str] = {
    "pirate_win": "Pirate (2 plunders)",
    "exe_win": "Executioner (target lynched)",
    "jester_win": "Jester (lynched)",
    "survivor_survived": "Survivor (survived)",
    "chaos_survived": "Chaos (survived)",
    "witch_town_loses": "Witch (evil win while alive)",
    "arsonist_win": "Arsonist (won)",
    "guardian_angel_win": "Guardian Angel (won)",
    "serial_killer_win": "Serial Killer (won)",
}

# Legacy JSON / import keys folded into canonical keys on read/write.
LEGACY_PERSONAL_KEY_TO_CANONICAL: Dict[str, str] = {
    "Pirate": "pirate_win",
    "Executioner": "exe_win",
    "Jester": "jester_win",
    "Survivor": "survivor_survived",
    "Chaos": "chaos_survived",
    "Witch": "witch_town_loses",
    "Arsonist": "arsonist_win",
    "guardian_angel_joint": "guardian_angel_win",
}


def _safe_int(v: object) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def migrate_personal_wins_dict(raw: object) -> Dict[str, int]:
    """Normalize personal_wins: legacy role names + guardian_angel_joint → canonical keys."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for k, v in raw.items():
        key = str(k)
        if key in LEGACY_PERSONAL_KEY_TO_CANONICAL:
            continue
        if key in PERSONAL_WIN_LABELS or key in PERSONAL_WIN_KEYS:
            out[key] = out.get(key, 0) + _safe_int(v)
    for old_key, new_key in LEGACY_PERSONAL_KEY_TO_CANONICAL.items():
        prev = _safe_int(raw.get(old_key, 0))
        if prev:
            out[new_key] = out.get(new_key, 0) + prev
    return out


def apply_personal_win_delta(personal: MutableMapping[str, int], key: str, delta: int = 1) -> None:
    if delta <= 0:
        return
    personal[key] = _safe_int(personal.get(key, 0)) + delta


def personal_wins_for_display(personal_raw: Mapping[str, object]) -> Dict[str, int]:
    """Canonical counts keyed by friendly label for !stats embeds."""
    norm = migrate_personal_wins_dict(dict(personal_raw) if personal_raw else {})
    display: Dict[str, int] = {}
    for key, count in norm.items():
        if count <= 0:
            continue
        label = PERSONAL_WIN_LABELS.get(key, key.replace("_", " ").title())
        display[label] = display.get(label, 0) + int(count)
    return display
