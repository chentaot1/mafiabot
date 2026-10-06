"""ToS1 two-player stalemate resolution (wiki role-vs-role table)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Neutral Killing roles that prevent a Town faction win while still alive.
# Pirate and Chaos are Neutral Chaos (non-factional) and are intentionally omitted.
NEUTRAL_KILLING_ROLES = frozenset({"Arsonist", "Serial Killer"})

_STALEMATE_TABLE_ROLES = frozenset({"Arsonist", "Escort", "Mobster", "Serial Killer", "Transporter"})

# Sorted role pair -> outcome. None = no stalemate (game continues until night resolution).
_TOS1_TWO_PLAYER: Dict[Tuple[str, str], Optional[str]] = {
    ("Arsonist", "Escort"): "Arsonist",
    ("Arsonist", "Mobster"): "Arsonist",
    ("Arsonist", "Serial Killer"): "Arsonist",
    ("Arsonist", "Transporter"): "Arsonist",
    ("Escort", "Mobster"): "Mafia",
    ("Escort", "Serial Killer"): None,
    ("Mobster", "Serial Killer"): "Serial Killer",
    ("Mobster", "Transporter"): "Town",
    ("Serial Killer", "Transporter"): "Serial Killer",
}


def normalize_stalemate_role(role: str) -> Optional[str]:
    """Consort uses the Escort stalemate row."""
    if role == "Consort":
        return "Escort"
    if role in _STALEMATE_TABLE_ROLES:
        return role
    return None


@dataclass(frozen=True)
class TwoPlayerStalemate:
    """Result of looking up two living roles against the ToS1 stalemate table."""

    applies: bool
    winner: Optional[str] = None


def lookup_two_player_stalemate(role_a: str, role_b: str) -> TwoPlayerStalemate:
    """
    When exactly two players remain, resolve per ToS1 stalemate table.

    - applies=False: pair not covered by the table (use normal faction rules).
    - applies=True, winner=str: end the game with that outcome.
    - applies=True, winner=None: no stalemate — keep playing (e.g. SK vs Escort).
    """
    a = normalize_stalemate_role(role_a)
    b = normalize_stalemate_role(role_b)
    if a is None or b is None:
        return TwoPlayerStalemate(applies=False)
    key = tuple(sorted((a, b)))
    if key not in _TOS1_TWO_PLAYER:
        return TwoPlayerStalemate(applies=False)
    return TwoPlayerStalemate(applies=True, winner=_TOS1_TWO_PLAYER[key])
