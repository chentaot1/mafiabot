from __future__ import annotations


def test_smoke_test_script_passes() -> None:
    """
    Keep the existing smoke coverage, but run it under pytest so it shows up in CI
    and is easy to run locally with one command.
    """
    import smoke_test

    # Under pytest, users often have DISCORD_TOKEN set in their environment.
    # `bot.py` is an entrypoint that calls `bot.run(TOKEN)` at import-time, which would
    # try to connect to Discord and hang the test run.
    #
    # We still keep all static/DB/persistence smoke checks; we just skip the one check
    # that imports/reloads `bot`.
    smoke_test.check_bot_entrypoint_requires_token = lambda: None  # type: ignore[assignment]
    smoke_test.main()
