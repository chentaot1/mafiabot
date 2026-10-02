from __future__ import annotations


def test_witch_receives_controlled_investigation_result() -> None:
    import asyncio

    import game as game_module
    from engine.night import run_night_pipeline

    class _FakeMember:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.messages: list[str] = []

        async def send(self, msg: str) -> None:
            self.messages.append(str(msg))

    class _FakeGuild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch = _FakeMember(1)
    sheriff = _FakeMember(2)
    mobster = _FakeMember(3)
    guild = _FakeGuild([witch, sheriff, mobster])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, sheriff, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]

    g.player_roles = {1: "Witch", 2: "Sheriff", 3: "Mobster"}
    g.role_states = {1: {"has_learned_role": False, "night1_shield_used": False}, 2: {}, 3: {}}

    # Witch controls Sheriff to investigate Mobster.
    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 3]},
        2: {"type": "investigate", "actor": 2, "target": 1, "role": "Sheriff"},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    sheriff_msgs = "\n".join(sheriff.messages).lower()
    witch_msgs = "\n".join(witch.messages).lower()

    # Sheriff should have been redirected to a mafia target -> suspicious.
    assert "suspicious" in sheriff_msgs
    # ToS-like Witch rule: Witch receives the same investigative result DM.
    assert "suspicious" in witch_msgs


def test_witch_receives_controlled_investigator_bucket_result() -> None:
    import asyncio

    import game as game_module
    from engine.night import run_night_pipeline

    class _FakeMember:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.messages: list[str] = []

        async def send(self, msg: str) -> None:
            self.messages.append(str(msg))

    class _FakeGuild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch = _FakeMember(1)
    invest = _FakeMember(2)
    mobster = _FakeMember(3)
    guild = _FakeGuild([witch, invest, mobster])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, invest, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]

    g.player_roles = {1: "Witch", 2: "Investigator", 3: "Mobster"}
    g.role_states = {1: {"has_learned_role": False, "night1_shield_used": False}, 2: {}, 3: {}}

    # Witch controls Investigator to investigate Mobster.
    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 3]},
        2: {"type": "investigate", "actor": 2, "target": 1, "role": "Investigator"},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    invest_msgs = "\n".join(invest.messages).lower()
    witch_msgs = "\n".join(witch.messages).lower()

    # Both should receive the same bucket-style message format.
    assert "could be" in invest_msgs
    assert "could be" in witch_msgs


def test_witch_receives_controlled_lookout_visitors_message() -> None:
    import asyncio

    import game as game_module
    from engine.night import run_night_pipeline

    class _FakeMember:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.messages: list[str] = []

        async def send(self, msg: str) -> None:
            self.messages.append(str(msg))

    class _FakeGuild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch = _FakeMember(1)
    lookout = _FakeMember(2)
    mobster = _FakeMember(3)
    doc = _FakeMember(4)
    guild = _FakeGuild([witch, lookout, mobster, doc])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, lookout, mobster, doc]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]

    g.player_roles = {1: "Witch", 2: "Lookout", 3: "Mobster", 4: "Doctor"}
    g.role_states = {1: {"has_learned_role": False, "night1_shield_used": False}, 2: {}, 3: {}, 4: {"self_heals_remaining": 1}}

    # Doctor visits Mobster (so Lookout watching Mobster sees a visitor).
    # Witch forces Lookout to watch Mobster.
    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 3]},
        2: {"type": "watch", "actor": 2, "target": 1},
        4: {"type": "heal", "actor": 4, "target": 3},
        3: {"type": "kill", "actor": 3, "target": 4},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    lo_msgs = "\n".join(lookout.messages).lower()
    witch_msgs = "\n".join(witch.messages).lower()

    assert "visited your target" in lo_msgs or "nobody visited your target" in lo_msgs
    assert "visited your target" in witch_msgs or "nobody visited your target" in witch_msgs


def test_witch_does_not_receive_results_if_controlled_target_had_no_action() -> None:
    import asyncio

    import game as game_module
    from engine.night import run_night_pipeline

    class _FakeMember:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.messages: list[str] = []

        async def send(self, msg: str) -> None:
            self.messages.append(str(msg))

    class _FakeGuild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch = _FakeMember(1)
    sheriff = _FakeMember(2)
    mobster = _FakeMember(3)
    guild = _FakeGuild([witch, sheriff, mobster])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, sheriff, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]

    g.player_roles = {1: "Witch", 2: "Sheriff", 3: "Mobster"}
    g.role_states = {1: {"has_learned_role": False, "night1_shield_used": False}, 2: {}, 3: {}}

    # Witch controls Sheriff, but Sheriff submitted no investigative action.
    g.night_actions = {1: {"type": "control", "actor": 1, "targets": [2, 3]}}

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    witch_msgs = "\n".join(witch.messages).lower()
    # Witch always learns the role of controlled target.
    assert "learned the role" in witch_msgs
    # But there should be no mirrored investigative result.
    assert "suspicious" not in witch_msgs
    assert "innocent" not in witch_msgs
    assert "could be" not in witch_msgs
