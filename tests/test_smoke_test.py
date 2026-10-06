from __future__ import annotations


def test_smoke_test_script_passes() -> None:
    """
    Keep the existing smoke coverage, but run it under pytest so it shows up in CI
    and is easy to run locally with one command.
    """
    import smoke_test

    smoke_test.main()
