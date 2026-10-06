"""Parity tests for faction_win_logic (live game + MC)."""
from __future__ import annotations

from faction_win_logic import (
    ARSONIST_HARMLESS_NEUTRALS,
    arsonist_harmless_neutral_stalemate,
    living_roles_faction_winner,
)
from scripts.monte_carlo.config import ARSO_HARMLESS_NEUTRALS
from scripts.monte_carlo.state import Player, check_main_winner


def test_arso_harmless_sets_match() -> None:
    assert ARSO_HARMLESS_NEUTRALS == ARSONIST_HARMLESS_NEUTRALS


def test_faction_town_win() -> None:
    assert living_roles_faction_winner(["Doctor", "Sheriff"]) == "Town"


def test_faction_mafia_parity() -> None:
    assert living_roles_faction_winner(["Mobster", "Framer", "Doctor"]) == "Mafia"


def test_faction_blocked_by_arsonist() -> None:
    assert living_roles_faction_winner(["Doctor", "Arsonist"]) is None


def test_faction_blocked_by_sk() -> None:
    assert living_roles_faction_winner(["Doctor", "Serial Killer"]) is None


def test_arsonist_harmless_stalemate() -> None:
    roles = ["Arsonist", "Survivor", "Jester"]
    assert arsonist_harmless_neutral_stalemate(roles) is True


def test_arsonist_not_stalemate_with_town() -> None:
    assert arsonist_harmless_neutral_stalemate(["Arsonist", "Doctor"]) is False


def test_check_main_winner_uses_shared_logic() -> None:
    players = [
        Player(i=0, role="Doctor"),
        Player(i=1, role="Sheriff"),
    ]
    for p in players:
        p.alive = True
    assert check_main_winner(players) == "Town"
    assert living_roles_faction_winner([p.role for p in players if p.alive]) == "Town"
