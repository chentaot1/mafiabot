from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import discord

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import night as night_engine  # noqa: E402


@dataclass
class FakeMember:
    id: int
    display_name: str
    inbox: List[str]

    async def send(self, content: str) -> None:
        # Collect DMs; mimic discord behavior (no exception).
        self.inbox.append(content)


class FakeGuild:
    def __init__(self, members: Dict[int, FakeMember]) -> None:
        self._members = members

    def get_member(self, user_id: int) -> Optional[FakeMember]:
        return self._members.get(user_id)


class FakeGame:
    def __init__(self) -> None:
        self.player_roles: Dict[int, str] = {}
        self.role_states: Dict[int, Dict] = {}
        self.night_actions: Dict[int, Dict] = {}
        self.doused_players: Set[int] = set()
        self.day_number: int = 1
        self._living_ids: Set[int] = set()
        self._members: Dict[int, FakeMember] = {}

    def add_player(self, pid: int, role: str) -> None:
        self.player_roles[pid] = role
        self._living_ids.add(pid)
        self._members[pid] = FakeMember(id=pid, display_name=f"P{pid}", inbox=[])

    async def get_member_safe(self, guild: FakeGuild, user_id: int) -> Optional[FakeMember]:
        return guild.get_member(user_id)

    async def sync_living_players(self, guild: FakeGuild) -> None:
        # No-op; living set is authoritative.
        return

    async def get_living_ids(self, guild: FakeGuild) -> List[int]:
        return sorted(self._living_ids)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


async def run_case_kill_vs_heal() -> None:
    g = FakeGame()
    g.add_player(1, "Mobster")
    g.add_player(2, "Doctor")
    g.add_player(3, "Sheriff")
    g.role_states[2] = {"self_heals_remaining": 1}
    g.night_actions = {
        1: {"type": "kill", "target": 3, "actor": 1},
        2: {"type": "heal", "target": 3, "actor": 2},
    }
    guild = FakeGuild(g._members)
    await night_engine.resolve_transports(g, guild)  # type: ignore[arg-type]
    await night_engine.resolve_control(g, guild)  # type: ignore[arg-type]
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    visit_log = {t: [v for v in vs if v not in blocked] for t, vs in visit_log_raw.items()}
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(3 not in deaths, "Doctor heal should prevent Mobster kill")


async def run_case_roleblock_stops_kill() -> None:
    g = FakeGame()
    g.add_player(1, "Mobster")
    g.add_player(2, "Escort")
    g.add_player(3, "Sheriff")
    g.night_actions = {
        1: {"type": "kill", "target": 3, "actor": 1},
        2: {"type": "roleblock", "target": 1, "actor": 2},
    }
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    _assert(1 in blocked, "Mobster should be roleblocked by Escort")
    visit_log = {t: [v for v in vs if v not in blocked] for t, vs in visit_log_raw.items()}
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(3 not in deaths, "Roleblocked Mobster should not kill")


async def run_case_transport_swaps_kill_target() -> None:
    g = FakeGame()
    g.add_player(1, "Mobster")
    g.add_player(2, "Transporter")
    g.add_player(3, "Sheriff")
    g.add_player(4, "Lookout")
    g.night_actions = {
        1: {"type": "kill", "target": 3, "actor": 1},
        2: {"type": "transport", "targets": [3, 4], "actor": 2},
    }
    guild = FakeGuild(g._members)
    await night_engine.resolve_transports(g, guild)  # type: ignore[arg-type]
    # after transport, kill target should be swapped to 4 (or 3 depending on implementation),
    # so we assert that the kill target is no longer 3.
    _assert(g.night_actions[1]["target"] != 3, "Transport should rewrite kill target away from original")


async def run_case_witch_controls_mafia_kill() -> None:
    g = FakeGame()
    g.add_player(1, "Mobster")
    g.add_player(2, "Witch")
    g.add_player(3, "Sheriff")
    g.add_player(4, "Lookout")
    g.role_states[2] = {"has_learned_role": False, "night1_shield_used": False}
    g.night_actions = {
        1: {"type": "kill", "target": 3, "actor": 1},
        2: {"type": "control", "targets": [1, 4], "actor": 2},
    }
    guild = FakeGuild(g._members)
    await night_engine.resolve_control(g, guild)  # type: ignore[arg-type]
    _assert(g.night_actions[1]["target"] == 4, "Witch control should redirect controlled actor target")


