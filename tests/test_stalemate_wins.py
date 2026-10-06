"""ToS1 two-player stalemate table (wiki parity)."""
from __future__ import annotations

import pytest

from stalemate_wins import lookup_two_player_stalemate


@pytest.mark.parametrize(
    "role_a, role_b, expected",
    [
        ("Arsonist", "Escort", "Arsonist"),
        ("Escort", "Arsonist", "Arsonist"),
        ("Arsonist", "Mobster", "Arsonist"),
        ("Mobster", "Arsonist", "Arsonist"),
        ("Arsonist", "Serial Killer", "Arsonist"),
        ("Serial Killer", "Arsonist", "Arsonist"),
        ("Arsonist", "Transporter", "Arsonist"),
        ("Transporter", "Arsonist", "Arsonist"),
        ("Escort", "Mobster", "Mafia"),
        ("Mobster", "Escort", "Mafia"),
        ("Escort", "Serial Killer", None),
        ("Serial Killer", "Escort", None),
        ("Mobster", "Serial Killer", "Serial Killer"),
        ("Serial Killer", "Mobster", "Serial Killer"),
        ("Mobster", "Transporter", "Town"),
        ("Transporter", "Mobster", "Town"),
        ("Serial Killer", "Transporter", "Serial Killer"),
        ("Transporter", "Serial Killer", "Serial Killer"),
    ],
)
def test_tos1_two_player_stalemate_table(role_a: str, role_b: str, expected: str | None) -> None:
    result = lookup_two_player_stalemate(role_a, role_b)
    assert result.applies is True
    assert result.winner == expected


def test_consort_uses_escort_row() -> None:
    result = lookup_two_player_stalemate("Consort", "Mobster")
    assert result.applies is True
    assert result.winner == "Mafia"


def test_off_table_pair_does_not_apply() -> None:
    result = lookup_two_player_stalemate("Survivor", "Serial Killer")
    assert result.applies is False


def test_check_main_winner_sk_escort_no_premature_town() -> None:
    from scripts.monte_carlo.state import Player, check_main_winner

    players = [
        Player(i=0, role="Escort"),
        Player(i=1, role="Serial Killer"),
    ]
    for p in players:
        p.alive = True
    assert check_main_winner(players) is None


def test_check_main_winner_transporter_mobster_town() -> None:
    from scripts.monte_carlo.state import Player, check_main_winner

    players = [
        Player(i=0, role="Transporter"),
        Player(i=1, role="Mobster"),
    ]
    for p in players:
        p.alive = True
    assert check_main_winner(players) == "Town"


def test_check_main_winner_mobster_sk_serial_killer() -> None:
    from scripts.monte_carlo.state import Player, check_main_winner

    players = [
        Player(i=0, role="Mobster"),
        Player(i=1, role="Serial Killer"),
    ]
    for p in players:
        p.alive = True
    assert check_main_winner(players) is None  # table gives SK; handled by try_two_player_stalemate_end


def test_try_two_player_stalemate_end_mobster_sk() -> None:
    from scripts.monte_carlo.state import Player
    from scripts.monte_carlo.wins import try_two_player_stalemate_end

    players = [
        Player(i=0, role="Mobster"),
        Player(i=1, role="Serial Killer"),
    ]
    alive = {0, 1}
    out: dict[str, bool] = {"Town": False, "Mafia": False, "Serial Killer": False}
    assert try_two_player_stalemate_end(players, alive, out) is True
    assert out["Serial Killer"] is True


def test_town_blocked_while_sk_alive_three_way() -> None:
    from scripts.monte_carlo.state import Player, check_main_winner

    players = [
        Player(i=0, role="Doctor"),
        Player(i=1, role="Sheriff"),
        Player(i=2, role="Serial Killer"),
    ]
    for p in players:
        p.alive = True
    assert check_main_winner(players) is None
