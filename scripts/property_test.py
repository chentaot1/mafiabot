from __future__ import annotations

"""
Property-based tests for the real night-resolution pipeline.

Run:
  pip install -r requirements.txt -r requirements-dev.txt
  python scripts/property_test.py
"""

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
import random

# Ensure repo root is importable when executed as scripts/property_test.py
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hypothesis import given, settings, strategies as st  # type: ignore

from scripts.sim_test import FakeGuild, FakeMember, run_night_pipeline, _init_state_for_role  # noqa: E402
from invariants import assert_post_night_pipeline_invariants  # noqa: E402
from game import Game  # noqa: E402
import config as bot_config  # noqa: E402


def make_game_with_roles(roles_by_seat: Dict[int, str]) -> tuple[Game, FakeGuild]:
    guild_id = 123
    n = max(roles_by_seat.keys())
    members = {i: FakeMember(i, f"P{i}") for i in range(1, n + 1)}
    guild = FakeGuild(guild_id, members)
    for m in members.values():
        m.guild = guild

    game = Game(guild_id)
    game.in_progress = True
    game.phase = "night"
    game.alive_role_id = None
    game.players = list(members.values())  # type: ignore[assignment]
    game.living_players = list(members.values())  # type: ignore[assignment]
    game.player_roles = dict(roles_by_seat)
    game.role_states = {i: _init_state_for_role(r) for i, r in roles_by_seat.items()}
    game.night_actions = {}
    game.graveyard = []
    return game, guild


def _role_universe() -> List[str]:
    # Prefer bot config as source of truth.
    return sorted(set(bot_config.TOWN_ROLES) | set(bot_config.ALL_MAFIA_ROLES) | {"Survivor", "Executioner", "Jester", "Witch", "Pirate", "Arsonist", "Chaos"})


ROLE_UNIVERSE = _role_universe()


action_type = st.sampled_from(
    [
        "heal",
        "roleblock",
        "investigate",
        "shoot",
        "watch",
        "track",
        "transport",
        "control",
        "douse",
        "ignite",
        "clean",
        "kill",
        "plunder",
        "frame",
        "protect",
        "guard",
        "hypnotize",
        "tailor",
        "hide",
        "chaos",
        "reanimate",
        "vest",
        "alert",
    ]
)


def _weird_target() -> st.SearchStrategy[Any]:
    # Include hashable/unhashable, valid/invalid.
    return st.one_of(
        st.integers(min_value=1, max_value=7),
        st.none(),
        st.text(min_size=0, max_size=5),
        st.lists(st.integers(min_value=1, max_value=7), min_size=0, max_size=2),
        st.dictionaries(st.text(min_size=1, max_size=3), st.integers(min_value=0, max_value=3), max_size=2),
    )


night_action_strategy = st.fixed_dictionaries(
    {
        "type": action_type,
        "actor": st.integers(min_value=1, max_value=7),
        "role": st.sampled_from(ROLE_UNIVERSE),
    },
    optional={
        "target": _weird_target(),
        "targets": st.lists(_weird_target(), min_size=0, max_size=2),
        "duel_won": st.booleans(),
        "fake_role": st.sampled_from(ROLE_UNIVERSE),
        "msg_type": st.sampled_from(["healed", "roleblocked", "transported", "controlled", "attacked"]),
    },
)


@settings(max_examples=500, deadline=None)
@given(
    roles=st.lists(st.sampled_from(ROLE_UNIVERSE), min_size=7, max_size=7),
    actions=st.dictionaries(st.integers(min_value=1, max_value=7), night_action_strategy, min_size=0, max_size=7),
)
def test_night_pipeline_never_throws(roles: List[str], actions: Dict[int, Dict[str, Any]]) -> None:
    # Ensure everyone has a role (duplicates allowed here; this is corruption/robustness focused).
    roles_by_seat = {i + 1: roles[i] for i in range(7)}
    game, guild = make_game_with_roles(roles_by_seat)

    # Install actions (may be malformed); the property is "never throw".
    # The sim harness expands `reanimate` actions and expects corpse metadata fields to exist.
    expanded: Dict[int, Dict[str, Any]] = {int(k): dict(v) for k, v in actions.items()}
    supported_corpse_roles = [
        "Doctor",
        "Sheriff",
        "Investigator",
        "Lookout",
        "Tracker",
        "Escort",
        "Transporter",
        "Bodyguard",
        "Vigilante",
    ]
    for _aid, act in expanded.items():
        if act.get("type") != "reanimate":
            continue
        if not isinstance(act.get("corpse_role"), str):
            act["corpse_role"] = random.choice(supported_corpse_roles)
        if not isinstance(act.get("corpse_player_id"), int):
            act["corpse_player_id"] = random.randint(1, 7)
    game.night_actions = expanded

    # Use a persistent event loop to avoid creating/destroying one per example.
    if not hasattr(test_night_pipeline_never_throws, "_loop"):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        setattr(test_night_pipeline_never_throws, "_loop", loop)
    loop = getattr(test_night_pipeline_never_throws, "_loop")

    async def _run() -> None:
        out = await run_night_pipeline(game, guild)  # should never raise
        assert_post_night_pipeline_invariants(game, out)

    loop.run_until_complete(_run())


def main() -> None:
    # Running via pytest is better, but this works standalone.
    test_night_pipeline_never_throws()
    print("property_test.py: OK")


if __name__ == "__main__":
    main()