async def run_case_framer_makes_sheriff_suspicious() -> None:
    g = FakeGame()
    g.add_player(1, "Sheriff")
    g.add_player(2, "Framer")
    g.add_player(3, "Doctor")
    g.night_actions = {
        2: {"type": "frame", "target": 3, "actor": 2},
        1: {"type": "investigate", "target": 3, "role": "Sheriff", "actor": 1},
    }
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    _assert(g.role_states.get(3, {}).get("is_framed") is True, "Frame should set is_framed")
    await night_engine.resolve_investigative(g, blocked, visit_log_raw, guild)  # type: ignore[arg-type]
    sheriff_dm = "\n".join(g._members[1].inbox)
    _assert("suspicious" in sheriff_dm.lower(), "Framed target should read suspicious to Sheriff")


async def run_case_tailor_sets_fake_death_role() -> None:
    g = FakeGame()
    g.add_player(1, "Tailor")
    g.add_player(2, "Sheriff")
    g.role_states[1] = {"uses_remaining": 1}
    g.night_actions = {1: {"type": "tailor", "target": 2, "fake_role": "Mobster", "actor": 1}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    _assert(g.role_states.get(2, {}).get("is_tailored_as") == "Mobster", "Tailor should set is_tailored_as")


async def run_case_gravedigger_hides_role_on_death() -> None:
    g = FakeGame()
    g.add_player(1, "Gravedigger")
    g.add_player(2, "Sheriff")
    g.role_states[1] = {"uses_remaining": 1}
    g.night_actions = {1: {"type": "hide", "target": 2, "actor": 1}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    _assert(g.role_states.get(2, {}).get("is_hidden_by_gravedigger") is True, "Hide should set is_hidden_by_gravedigger")


async def run_case_gatekeeper_blocks_visitors() -> None:
    g = FakeGame()
    g.add_player(1, "Gatekeeper")
    g.add_player(2, "Sheriff")
    g.add_player(3, "Doctor")
    g.role_states[1] = {"uses_remaining": 2}
    g.night_actions = {
        1: {"type": "guard", "target": 2, "actor": 1},
        3: {"type": "heal", "target": 2, "actor": 3},
    }
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    _assert(3 in blocked, "Gatekeeper should block non-mafia visitors to guarded target")


async def run_case_alert_kills_visitors() -> None:
    g = FakeGame()
    g.add_player(1, "Scary Grandma")
    g.add_player(2, "Mobster")
    g.add_player(3, "Doctor")
    g.role_states[1] = {"alerts_remaining": 2, "is_on_alert": True}
    g.night_actions = {
        2: {"type": "kill", "target": 1, "actor": 2},
        3: {"type": "heal", "target": 1, "actor": 3},
    }
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    visit_log = {t: [v for v in vs if v not in blocked] for t, vs in visit_log_raw.items()}
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(2 in deaths, "Scary Grandma on alert should kill visiting attacker")


async def run_case_arsonist_douse_and_ignite() -> None:
    g = FakeGame()
    g.add_player(1, "Arsonist")
    g.add_player(2, "Sheriff")
    g.add_player(3, "Mobster")
    g.night_actions = {1: {"type": "douse", "target": 2, "actor": 1}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    _assert(2 in g.doused_players, "Douse should add target to doused_players")
    g.night_actions = {1: {"type": "ignite", "actor": 1}}
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log_raw, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(2 in deaths, "Ignite should kill doused targets")

async def run_case_arsonist_clean_removes_self_douse() -> None:
    g = FakeGame()
    g.add_player(1, "Arsonist")
    g.add_player(2, "Witch")
    g.add_player(3, "Sheriff")
    # Witch controls Arsonist to douse themselves (control target is arsonist).
    g.role_states[2] = {"has_learned_role": False, "night1_shield_used": False}
    g.night_actions = {
        1: {"type": "douse", "target": 3, "actor": 1},
        2: {"type": "control", "targets": [1, 1], "actor": 2},
    }
    guild = FakeGuild(g._members)
    await night_engine.resolve_control(g, guild)  # type: ignore[arg-type]
    # Control should redirect the douse target to self.
    _assert(g.night_actions[1]["target"] == 1, "Witch control should be able to self-target douse via redirect")
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    _assert(1 in g.doused_players, "Arsonist should be doused if redirected to self (edge case)")

    # Now clean should remove self from doused list.
    g.night_actions = {1: {"type": "clean", "actor": 1}}
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    _assert(1 not in g.doused_players, "Clean should remove Arsonist from doused_players")

async def run_case_witch_can_prevent_ignite() -> None:
    g = FakeGame()
    g.add_player(1, "Witch")
    g.add_player(2, "Arsonist")
    g.add_player(3, "Sheriff")
    g.role_states[1] = {"has_learned_role": False, "night1_shield_used": False}
    g.night_actions = {
        2: {"type": "ignite", "actor": 2},
        1: {"type": "control", "targets": [2, 3], "actor": 1},
    }
    guild = FakeGuild(g._members)
    await night_engine.resolve_control(g, guild)  # type: ignore[arg-type]
    _assert(g.night_actions[2]["type"] == "douse", "Witch control should convert Arsonist ignite into douse")
    _assert(g.night_actions[2]["target"] == 3, "Converted douse should target the Witch's forced target")


async def run_case_investigator_bucket_and_frame_override() -> None:
    g = FakeGame()
    g.add_player(1, "Investigator")
    g.add_player(2, "Framer")
    g.add_player(3, "Doctor")
    g.night_actions = {1: {"type": "investigate", "target": 3, "role": "Investigator", "actor": 1}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    await night_engine.resolve_investigative(g, blocked, visit_log_raw, guild)  # type: ignore[arg-type]
    msg = "\n".join(g._members[1].inbox)
    _assert("Doctor" in msg and "Bodyguard" in msg and "Survivor" in msg, "Investigator should return correct bucket for Doctor")
    _assert("Civilian" not in msg, "Investigator bucket should not mention removed Civilian role")

    # Now frame the target; apparent role should shift to Framer bucket.
    g._members[1].inbox.clear()
    g.night_actions = {
        2: {"type": "frame", "target": 3, "actor": 2},
        1: {"type": "investigate", "target": 3, "role": "Investigator", "actor": 1},
    }
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    await night_engine.resolve_investigative(g, blocked, visit_log_raw, guild)  # type: ignore[arg-type]
    msg2 = "\n".join(g._members[1].inbox)
    _assert("Framer" in msg2 and "Witch" in msg2, "Framed target should show Framer/Jester/Executioner/Witch bucket")


async def run_case_lookout_and_tracker() -> None:
    g = FakeGame()
    g.add_player(1, "Lookout")
    g.add_player(2, "Tracker")
    g.add_player(3, "Doctor")
    g.add_player(4, "Mobster")
    g.add_player(5, "Sheriff")
    g.night_actions = {
        1: {"type": "watch", "target": 5, "actor": 1},
        2: {"type": "track", "target": 4, "actor": 2},
        3: {"type": "heal", "target": 5, "actor": 3},
        4: {"type": "kill", "target": 5, "actor": 4},
    }
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    visit_log = {t: [v for v in vs if v not in blocked] for t, vs in visit_log_raw.items()}
    await night_engine.resolve_investigative(g, blocked, visit_log, guild)  # type: ignore[arg-type]
    lookout_msg = "\n".join(g._members[1].inbox)
    _assert("P3" in lookout_msg and "P4" in lookout_msg, "Lookout should see Doctor and Mobster visiting target")
    tracker_msg = "\n".join(g._members[2].inbox)
    _assert("P5" in tracker_msg, "Tracker should report Mobster visited target")


async def run_case_bodyguard_retal_and_retri_ret_protect_survives() -> None:
    # Normal Bodyguard: attacker dies, bodyguard dies.
    g = FakeGame()
    g.add_player(1, "Bodyguard")
    g.add_player(2, "Mobster")
    g.add_player(3, "Sheriff")
    g.role_states[1] = {"uses_remaining": 2}
    g.night_actions = {
        1: {"type": "protect", "target": 3, "actor": 1},
        2: {"type": "kill", "target": 3, "actor": 2},
    }
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    visit_log = {t: [v for v in vs if v not in blocked] for t, vs in visit_log_raw.items()}
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(2 in deaths, "Bodyguard should kill attacker")
    _assert(1 in deaths, "Bodyguard should die on guard (normal protect)")
    _assert(3 not in deaths, "Protected target should live")

    # Retributionist using BG corpse path: attacker dies, protector does NOT die.
    g2 = FakeGame()
    g2.add_player(10, "Retributionist")
    g2.add_player(11, "Mobster")
    g2.add_player(12, "Sheriff")
    g2.night_actions = {
        10: {"type": "ret_protect", "target": 12, "actor": 10},
        11: {"type": "kill", "target": 12, "actor": 11},
    }
    guild2 = FakeGuild(g2._members)
    visit_log_raw2 = night_engine.build_visit_log(g2)  # type: ignore[arg-type]
    blocked2 = night_engine.resolve_blocking(g2, visit_log_raw2)  # type: ignore[arg-type]
    visit_log2 = {t: [v for v in vs if v not in blocked2] for t, vs in visit_log_raw2.items()}
    healed_by2, protected_by2 = await night_engine.apply_misc_actions(g2, blocked2, guild2)  # type: ignore[arg-type]
    deaths2 = await night_engine.resolve_killing(g2, visit_log2, blocked2, healed_by2, protected_by2, guild2)  # type: ignore[arg-type]
    _assert(11 in deaths2, "ret_protect should still counter-kill attacker")
    _assert(10 not in deaths2, "ret_protect should not die on guard")
    _assert(12 not in deaths2, "Protected target should live (ret_protect)")


async def run_case_survivor_vest_blocks_kill() -> None:
    g = FakeGame()
    g.add_player(1, "Survivor")
    g.add_player(2, "Mobster")
    g.role_states[1] = {"vests_remaining": 1}
    g.night_actions = {1: {"type": "vest", "actor": 1}, 2: {"type": "kill", "target": 1, "actor": 2}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    visit_log = {t: [v for v in vs if v not in blocked] for t, vs in visit_log_raw.items()}
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(1 not in deaths, "Survivor vest should prevent normal kill")
    _assert(g.role_states[1].get("vests_remaining") == 0, "Vest should consume one vest")


async def run_case_hypnotist_fake_message() -> None:
    g = FakeGame()
    g.add_player(1, "Hypnotist")
    g.add_player(2, "Sheriff")
    g.night_actions = {1: {"type": "hypnotize", "target": 2, "msg_type": "roleblocked", "actor": 1}}
    guild = FakeGuild(g._members)
    await night_engine.send_night_feedback(g, blocked=[], guild=guild)  # type: ignore[arg-type]
    msg = "\n".join(g._members[2].inbox)
    _assert("roleblocked" in msg.lower(), "Hypnotist should send fake roleblocked message")


async def run_case_mole_reveals_role_and_consumes_use() -> None:
    g = FakeGame()
    g.add_player(1, "Mole")
    g.add_player(2, "Mobster")
    g.role_states[1] = {"uses_remaining": 1}
    g.night_actions = {1: {"type": "investigate", "target": 2, "role": "Mole", "actor": 1}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    await night_engine.resolve_investigative(g, blocked, visit_log_raw, guild)  # type: ignore[arg-type]
    msg = "\n".join(g._members[1].inbox)
    _assert("Mobster" in msg, "Mole should reveal exact role")
    _assert(g.role_states[1]["uses_remaining"] == 0, "Mole should consume a use")


async def run_case_vigilante_shoot_consumes_shot_and_sets_guilt_on_town() -> None:
    g = FakeGame()
    g.add_player(1, "Vigilante")
    g.add_player(2, "Sheriff")  # Town
    g.role_states[1] = {"shots_remaining": 1}
    g.night_actions = {1: {"type": "shoot", "target": 2, "actor": 1}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log_raw, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(2 in deaths, "Vigilante shoot should kill target (no defense)")
    _assert(g.role_states[1]["shots_remaining"] == 0, "Vigilante shot should be consumed")
    _assert(g.role_states[1].get("guilty_tomorrow") is True, "Vigilante should become guilty after shooting Town")


async def run_case_pirate_plunder_requires_duel_win_and_roleblocks_target() -> None:
    # If duel is not won, Pirate should not kill (but should still roleblock target via resolve_blocking).
    g = FakeGame()
    g.add_player(1, "Pirate")
    g.add_player(2, "Mobster")
    g.add_player(3, "Doctor")
    g.night_actions = {1: {"type": "plunder", "target": 2, "actor": 1, "duel_won": False}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    _assert(2 in blocked, "Pirate plunder should roleblock the target")
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log_raw, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(2 not in deaths, "Pirate should not kill if duel not won")

    # If duel is won, Pirate should kill target (unless defended).
    g2 = FakeGame()
    g2.add_player(10, "Pirate")
    g2.add_player(11, "Mobster")
    g2.night_actions = {10: {"type": "plunder", "target": 11, "actor": 10, "duel_won": True}}
    guild2 = FakeGuild(g2._members)
    visit_log_raw2 = night_engine.build_visit_log(g2)  # type: ignore[arg-type]
    blocked2 = night_engine.resolve_blocking(g2, visit_log_raw2)  # type: ignore[arg-type]
    healed_by2, protected_by2 = await night_engine.apply_misc_actions(g2, blocked2, guild2)  # type: ignore[arg-type]
    deaths2 = await night_engine.resolve_killing(g2, visit_log_raw2, blocked2, healed_by2, protected_by2, guild2)  # type: ignore[arg-type]
    _assert(11 in deaths2, "Pirate should kill if duel won")


async def run_case_bodyguard_vest_grants_defense() -> None:
    g = FakeGame()
    g.add_player(1, "Bodyguard")
    g.add_player(2, "Mobster")
    g.role_states[1] = {"self_protects_remaining": 1}
    g.night_actions = {1: {"type": "bg_vest", "actor": 1}, 2: {"type": "kill", "target": 1, "actor": 2}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log_raw, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(1 not in deaths, "Bodyguard vest should prevent normal kill (basic defense)")
    _assert(g.role_states[1].get("self_protects_remaining") == 0, "Bodyguard vest should consume self protect")


async def run_case_witch_night1_shield_blocks_first_kill() -> None:
    g = FakeGame()
    g.add_player(1, "Witch")
    g.add_player(2, "Mobster")
    g.role_states[1] = {"night1_shield_used": False, "has_learned_role": False}
    g.day_number = 1
    g.night_actions = {2: {"type": "kill", "target": 1, "actor": 2}}
    guild = FakeGuild(g._members)
    visit_log_raw = night_engine.build_visit_log(g)  # type: ignore[arg-type]
    blocked = night_engine.resolve_blocking(g, visit_log_raw)  # type: ignore[arg-type]
    healed_by, protected_by = await night_engine.apply_misc_actions(g, blocked, guild)  # type: ignore[arg-type]
    deaths = await night_engine.resolve_killing(g, visit_log_raw, blocked, healed_by, protected_by, guild)  # type: ignore[arg-type]
    _assert(1 not in deaths, "Witch night 1 shield should prevent first normal kill")
    _assert(g.role_states[1].get("night1_shield_used") is True, "Witch shield should be marked used")
    _assert(any("barrier" in m.lower() for m in g._members[1].inbox), "Witch should receive barrier DM on shield proc")


async def main() -> None:
    # Each case is designed to touch a distinct ability path.
    cases = [
        run_case_kill_vs_heal,
        run_case_roleblock_stops_kill,
        run_case_transport_swaps_kill_target,
        run_case_witch_controls_mafia_kill,
        run_case_framer_makes_sheriff_suspicious,
        run_case_tailor_sets_fake_death_role,
        run_case_gravedigger_hides_role_on_death,
        run_case_gatekeeper_blocks_visitors,
        run_case_alert_kills_visitors,
        run_case_arsonist_douse_and_ignite,
        run_case_arsonist_clean_removes_self_douse,
        run_case_witch_can_prevent_ignite,
        run_case_investigator_bucket_and_frame_override,
        run_case_lookout_and_tracker,
        run_case_bodyguard_retal_and_retri_ret_protect_survives,
        run_case_survivor_vest_blocks_kill,
        run_case_hypnotist_fake_message,
        run_case_mole_reveals_role_and_consumes_use,
        run_case_vigilante_shoot_consumes_shot_and_sets_guilt_on_town,
        run_case_pirate_plunder_requires_duel_win_and_roleblocks_target,
        run_case_bodyguard_vest_grants_defense,
        run_case_witch_night1_shield_blocks_first_kill,
    ]
    for c in cases:
        await c()
    print("ability_self_test.py: OK")


if __name__ == "__main__":
    asyncio.run(main())
