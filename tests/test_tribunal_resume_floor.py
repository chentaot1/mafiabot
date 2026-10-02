"""Pairs with B4: tribunal resume floor constant is tunable and defaults safely."""

from config import TRIBUNAL_RESUME_MIN_SECONDS


def test_tribunal_resume_floor_is_positive() -> None:
    assert isinstance(TRIBUNAL_RESUME_MIN_SECONDS, int)
    assert TRIBUNAL_RESUME_MIN_SECONDS >= 1
