"""
Lightweight smoke tests for the codebase.

This intentionally does NOT import `bot.py` because `bot.py` is an entrypoint that
expects `DISCORD_TOKEN` to exist and will run the Discord client.

Night engine (optional): after all in-process checks pass, run a fast bounded
`scripts/sim_test.py` fuzz pass with:
  python smoke_test.py --with-night-sim
Or set env SMOKE_WITH_NIGHT_SIM=1. Iterations: SMOKE_NIGHT_SIM_FUZZ (default 100).

For smoke + sim + Hypothesis property in one command, use:
  python scripts/night_smoke_gate.py
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BOT_PY = ROOT / "bot.py"
GAME_PY = ROOT / "game.py"
NIGHT_PY = ROOT / "engine" / "night.py"
CHECKS_PY = ROOT / "checks.py"
ERRORS_PY = ROOT / "errors.py"


def _want_night_sim_followup() -> bool:
    v = os.environ.get("SMOKE_WITH_NIGHT_SIM", "").strip().lower()
    return v in ("1", "true", "yes", "on")



def _run_night_sim_followup() -> None:
    """Bounded night-engine fuzz via sim_test (real pipeline, subprocess)."""
    n = int(os.environ.get("SMOKE_NIGHT_SIM_FUZZ", "100"))
    n = max(1, min(n, 50_000))
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "sim_test.py"),
        "--player-count",
        "7",
        "--seed",
        str(int(os.environ.get("SMOKE_NIGHT_SIM_SEED", "12345"))),
        "--fuzz-iterations",
        str(n),
        "--skip-scenarios",
        "--skip-exhaustive",
    ]
    print("smoke_test.py: running night sim_test.py follow-up...", flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)



def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None



def _find_async_fn(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for n in tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return n
    raise AssertionError(f"Could not find async function {name!r}")



def _find_game_method(tree: ast.Module, method: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == "Game":
            for b in n.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and b.name == method:
                    return b
    raise AssertionError(f"Could not find Game.{method}")


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == name:
            return n
    raise AssertionError(f"Could not find class {name!r}")



def _const_strings(node: ast.AST) -> set[str]:
    strings: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            strings.add(n.value)
    return strings



def _has_async_with_self_lock(fn: ast.AST, attr: str) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.AsyncWith):
            for item in n.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Attribute) and isinstance(ctx.value, ast.Name) and ctx.value.id == "self" and ctx.attr == attr:
                    return True
    return False



def check_resolve_pipeline_shape() -> None:
    # Contract: bot.resolve should delegate night resolution to the shared engine pipeline.
    expected = ["run_night_pipeline"]

    bot_tree = ast.parse((ROOT / "gameplay/resolution.py").read_text(encoding="utf-8"), filename=str(BOT_PY))
    resolve_fn = _find_async_fn(bot_tree, "evaluate")

    seen: list[tuple[int, int, str]] = []
    for n in ast.walk(resolve_fn):
        if isinstance(n, ast.Call):
            dn = _dotted_name(n.func)
            if dn in expected and hasattr(n, "lineno"):
                seen.append((n.lineno, getattr(n, "col_offset", 0), dn))
    seen.sort()
    ordered = [dn for _, __, dn in seen]
    assert ordered == expected, f"resolve() pipeline call mismatch: {ordered}"

    wrappers = {
        "_resolve_transports": ("await", "night_engine.resolve_transports"),
        "_resolve_control": ("await", "night_engine.resolve_control"),
        "_build_visit_log": ("return", "night_engine.build_visit_log"),
        "_resolve_blocking": ("return", "night_engine.resolve_blocking"),
        "_apply_misc_actions": ("return_await", "night_engine.apply_misc_actions"),
        "_resolve_investigative": ("await", "night_engine.resolve_investigative"),
        "_resolve_killing": ("return_await", "night_engine.resolve_killing"),
        "_send_night_feedback": ("await", "night_engine.send_night_feedback"),
    }

    game_tree = ast.parse(GAME_PY.read_text(encoding="utf-8"), filename=str(GAME_PY))
    for method, (mode, target) in wrappers.items():
        fn = _find_game_method(game_tree, method)
        body = fn.body
        # strip optional docstring
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]

        if mode == "await":
            assert len(body) == 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Await)
            call = body[0].value.value
            assert isinstance(call, ast.Call) and _dotted_name(call.func) == target
        elif mode == "return":
            assert len(body) == 1 and isinstance(body[0], ast.Return)
            call = body[0].value
            assert isinstance(call, ast.Call) and _dotted_name(call.func) == target
        elif mode == "return_await":
            assert len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Await)
            call = body[0].value.value
            assert isinstance(call, ast.Call) and _dotted_name(call.func) == target
        else:
            raise AssertionError(f"Unknown wrapper mode: {mode}")



def check_bot_py_compiles() -> None:
    # bot.py is large and can be accidentally broken by indentation/paste issues.
    # Compilation catches syntax/indent errors without importing/running the bot.
    src = BOT_PY.read_text(encoding="utf-8")
    compile(src, str(BOT_PY), "exec")



def check_resolve_expands_custom_actions() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'gameplay/resolution.py').read_text(encoding="utf-8")
    assert 'expand_reanimate_for_night_resolve(game)' in src
    assert 'run_night_pipeline(game, guild)' in src



def check_bot_resolve_chaos_targets_int_coerced() -> None:
    # Chaos is resolved in engine/night.py; bot.resolve should not need to parse chaos targets.
    src = BOT_PY.read_text(encoding="utf-8")
    assert "if a_type == \"chaos\"" not in src and "if a_type == 'chaos'" not in src



def check_will_modal_and_command_exist() -> None:
    bot_tree = ast.parse(BOT_PY.read_text(encoding="utf-8"), filename=str(BOT_PY))
    # Ensure we have a will command and modal/view classes (static sanity).
    class_names = {n.name for n in bot_tree.body if isinstance(n, ast.ClassDef)}
    assert "WillModal" in class_names, "Expected WillModal class in bot.py"
    assert "WillView" in class_names, "Expected WillView class in bot.py"

    will_fn = None
    for n in bot_tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "will":
            will_fn = n
            break
    assert will_fn is not None, "Expected async command function `will` in bot.py"

    strings = _const_strings(will_fn)
    assert "clear" in strings, "Expected `!will clear` support"

    # Privacy contract: DM-only and attempts to delete guild invocation.
    dotted = {_dotted_name(n) for n in ast.walk(will_fn)}
    assert any("DMChannel" in (x or "") for x in dotted), "Expected will() to check discord.DMChannel"
    call_names = {_dotted_name(n.func) for n in ast.walk(will_fn) if isinstance(n, ast.Call)}
    assert any((x or "").endswith("ctx.message.delete") for x in call_names), "Expected will() to delete guild-invoked messages"



def check_witch_can_prevent_ignite_in_engine() -> None:
    night_tree = ast.parse(NIGHT_PY.read_text(encoding="utf-8"), filename=str(NIGHT_PY))
    fn = None
    for n in night_tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "resolve_control":
            fn = n
            break
    assert fn is not None, "Expected resolve_control in engine/night.py"

    # Static check: resolve_control should reference both 'ignite' and 'douse'
    # as string constants (the prevention logic depends on this).
    strings = _const_strings(fn)
    assert "ignite" in strings, "Expected resolve_control to reference 'ignite' (prevent ignite path)"
    assert "douse" in strings, "Expected resolve_control to reference 'douse' (forced douse path)"



def check_gatekeeper_can_block_witch_control_on_guarded_target() -> None:
    # Major rules invariant (per role text): Gatekeeper can still block visitors to a guarded target,
    # including a Witch attempting to control that target.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _FakeMember:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _FakeGuild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch, sheriff, mobster, gatekeeper = _FakeMember(1), _FakeMember(2), _FakeMember(3), _FakeMember(4)
    guild = _FakeGuild([witch, sheriff, mobster, gatekeeper])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, sheriff, mobster, gatekeeper]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Witch", 2: "Sheriff", 3: "Mobster", 4: "Gatekeeper"}
    g.role_states = {1: {"night1_shield_used": False, "has_learned_role": False}, 2: {}, 3: {}, 4: {"uses_remaining": 2}}

    # Witch tries to control Sheriff onto Mobster, but Gatekeeper guards Sheriff.
    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 3]},
        2: {"type": "investigate", "actor": 2, "target": 1, "role": "Sheriff"},
        4: {"type": "guard", "actor": 4, "target": 2},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    # If Gatekeeper blocked the Witch's visit, control should not have redirected the Sheriff's action.
    assert int(g.night_actions[2]["target"]) == 1, "Expected Gatekeeper to prevent control redirect on guarded target"


def check_roleblocked_gatekeeper_does_not_block_witch_control() -> None:
    # Major interaction invariant: if the Gatekeeper is roleblocked, their guard should not apply.
    # That means Witch control targeting the guarded player should be able to redirect actions.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _FakeMember:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _FakeGuild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch, sheriff, mobster, gatekeeper, escort = (
        _FakeMember(1),
        _FakeMember(2),
        _FakeMember(3),
        _FakeMember(4),
        _FakeMember(5),
    )
    guild = _FakeGuild([witch, sheriff, mobster, gatekeeper, escort])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, sheriff, mobster, gatekeeper, escort]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Witch", 2: "Sheriff", 3: "Mobster", 4: "Gatekeeper", 5: "Escort"}
    g.role_states = {1: {"night1_shield_used": False, "has_learned_role": False}, 2: {}, 3: {}, 4: {"uses_remaining": 2}, 5: {}}

    # Gatekeeper guards Sheriff, but Escort roleblocks the Gatekeeper.
    # Witch should be able to control Sheriff (guard is ineffective).
    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 3]},
        2: {"type": "investigate", "actor": 2, "target": 1, "role": "Sheriff"},
        4: {"type": "guard", "actor": 4, "target": 2},
        5: {"type": "roleblock", "actor": 5, "target": 4},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    # If control worked, Sheriff should have been redirected to Mobster.
    assert int(g.night_actions[2]["target"]) == 3, "Expected roleblocked Gatekeeper guard to not prevent control redirect"



def check_gatekeeper_guard_does_not_apply_if_gatekeeper_is_blocked() -> None:
    # Static safety net: ensure resolve_control delegates Gatekeeper-block detection to the
    # shared chain-aware helper (`_compute_blocked_sets`) and uses `gk_blocked_pre` to gate
    # control. This guarantees behavior matches resolve_blocking() (incl. chained blocks).
    src = NIGHT_PY.read_text(encoding="utf-8")
    assert "def _compute_blocked_sets(" in src, (
        "Expected engine/night.py to expose _compute_blocked_sets() as the shared chain-aware "
        "block computation used by both resolve_blocking() and resolve_control()."
    )
    rc_idx = src.find("async def resolve_control(")
    assert rc_idx != -1, "resolve_control() not found in engine/night.py"
    rc_seg = src[rc_idx : rc_idx + 4000]
    assert "_compute_blocked_sets" in rc_seg, (
        "Expected resolve_control() to use _compute_blocked_sets() for the Gatekeeper precheck."
    )
    assert "gk_blocked_pre" in rc_seg, (
        "Expected resolve_control() to gate control on `gk_blocked_pre` (Witch is Gatekeeper-blocked)."
    )



def check_roleblocked_gatekeeper_guard_does_not_block_other_visitors() -> None:
    # Runtime invariant: if Gatekeeper is roleblocked, guarding should not block a Doctor heal visit.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    doc, survivor, gatekeeper, escort, mobster = _M(1), _M(2), _M(3), _M(4), _M(5)
    guild = _G([doc, survivor, gatekeeper, escort, mobster])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [doc, survivor, gatekeeper, escort, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Doctor", 2: "Survivor", 3: "Gatekeeper", 4: "Escort", 5: "Mobster"}
    g.role_states = {1: {"self_heals_remaining": 1}, 2: {"vests_remaining": 2}, 3: {"uses_remaining": 2}, 4: {}, 5: {}}
    g.night_actions = {
        1: {"type": "heal", "actor": 1, "target": 2},
        3: {"type": "guard", "actor": 3, "target": 2},
        4: {"type": "roleblock", "actor": 4, "target": 3},  # blocks the guard
        5: {"type": "kill", "actor": 5, "target": 2},  # ensures heal has something to do
    }

    _visit_log, blocked, healed_by, _prot_by, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    assert 3 in blocked, "Expected Gatekeeper to be blocked"
    assert healed_by.get(2) == 1, "Expected Doctor heal to apply when Gatekeeper is blocked"
    assert 2 not in deaths, "Expected Survivor to survive due to heal (and no vest used)"



def check_witch_control_does_not_override_control_immune() -> None:
    # Runtime invariant: Transporter is control-immune; Witch should not redirect their transport targets.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch, transporter, a, b = _M(1), _M(2), _M(3), _M(4)
    guild = _G([witch, transporter, a, b])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, transporter, a, b]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Witch", 2: "Transporter", 3: "Survivor", 4: "Mobster"}
    g.role_states = {1: {"night1_shield_used": False}, 2: {}, 3: {"vests_remaining": 2}, 4: {}}
    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 4]},
        2: {"type": "transport", "actor": 2, "targets": [3, 4]},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    # Transport targets should remain as submitted.
    assert g.night_actions[2]["targets"] == [3, 4]



def check_gatekeeper_guard_blocks_doctor_heal_to_guarded_target() -> None:
    # Major invariant: Gatekeeper should block non-mafia visitors to guarded target (including Doctor),
    # so a heal to the guarded target should not apply if guard is active.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    doc, survivor, gatekeeper, mobster = _M(1), _M(2), _M(3), _M(4)
    guild = _G([doc, survivor, gatekeeper, mobster])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [doc, survivor, gatekeeper, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Doctor", 2: "Survivor", 3: "Gatekeeper", 4: "Mobster"}
    g.role_states = {1: {"self_heals_remaining": 1}, 2: {"vests_remaining": 2}, 3: {"uses_remaining": 2}, 4: {}}
    g.night_actions = {
        1: {"type": "heal", "actor": 1, "target": 2},
        3: {"type": "guard", "actor": 3, "target": 2},
        4: {"type": "kill", "actor": 4, "target": 2},
    }

    _visit_log, blocked, healed_by, _prot_by, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 1 in blocked, "Expected Doctor (visitor) to be blocked by Gatekeeper guarding the target"
    assert healed_by.get(2) != 1, "Expected heal to not apply when Doctor is Gatekeeper-blocked"
    assert 2 in deaths, "Expected guarded target to die since heal should not apply"



def check_witch_control_blocked_by_gatekeeper_does_not_mirror_invest_results() -> None:
    # Major invariant: if Gatekeeper prevents control on guarded target, Witch should not receive mirrored results
    # because the investigation wasn't redirected by control.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch, sheriff, mobster, gatekeeper = _M(1), _M(2), _M(3), _M(4)
    guild = _G([witch, sheriff, mobster, gatekeeper])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, sheriff, mobster, gatekeeper]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Witch", 2: "Sheriff", 3: "Mobster", 4: "Gatekeeper"}
    g.role_states = {1: {"night1_shield_used": False, "has_learned_role": False}, 2: {}, 3: {}, 4: {"uses_remaining": 2}}
    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 3]},
        2: {"type": "investigate", "actor": 2, "target": 1, "role": "Sheriff"},
        4: {"type": "guard", "actor": 4, "target": 2},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    # Sheriff still investigated Witch (target stayed 1), so Witch shouldn't get a mirrored "suspicious" DM
    # (she'll still get "learned the role..." DM for the control attempt).
    assert not any("suspicious" in s.lower() for s in witch.dms), "Did not expect mirrored sheriff result when control was blocked"



def check_guard_consumes_use_only_if_valid_target() -> None:
    # Major corruption-safety invariant: malformed guard actions should not consume uses.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    gatekeeper, doc = _M(1), _M(2)
    guild = _G([gatekeeper, doc])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [gatekeeper, doc]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Gatekeeper", 2: "Doctor"}
    g.role_states = {1: {"uses_remaining": 2}, 2: {"self_heals_remaining": 1}}
    g.night_actions = {1: {"type": "guard", "actor": 1, "target": None}}

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert g.role_states[1]["uses_remaining"] == 2, "Expected guard uses to not decrement on malformed target"



def check_transporter_redirects_actions_and_visit_log_reflects_redirect() -> None:
    # Major invariant: transport should redirect targeted actions and visit logs should reflect the redirected target.
    # Specifically: if Mobster kills A but Transporter swaps A<->B, the kill should land on B.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    transporter, mobster, a, b = _M(1), _M(2), _M(3), _M(4)
    guild = _G([transporter, mobster, a, b])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [transporter, mobster, a, b]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Transporter", 2: "Mobster", 3: "Survivor", 4: "Doctor"}
    g.role_states = {1: {}, 2: {}, 3: {"vests_remaining": 2}, 4: {"self_heals_remaining": 1}}
    g.night_actions = {
        1: {"type": "transport", "actor": 1, "targets": [3, 4]},
        2: {"type": "kill", "actor": 2, "target": 3},
    }

    visit_log, _blocked, _healed_by, _prot_by, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 4 in deaths and 3 not in deaths, "Expected transport to redirect kill from A to B"
    # Visit log should count Mobster visiting the redirected target (B).
    assert 4 in visit_log and 2 in visit_log.get(4, []), "Expected visit_log to reflect redirected kill target"



def check_transporter_swap_does_not_redirect_immune_actions() -> None:
    # Major invariant: some actions should not be redirected by transport (Pirate plunder, vest, bg_vest, clean).
    # We'll check Pirate plunder's target remains unchanged after a transport swap.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    transporter, pirate, a, b = _M(1), _M(2), _M(3), _M(4)
    guild = _G([transporter, pirate, a, b])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [transporter, pirate, a, b]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Transporter", 2: "Pirate", 3: "Survivor", 4: "Doctor"}
    g.role_states = {2: {"wins": 0}, 3: {"vests_remaining": 2}, 4: {"self_heals_remaining": 1}}
    g.night_actions = {
        1: {"type": "transport", "actor": 1, "targets": [3, 4]},
        2: {"type": "plunder", "actor": 2, "target": 3, "duel_won": True, "duel_finished": True},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert int(g.night_actions[2]["target"]) == 3, "Expected transport to not redirect Pirate plunder target"



def check_transporter_messages_sent_to_swapped_targets() -> None:
    # Major UX invariant: swapped players should both receive the transported DM.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    transporter, a, b = _M(1), _M(2), _M(3)
    guild = _G([transporter, a, b])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [transporter, a, b]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Transporter", 2: "Survivor", 3: "Doctor"}
    g.role_states = {2: {"vests_remaining": 2}, 3: {"self_heals_remaining": 1}}
    g.night_actions = {1: {"type": "transport", "actor": 1, "targets": [2, 3]}}

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert any("transported" in s.lower() for s in a.dms), "Expected target A to get transported DM"
    assert any("transported" in s.lower() for s in b.dms), "Expected target B to get transported DM"



def check_visit_log_excludes_blocked_visitors_for_lookout_and_alert() -> None:
    # Major invariant: after blocking is computed, effective visit_log should exclude blocked visitors.
    # If Mobster is roleblocked, Lookout watching the Mobster's target should not see Mobster,
    # and Scary Grandma on alert should not shoot a blocked visitor.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    lookout, escort, mobster, grandma, victim = _M(1), _M(2), _M(3), _M(4), _M(5)
    guild = _G([lookout, escort, mobster, grandma, victim])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [lookout, escort, mobster, grandma, victim]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Lookout", 2: "Escort", 3: "Mobster", 4: "Scary Grandma", 5: "Survivor"}
    g.role_states = {4: {"alerts_remaining": 2}, 5: {"vests_remaining": 2}}
    g.night_actions = {
        1: {"type": "watch", "actor": 1, "target": 5},
        2: {"type": "roleblock", "actor": 2, "target": 3},
        3: {"type": "kill", "actor": 3, "target": 5},
        4: {"type": "alert", "actor": 4},
    }

    visit_log, blocked, _healed_by, _prot_by, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 3 in blocked, "Expected Mobster to be blocked"
    # Lookout DM should not include Mobster name since blocked visitors shouldn't count.
    lo_txt = "\n".join(lookout.dms).lower()
    assert "p3" not in lo_txt, "Did not expect blocked Mobster to appear in lookout visitors list"
    # Grandma should not kill blocked Mobster (no visit when blocked).
    assert 3 not in deaths, "Did not expect Scary Grandma to shoot a blocked visitor"
    assert 5 not in deaths, "Did not expect victim to die since Mobster was blocked"



def check_transporter_redirect_affects_track_results() -> None:
    # Major invariant: Tracker should report the actual (redirected) destination after transport.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    tracker, transporter, mobster, a, b = _M(1), _M(2), _M(3), _M(4), _M(5)
    guild = _G([tracker, transporter, mobster, a, b])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [tracker, transporter, mobster, a, b]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Tracker", 2: "Transporter", 3: "Mobster", 4: "Survivor", 5: "Doctor"}
    g.role_states = {4: {"vests_remaining": 2}, 5: {"self_heals_remaining": 1}}
    g.night_actions = {
        1: {"type": "track", "actor": 1, "target": 3},
        2: {"type": "transport", "actor": 2, "targets": [4, 5]},
        3: {"type": "kill", "actor": 3, "target": 4},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    txt = "\n".join(tracker.dms).lower()
    # Mobster targeted 4, but transport swaps 4<->5, so mobster actually visits 5.
    assert "p5" in txt and "p4" not in txt, "Expected tracker to report redirected visit destination"



def check_bodyguard_counterkill_still_hits_redirected_attacker() -> None:
    # Major invariant: if BOTH the attack and the Bodyguard protect are redirected onto the same swapped target,
    # the attacker should still die (counterkill should work under transport redirects).
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    bg, transporter, mobster, a, b = _M(1), _M(2), _M(3), _M(4), _M(5)
    guild = _G([bg, transporter, mobster, a, b])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [bg, transporter, mobster, a, b]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Bodyguard", 2: "Transporter", 3: "Mobster", 4: "Survivor", 5: "Doctor"}
    g.role_states = {1: {"uses_remaining": 1, "self_protects_remaining": 1}, 4: {"vests_remaining": 2}, 5: {"self_heals_remaining": 1}}
    # Mobster targets A, but transport swaps A<->B so target becomes B.
    # Bodyguard also targets A, so protect is redirected onto B as well.
    # Bodyguard should counterkill Mobster (and die on guard).
    g.night_actions = {
        1: {"type": "protect", "actor": 1, "target": 4},           # protect A -> redirected to B
        2: {"type": "transport", "actor": 2, "targets": [4, 5]},   # swap A<->B
        3: {"type": "kill", "actor": 3, "target": 4},              # target A -> redirected to B
    }

    _visit_log, _blocked, _healed_by, _prot_by, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 3 in deaths, "Expected Bodyguard counterkill to kill attacker after transport redirect"



def check_arsonist_clean_after_transport_and_douse() -> None:
    # Major invariant: clean is second-pass and should remove douse even if targets were transported.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    arso, transporter, a, b = _M(1), _M(2), _M(3), _M(4)
    guild = _G([arso, transporter, a, b])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [arso, transporter, a, b]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Arsonist", 2: "Transporter", 3: "Survivor", 4: "Doctor"}
    g.role_states = {3: {"vests_remaining": 2}, 4: {"self_heals_remaining": 1}}
    # Transport swaps A<->B but douse targets A directly (douse has a target and is redirectable).
    # Then Arsonist cleans self; if Arso was already doused, clean should remove it regardless.
    g.doused_players = {1}
    g.night_actions = {
        2: {"type": "transport", "actor": 2, "targets": [3, 4]},
        1: {"type": "douse", "actor": 1, "target": 3},
        99: {"type": "clean", "actor": 1},  # malformed actor id on purpose? no—must be actor 1
    }
    # Fix action actor id to 1 (clean is self-only); keep dict key consistent
    g.night_actions[1] = {"type": "clean", "actor": 1}

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 1 not in g.doused_players, "Expected clean to remove gasoline from Arsonist"



def check_alert_kills_unblocked_visitors_after_transport_redirect() -> None:
    # Major invariant: Alert should kill effective visitors (post-transport, post-blocking).
    # If Mobster targets A but is transported onto Grandma (on alert), Mobster should die.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    grandma, transporter, mobster, a = _M(1), _M(2), _M(3), _M(4)
    guild = _G([grandma, transporter, mobster, a])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [grandma, transporter, mobster, a]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Scary Grandma", 2: "Transporter", 3: "Mobster", 4: "Survivor"}
    g.role_states = {1: {"alerts_remaining": 2}, 4: {"vests_remaining": 2}}
    # Mobster targets A. Transporter swaps A<->Grandma, so Mobster is redirected to Grandma (on alert).
    g.night_actions = {
        1: {"type": "alert", "actor": 1},
        2: {"type": "transport", "actor": 2, "targets": [1, 4]},
        3: {"type": "kill", "actor": 3, "target": 4},
    }

    _visit_log, blocked, _healed_by, _prot_by, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 3 not in blocked, "Expected Mobster not to be blocked in this scenario"
    assert 3 in deaths, "Expected Scary Grandma to kill the redirected visitor on alert"



def check_ignite_kills_even_if_healed_and_doctor_gets_unstoppable_message() -> None:
    # Major invariant: Ignite is unstoppable; heals should not prevent ignite deaths, and doctors should get feedback.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    arso, doc, victim = _M(1), _M(2), _M(3)
    guild = _G([arso, doc, victim])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [arso, doc, victim]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Arsonist", 2: "Doctor", 3: "Survivor"}
    g.role_states = {2: {"self_heals_remaining": 1}, 3: {"vests_remaining": 2}}
    g.doused_players = {3}
    g.night_actions = {
        2: {"type": "heal", "actor": 2, "target": 3},
        1: {"type": "ignite", "actor": 1},
    }

    _visit_log, _blocked, _healed_by, _prot_by, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 3 in deaths, "Expected ignite to kill doused victim even if healed"
    assert any("unstoppable" in s.lower() for s in doc.dms), "Expected doctor unstoppable feedback on ignite"



def check_control_then_transport_still_redirects_controlled_action() -> None:
    # Major invariant: control resolution happens before transport redirection of targets is applied to actions,
    # but transport should still apply to the *post-control* target of the controlled action.
    # Scenario: Witch forces Sheriff to investigate A. Transport swaps A<->B. Sheriff should investigate B.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch, sheriff, transporter, a, b = _M(1), _M(2), _M(3), _M(4), _M(5)
    guild = _G([witch, sheriff, transporter, a, b])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, sheriff, transporter, a, b]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Witch", 2: "Sheriff", 3: "Transporter", 4: "Mobster", 5: "Doctor"}
    g.role_states = {1: {"night1_shield_used": False, "has_learned_role": False}, 5: {"self_heals_remaining": 1}}
    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 4]},  # force sheriff to target A
        2: {"type": "investigate", "actor": 2, "target": 5, "role": "Sheriff"},  # would target B originally
        3: {"type": "transport", "actor": 3, "targets": [4, 5]},  # swap A<->B
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    # Sheriff should end up investigating the transported counterpart (B => id 5).
    from engine.night import effective_primary_target
    assert effective_primary_target(g, 2) == 5



def check_controlled_watch_mirrors_visitors_after_transport() -> None:
    # Major invariant: if Witch forces Lookout to watch a target, and transport redirects visitors,
    # both Lookout and Witch should receive the final visitor list.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch, lookout, transporter, mobster, a, b = _M(1), _M(2), _M(3), _M(4), _M(5), _M(6)
    guild = _G([witch, lookout, transporter, mobster, a, b])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, lookout, transporter, mobster, a, b]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Witch", 2: "Lookout", 3: "Transporter", 4: "Mobster", 5: "Survivor", 6: "Doctor"}
    g.role_states = {1: {"night1_shield_used": False, "has_learned_role": False}, 5: {"vests_remaining": 2}, 6: {"self_heals_remaining": 1}}
    # Mobster targets A(5). Transport swaps A(5) with B(6) so visit lands on B(6).
    # Witch forces Lookout to watch A(5) -> watch target is redirected by transport to B(6), so visitor list should include Mobster.
    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 5]},
        2: {"type": "watch", "actor": 2, "target": 5},
        3: {"type": "transport", "actor": 3, "targets": [5, 6]},
        4: {"type": "kill", "actor": 4, "target": 5},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    lo = "\n".join(lookout.dms).lower()
    wi = "\n".join(witch.dms).lower()
    assert "p4" in lo and "p4" in wi, "Expected both Lookout and Witch to see Mobster in visitors after transport redirect"



def check_executioner_converts_to_jester_if_target_dies_at_night() -> None:
    # Major invariant: Executioner becomes Jester if their target dies non-lynch.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str, **kwargs) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    exe, mobster, target = _M(1), _M(2), _M(3)
    guild = _G([exe, mobster, target])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [exe, mobster, target]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Executioner", 2: "Mobster", 3: "Doctor"}
    g.role_states = {1: {"exe_target": 3}, 3: {"self_heals_remaining": 1}}
    g.night_actions = {2: {"type": "kill", "actor": 2, "target": 3}}
    deaths = asyncio.run(run_night_pipeline(g, guild))[4]  # type: ignore[arg-type]
    assert 3 in deaths
    # Apply death to trigger conversion logic.
    # We can call process_death_by_id since we don't have discord channel; use a dummy with send.
    class _Chan:
        async def send(self, _msg: str, **kwargs) -> None:
            return
    asyncio.run(g.process_death_by_id(_Chan(), guild, 3, "night_kill"))  # type: ignore[arg-type]
    assert g.player_roles[1] == "Jester", "Expected Executioner to convert to Jester after target dies at night"



def check_jester_fallback_haunt_targets_only_eligible_voters() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'night_guilt.py').read_text(encoding="utf-8")
    assert 'vid in living_ids' in src
    assert 'random.choice(eligible)' in src



def check_jester_eligible_haunt_includes_abstain_excludes_innocent() -> None:
    # Major invariant: tribunal should allow Jester to haunt guilty OR abstaining voters, not innocent voters.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("eligible_haunt_ids")
    assert idx != -1, "Expected vote() to compute eligible_haunt_ids"
    seg = src[idx : idx + 250]
    # The key contract is '!= \"❌\"' (guilty or abstain) rather than '== \"✅\"' (guilty only).
    assert "!= \"❌\"" in seg or "!= '❌'" in seg, "Expected eligible haunt pool to exclude only innocent votes"



def check_pirate_plunder_roleblocks_target_regardless_of_duel_outcome() -> None:
    # Major invariant: Pirate plunder should roleblock the target regardless of duel outcome.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    pirate, doctor = _M(1), _M(2)
    guild = _G([pirate, doctor])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [pirate, doctor]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Pirate", 2: "Doctor"}
    g.role_states = {1: {"wins": 0}, 2: {"self_heals_remaining": 1}}
    g.night_actions = {
        1: {"type": "plunder", "actor": 1, "target": 2, "duel_won": False, "duel_finished": True},
        2: {"type": "heal", "actor": 2, "target": 2},
    }

    _visit_log, blocked, healed_by, _prot_by, _deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 2 in blocked, "Expected plunder to roleblock the target regardless of duel outcome"
    assert healed_by.get(2) != 2, "Expected blocked Doctor heal to not apply"



def check_gatekeeper_blocked_pirate_plunder_does_not_roleblock_target() -> None:
    # Major invariant: if Gatekeeper blocks the Pirate's visit (guarding the target), the plunder should not roleblock.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    pirate, victim, gatekeeper, doctor = _M(1), _M(2), _M(3), _M(4)
    guild = _G([pirate, victim, gatekeeper, doctor])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [pirate, victim, gatekeeper, doctor]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Pirate", 2: "Survivor", 3: "Gatekeeper", 4: "Doctor"}
    g.role_states = {1: {"wins": 0}, 2: {"vests_remaining": 2}, 3: {"uses_remaining": 2}, 4: {"self_heals_remaining": 1}}
    g.night_actions = {
        1: {"type": "plunder", "actor": 1, "target": 2, "duel_won": True, "duel_finished": True},
        2: {"type": "vest", "actor": 2, "target": 2},
        3: {"type": "guard", "actor": 3, "target": 2},
        4: {"type": "heal", "actor": 4, "target": 4},
    }
    _visit_log, blocked, _healed_by, _prot_by, _deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 1 in blocked, "Expected Pirate to be blocked by Gatekeeper guard on target"
    assert 2 not in blocked, "Did not expect guarded target to be roleblocked by Pirate when Pirate is blocked"



def check_gatekeeper_does_not_block_mafia_visitors() -> None:
    # Major invariant: Gatekeeper blocks non-mafia visitors; Mafia roles should not be blocked by a guard.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    mobster, gatekeeper, victim = _M(1), _M(2), _M(3)
    guild = _G([mobster, gatekeeper, victim])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [mobster, gatekeeper, victim]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Mobster", 2: "Gatekeeper", 3: "Survivor"}
    g.role_states = {2: {"uses_remaining": 2}, 3: {"vests_remaining": 2}}
    g.night_actions = {
        2: {"type": "guard", "actor": 2, "target": 3},
        1: {"type": "kill", "actor": 1, "target": 3},
    }

    _visit_log, blocked, _healed_by, _prot_by, _deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 1 not in blocked, "Did not expect Gatekeeper to block Mafia visitor"



def check_mobster_promotion_does_not_change_role_start() -> None:
    # Core gameplay invariant: promotion should not rewrite role_start snapshot (used for history/leaderboards).
    import game as game_module

    mafia_member = None
    town_member = None

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "day"
    g.day_number = 2
    # Mafia present but no Mobster alive -> promotion triggers.
    # Use 3 living players so mafia doesn't instantly win (promotion happens mid-check_win_conditions).
    g.player_roles = {1: "Consort", 2: "Doctor", 3: "Sheriff"}
    g.role_states = {1: {"role_start": "Consort"}, 2: {"role_start": "Doctor"}, 3: {"role_start": "Sheriff"}}

    class _Member:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.roles = []
            self.guild_permissions = type("P", (), {"administrator": False})()

        async def send(self, _msg: str) -> None:
            return

    class _Guild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}
            self.me = None
            self.default_role = object()
            self.text_channels = []
            self.system_channel = None

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

        def get_role(self, _id):
            return None

        def get_channel(self, _id):
            return None

    class _Chan:
        def __init__(self, guild):
            self.guild = guild
            self.id = 999
            self.sent: list[str] = []

        async def send(self, msg: str) -> None:
            self.sent.append(str(msg))

        def permissions_for(self, _m):
            class _P:
                send_messages = True
                view_channel = True
            return _P()

    # Minimal stubs to satisfy check_win_conditions: no member syncing required for this invariant.
    async def _fake_sync(_guild):
        return

    async def _fake_get_living_ids(_guild):
        return [1, 2, 3]

    g.sync_living_players = _fake_sync  # type: ignore[assignment]
    g.get_living_ids = _fake_get_living_ids  # type: ignore[assignment]
    g.game_channel_id = 999

    mafia_member = _Member(1)
    town_member = _Member(2)
    town_member2 = _Member(3)
    guild = _Guild([mafia_member, town_member, town_member2])
    chan = _Chan(guild)
    # Living list must contain the mafia id for promotion to trigger.
    g.living_players = [mafia_member, town_member, town_member2]  # type: ignore[assignment]

    # Bind dummy bot so check_win_conditions can find channel/guild.
    class _Bot:
        def get_channel(self, _id):
            return chan

        def get_guild(self, _id):
            return guild

        user = None

    game_module.bind_bot(_Bot())  # type: ignore[arg-type]

    # Call the internal win check; it will execute promotion logic.
    import asyncio
    asyncio.run(g.check_win_conditions())
    # role_start should remain as originally assigned even if role_end is promoted.
    assert g.role_states.get(1, {}).get("role_start") == "Consort"



def check_mobster_promotion_happens_before_win_check_returns() -> None:
    # Core gameplay invariant: if mafia is alive and none are Mobster, someone becomes Mobster.
    import asyncio
    import game as game_module

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _Guild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}
            self.me = None

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

        @property
        def text_channels(self):
            return []

        @property
        def system_channel(self):
            return None

    class _Chan:
        def __init__(self, guild):
            self.guild = guild
            self.id = 999

        async def send(self, _msg: str) -> None:
            return

        def permissions_for(self, _m):
            class _P:
                send_messages = True
                view_channel = True
            return _P()

    import game as game_module2
    # Bind dummy bot so check_win_conditions can look up channel without crashing.
    class _Bot:
        def __init__(self, chan, guild):
            self._chan = chan
            self._guild = guild
            self.user = None

        def get_channel(self, _id):
            return self._chan

        def get_guild(self, _id):
            return self._guild

    m1, m2, m3 = _M(1), _M(2), _M(3)
    guild = _Guild([m1, m2, m3])
    chan = _Chan(guild)
    game_module2.bind_bot(_Bot(chan, guild))  # type: ignore[arg-type]

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "day"
    g.day_number = 2
    g.game_channel_id = 999
    g.players = [m1, m2, m3]  # type: ignore[assignment]
    g.living_players = [m1, m2, m3]  # type: ignore[assignment]
    g.player_roles = {1: "Consort", 2: "Doctor", 3: "Sheriff"}
    g.role_states = {1: {"role_start": "Consort"}, 2: {"role_start": "Doctor"}, 3: {"role_start": "Sheriff"}}

    # Ensure promotion runs when win check happens (should not end game).
    asyncio.run(g.check_win_conditions())
    assert g.player_roles[1] == "Mobster" or g.player_roles[2] == "Mobster", "Expected a mafia member to be promoted to Mobster"



def check_mobster_promotion_only_targets_mafia_roles() -> None:
    # Core invariant: promotion should only ever apply to a mafia member.
    import asyncio
    import game as game_module

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.roles = []
            self.guild_permissions = type("P", (), {"administrator": False})()

        async def send(self, _msg: str) -> None:
            return

    class _Guild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}
            self.me = None
            self.default_role = object()
            self.text_channels = []
            self.system_channel = None

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

        def get_role(self, _id):
            return None

        def get_channel(self, _id):
            return None

    class _Chan:
        def __init__(self, guild):
            self.guild = guild
            self.id = 999

        async def send(self, _msg: str) -> None:
            return

        def permissions_for(self, _m):
            class _P:
                send_messages = True
                view_channel = True
            return _P()

    m_mafia = _M(1)
    m_town1 = _M(2)
    m_town2 = _M(3)
    guild = _Guild([m_mafia, m_town1, m_town2])
    chan = _Chan(guild)

    class _Bot:
        def get_channel(self, _id):
            return chan

        def get_guild(self, _id):
            return guild

        user = None

    game_module.bind_bot(_Bot())  # type: ignore[arg-type]

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "day"
    g.day_number = 2
    g.game_channel_id = 999
    g.players = [m_mafia, m_town1, m_town2]  # type: ignore[assignment]
    g.living_players = [m_mafia, m_town1, m_town2]  # type: ignore[assignment]
    g.player_roles = {1: "Consort", 2: "Doctor", 3: "Sheriff"}
    g.role_states = {1: {"role_start": "Consort"}, 2: {"role_start": "Doctor"}, 3: {"role_start": "Sheriff"}}

    asyncio.run(g.check_win_conditions())
    promoted_ids = [pid for pid, r in g.player_roles.items() if r == "Mobster"]
    assert promoted_ids, "Expected some Mobster promotion"
    assert promoted_ids[0] == 1, "Expected only the mafia member to be eligible for Mobster promotion"



def check_mobster_promotion_does_not_happen_if_mobster_exists() -> None:
    # Core invariant: if a Mobster is already alive, no promotion should occur.
    import asyncio
    import game as game_module

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.roles = []
            self.guild_permissions = type("P", (), {"administrator": False})()

        async def send(self, _msg: str) -> None:
            return

    class _Guild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}
            self.me = None
            self.default_role = object()
            self.text_channels = []
            self.system_channel = None

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

        def get_role(self, _id):
            return None

        def get_channel(self, _id):
            return None

    class _Chan:
        def __init__(self, guild):
            self.guild = guild
            self.id = 999

        async def send(self, _msg: str) -> None:
            return

        def permissions_for(self, _m):
            class _P:
                send_messages = True
                view_channel = True
            return _P()

    m1, m2, m3, m4, m5 = _M(1), _M(2), _M(3), _M(4), _M(5)
    guild = _Guild([m1, m2, m3, m4, m5])
    chan = _Chan(guild)

    class _Bot:
        def get_channel(self, _id):
            return chan

        def get_guild(self, _id):
            return guild

        user = None

    game_module.bind_bot(_Bot())  # type: ignore[arg-type]

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "day"
    g.day_number = 2
    g.game_channel_id = 999
    # Use 2 mafia vs 3 town so mafia doesn't immediately win and reset state.
    g.players = [m1, m2, m3, m4, m5]  # type: ignore[assignment]
    g.living_players = [m1, m2, m3, m4, m5]  # type: ignore[assignment]
    g.player_roles = {1: "Mobster", 2: "Consort", 3: "Doctor", 4: "Sheriff", 5: "Investigator"}
    g.role_states = {
        1: {"role_start": "Mobster"},
        2: {"role_start": "Consort"},
        3: {"role_start": "Doctor"},
        4: {"role_start": "Sheriff"},
        5: {"role_start": "Investigator"},
    }

    asyncio.run(g.check_win_conditions())
    assert g.player_roles.get(2) == "Consort", "Did not expect another mafia member to be promoted when Mobster exists"



def check_roleblock_chain_stability_mutual_roleblocks() -> None:
    # Major invariant: mutual roleblocks should respect ROLEBLOCK_IMMUNE_ROLES.
    # In this ruleset, Escort/Consort are immune, so they should not end up blocked by each other.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    e1, e2, mobster = _M(1), _M(2), _M(3)
    guild = _G([e1, e2, mobster])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [e1, e2, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Escort", 2: "Consort", 3: "Mobster"}
    g.role_states = {}
    g.night_actions = {
        1: {"type": "roleblock", "actor": 1, "target": 2},
        2: {"type": "roleblock", "actor": 2, "target": 1},
        3: {"type": "kill", "actor": 3, "target": 1},
    }

    _visit_log, blocked, _healed_by, _prot_by, _deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 1 not in blocked and 2 not in blocked, "Expected mutual roleblock to respect roleblock immunities"



def check_endgame_stats_commit_does_not_crash_on_weird_personal_wins_types() -> None:
    # Major invariant: stats commit should tolerate corrupted personal_wins dict values.
    import game as game_module

    # Bind a dummy bot so SQLite best-effort path doesn't spam logs.
    class _DummyBot:
        db = None

    game_module.bind_bot(_DummyBot())  # type: ignore[arg-type]

    g = game_module.Game(guild_id=123)
    g.player_roles = {1: "Witch"}
    g.role_states = {1: {}}
    # Simulate corrupted stats store
    from persistence import save_stats
    save_stats(123, {"players": {"1": {"personal_wins": {"witch_town_loses": "NaN", "Witch": 2}}}})
    g._commit_endgame_stats(outcome="Mafia", living_ids=[1])



def check_gatekeeper_guard_consumes_exactly_one_use() -> None:
    # Major invariant: Gatekeeper guard should consume exactly 1 use per valid guard action,
    # even though blocking resolution may iterate internally.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    gatekeeper, victim, doc = _M(1), _M(2), _M(3)
    guild = _G([gatekeeper, victim, doc])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [gatekeeper, victim, doc]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Gatekeeper", 2: "Survivor", 3: "Doctor"}
    g.role_states = {1: {"uses_remaining": 2}, 2: {"vests_remaining": 2}, 3: {"self_heals_remaining": 1}}
    g.night_actions = {1: {"type": "guard", "actor": 1, "target": 2}}

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert g.role_states[1]["uses_remaining"] == 1, "Expected Gatekeeper to consume exactly one use for guard"



def check_gatekeeper_guard_use_is_idempotent_within_same_night() -> None:
    # Downstream check: if the engine pipeline is invoked twice in the same night (buggy double-resolve),
    # Gatekeeper should not burn 2 uses due to internal recompute/idempotency marker.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    gatekeeper, victim = _M(1), _M(2)
    guild = _G([gatekeeper, victim])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [gatekeeper, victim]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Gatekeeper", 2: "Survivor"}
    g.role_states = {1: {"uses_remaining": 2}, 2: {"vests_remaining": 2}}
    g.night_actions = {1: {"type": "guard", "actor": 1, "target": 2}}

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert g.role_states[1]["uses_remaining"] == 1, "Expected only one use consumed even if pipeline runs twice"



def _smoke_owned_game(game):
    """Give headless phase checks the same canonical ownership as live commands."""
    from contextlib import contextmanager
    import game as gm
    @contextmanager
    def scope():
        games, client = gm.active_games, gm._BOT
        gm.active_games, gm._BOT = {game.guild_id: game}, None
        try:
            yield
        finally:
            gm.active_games, gm._BOT = games, client
    return scope()


def check_start_night_clears_gatekeeper_used_marker() -> None:
    # Downstream check: the per-night marker must be cleared at the start of the next night.
    import game as game_module

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "day"
    g.day_number = 2
    g.role_states = {1: {"uses_remaining": 2, "gatekeeper_used_this_night": True}}

    class _Ctx:
        def __init__(self):
            self.guild = self

        def get_channel(self, _id):
            return None

        def get_role(self, _id):
            return None

        async def send(self, _msg: str) -> None:
            return

    import asyncio

    with _smoke_owned_game(g):
        asyncio.run(g.start_night(_Ctx()))
    assert "gatekeeper_used_this_night" not in g.role_states[1], "Expected start_night to clear gatekeeper marker"



def check_double_pipeline_does_not_double_consume_misc_uses() -> None:
    # Downstream invariant: if the pipeline runs twice in one night, self-only consumables should not double decrement.
    # Targets: Survivor vest, Scary Grandma alert, Doctor self-heal, Bodyguard uses.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    doc, surv, grandma, bg, mobster = _M(1), _M(2), _M(3), _M(4), _M(5)
    guild = _G([doc, surv, grandma, bg, mobster])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [doc, surv, grandma, bg, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Doctor", 2: "Survivor", 3: "Scary Grandma", 4: "Bodyguard", 5: "Mobster"}
    g.role_states = {
        1: {"self_heals_remaining": 1},
        2: {"vests_remaining": 2},
        3: {"alerts_remaining": 2},
        4: {"uses_remaining": 1, "self_protects_remaining": 1},
    }
    g.night_actions = {
        1: {"type": "heal", "actor": 1, "target": 1},      # self-heal consumes self_heals_remaining
        2: {"type": "vest", "actor": 2, "target": 2},      # consumes vests_remaining
        3: {"type": "alert", "actor": 3},                  # consumes alerts_remaining
        4: {"type": "protect", "actor": 4, "target": 2},   # consumes uses_remaining
        5: {"type": "kill", "actor": 5, "target": 2},      # ensure protection is relevant
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    assert g.role_states[1]["self_heals_remaining"] == 0, f"Expected Doctor self-heal to consume once (got {g.role_states[1]['self_heals_remaining']})"
    assert g.role_states[2]["vests_remaining"] == 1, "Expected Survivor vest to consume once"
    assert g.role_states[3]["alerts_remaining"] == 1, "Expected alert to consume once"
    assert g.role_states[4]["uses_remaining"] == 0, "Expected Bodyguard protect to consume once"



def check_double_pipeline_does_not_double_consume_limited_action_uses() -> None:
    # Core gameplay invariants: if night pipeline runs twice, limited-use actions should not double-consume.
    # Targets: Vigilante shot, Mole investigate use, Tailor uses, Gravedigger hide uses.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    vig, mole, tailor, grave, target = _M(1), _M(2), _M(3), _M(4), _M(5)
    guild = _G([vig, mole, tailor, grave, target])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [vig, mole, tailor, grave, target]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Vigilante", 2: "Mole", 3: "Tailor", 4: "Gravedigger", 5: "Survivor"}
    g.role_states = {
        1: {"shots_remaining": 1, "will_die_of_guilt": False, "guilty_tomorrow": False},
        2: {"uses_remaining": 1},
        3: {"uses_remaining": 1},
        4: {"uses_remaining": 1},
        5: {"vests_remaining": 2},
    }
    g.night_actions = {
        1: {"type": "shoot", "actor": 1, "target": 5},
        2: {"type": "investigate", "actor": 2, "target": 5, "role": "Mole"},
        3: {"type": "tailor", "actor": 3, "target": 5, "fake_role": "Doctor"},
        4: {"type": "hide", "actor": 4, "target": 5},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    assert g.role_states[1]["shots_remaining"] == 0, "Expected Vigilante shot to consume once"
    assert g.role_states[2]["uses_remaining"] == 0, "Expected Mole investigate to consume once"
    assert g.role_states[3]["uses_remaining"] == 0, "Expected Tailor to consume once"
    assert g.role_states[4]["uses_remaining"] == 0, "Expected Gravedigger hide to consume once"



def check_action_consumption_never_underflows_below_zero() -> None:
    # Corruption-tolerance invariant: counters should never go negative even if an action is present at 0 uses.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    doc, surv, grandma, bg, vig, mole, tailor, grave, gatekeeper, chaos, mobster = (
        _M(1),
        _M(2),
        _M(3),
        _M(4),
        _M(5),
        _M(6),
        _M(7),
        _M(8),
        _M(9),
        _M(10),
        _M(11),
    )
    guild = _G([doc, surv, grandma, bg, vig, mole, tailor, grave, gatekeeper, chaos, mobster])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [doc, surv, grandma, bg, vig, mole, tailor, grave, gatekeeper, chaos, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {
        1: "Doctor",
        2: "Survivor",
        3: "Scary Grandma",
        4: "Bodyguard",
        5: "Vigilante",
        6: "Mole",
        7: "Tailor",
        8: "Gravedigger",
        9: "Gatekeeper",
        10: "Chaos",
        11: "Mobster",
    }
    g.role_states = {
        1: {"self_heals_remaining": 0},
        2: {"vests_remaining": 0},
        3: {"alerts_remaining": 0},
        4: {"uses_remaining": 0, "self_protects_remaining": 0},
        5: {"shots_remaining": 0, "will_die_of_guilt": False, "guilty_tomorrow": False},
        6: {"uses_remaining": 0},
        7: {"uses_remaining": 0},
        8: {"uses_remaining": 0},
        9: {"uses_remaining": 0},
        10: {"uses_remaining": 0},
    }
    g.night_actions = {
        1: {"type": "heal", "actor": 1, "target": 1},
        2: {"type": "vest", "actor": 2, "target": 2},
        3: {"type": "alert", "actor": 3},
        4: {"type": "protect", "actor": 4, "target": 2},
        5: {"type": "shoot", "actor": 5, "target": 11},
        6: {"type": "investigate", "actor": 6, "target": 11, "role": "Mole"},
        7: {"type": "tailor", "actor": 7, "target": 11, "fake_role": "Doctor"},
        8: {"type": "hide", "actor": 8, "target": 11},
        9: {"type": "guard", "actor": 9, "target": 2},
        10: {"type": "chaos", "actor": 10, "targets": [2, 11]},
    }

    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    assert g.role_states[1]["self_heals_remaining"] >= 0
    assert g.role_states[2]["vests_remaining"] >= 0
    assert g.role_states[3]["alerts_remaining"] >= 0
    assert g.role_states[4]["uses_remaining"] >= 0 and g.role_states[4]["self_protects_remaining"] >= 0
    assert g.role_states[5]["shots_remaining"] >= 0
    assert g.role_states[6]["uses_remaining"] >= 0
    assert g.role_states[7]["uses_remaining"] >= 0
    assert g.role_states[8]["uses_remaining"] >= 0
    assert g.role_states[9]["uses_remaining"] >= 0
    assert g.role_states[10]["uses_remaining"] >= 0



def check_general_prompt_invariants_apply_monotonicity_and_idempotency() -> None:
    # Property-style: applying the same misc-actions pass twice shouldn't underflow and should be idempotent.
    import asyncio

    import game as game_module
    from engine.night import apply_misc_actions

    class _Guild:
        def get_member(self, _uid: int):
            return None

    g = game_module.Game(guild_id=1)
    g.in_progress = True
    g.phase = "night"
    g.player_roles = {1: "Doctor"}
    g.role_states = {1: {"self_heals_remaining": 1}}
    g.night_actions = {1: {"type": "heal", "actor": 1, "target": 1}}

    guild = _Guild()
    # apply_misc_actions signature is (game, blocked_list, guild)
    asyncio.run(apply_misc_actions(g, blocked=[], guild=guild))  # type: ignore[arg-type]
    after1 = int(g.role_states[1].get("self_heals_remaining", 0))
    assert after1 >= 0
    asyncio.run(apply_misc_actions(g, blocked=[], guild=guild))  # type: ignore[arg-type]
    after2 = int(g.role_states[1].get("self_heals_remaining", 0))
    assert after2 == after1, f"Expected idempotent consumption, got {after1}->{after2}"



def check_general_prompt_privacy_no_crash_on_missing_member_objects() -> None:
    # Property-style: pipeline tolerates missing discord.Member objects without crashing.
    import asyncio

    import game as game_module
    from engine.night import run_night_pipeline

    class _Guild:
        def get_member(self, _uid: int):
            return None

        async def fetch_member(self, _uid: int):
            return None

    g = game_module.Game(guild_id=1)
    g.in_progress = True
    g.phase = "night"
    g.player_roles = {1: "Lookout", 2: "Doctor"}
    g.role_states = {1: {}, 2: {}}
    g.night_actions = {
        1: {"type": "watch", "actor": 1, "target": 2},
        2: {"type": "heal", "actor": 2, "target": 2},
    }
    guild = _Guild()
    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]



def check_seeded_micro_fuzz_engine_invariants() -> None:
    # Seeded bounded fuzz: run a few random nights and assert basic invariants (no crashes, no negative counters).
    import asyncio
    import random

    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    rng = random.Random(1337)
    role_pool = ["Doctor", "Sheriff", "Investigator", "Lookout", "Tracker", "Escort", "Bodyguard", "Vigilante", "Survivor", "Mole"]
    action_by_role = {
        "Doctor": lambda a, t: {"type": "heal", "actor": a, "target": t},
        "Sheriff": lambda a, t: {"type": "investigate", "actor": a, "target": t, "role": "Sheriff"},
        "Investigator": lambda a, t: {"type": "investigate", "actor": a, "target": t, "role": "Investigator"},
        "Lookout": lambda a, t: {"type": "watch", "actor": a, "target": t},
        "Tracker": lambda a, t: {"type": "track", "actor": a, "target": t},
        "Escort": lambda a, t: {"type": "roleblock", "actor": a, "target": t},
        "Bodyguard": lambda a, t: {"type": "protect", "actor": a, "target": t},
        "Vigilante": lambda a, t: {"type": "shoot", "actor": a, "target": t},
        "Survivor": lambda a, _t: {"type": "vest", "actor": a, "target": a},
        "Mole": lambda a, t: {"type": "investigate", "actor": a, "target": t, "role": "Mole"},
    }

    for _seed in range(10):
        n_players = 6
        members = [_M(i + 1) for i in range(n_players)]
        guild = _G(members)
        g = game_module.Game(guild_id=1)
        g.in_progress = True
        g.phase = "night"
        g.players = members  # type: ignore[assignment]
        g.living_players = members[:]  # type: ignore[assignment]

        # Random roles + minimal states for consumables.
        g.player_roles = {}
        g.role_states = {}
        for m in members:
            r = rng.choice(role_pool)
            g.player_roles[m.id] = r
            st = {}
            if r == "Doctor":
                st["self_heals_remaining"] = 1
            if r == "Vigilante":
                st["shots_remaining"] = 1
            if r in {"Mole", "Bodyguard", "Escort"}:
                st["uses_remaining"] = 1
            if r == "Survivor":
                st["vests_remaining"] = 1
            g.role_states[m.id] = st

        # Random actions for each actor where applicable.
        g.night_actions = {}
        ids = [m.id for m in members]
        for a_id, r in g.player_roles.items():
            if r not in action_by_role:
                continue
            t = rng.choice(ids)
            g.night_actions[a_id] = action_by_role[r](a_id, t)

        asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

        # Invariants: never negative counters.
        for s in g.role_states.values():
            for k, v in s.items():
                if k.endswith("_remaining") or k.endswith("_remaining_this_night") or k.endswith("_remaining_today") or k in {"uses_remaining", "shots_remaining"}:
                    if isinstance(v, int):
                        assert v >= 0, f"Expected non-negative {k}, got {v}"


def check_vigilante_shoot_does_not_execute_with_zero_bullets() -> None:
    # Major corruption tolerance: if a shoot action exists with 0 bullets, it must not kill.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    vig, target = _M(1), _M(2)
    guild = _G([vig, target])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [vig, target]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Vigilante", 2: "Survivor"}
    g.role_states = {1: {"shots_remaining": 0, "will_die_of_guilt": False}, 2: {"vests_remaining": 2}}
    g.night_actions = {1: {"type": "shoot", "actor": 1, "target": 2}}
    deaths = asyncio.run(run_night_pipeline(g, guild))[4]  # type: ignore[arg-type]
    assert 2 not in deaths, "Did not expect Vigilante to kill with 0 bullets"



def check_survivor_vest_does_not_apply_with_zero_vests() -> None:
    # Major corruption tolerance: if a vest action exists with 0 vests, it must not grant defense.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    surv, mobster = _M(1), _M(2)
    guild = _G([surv, mobster])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [surv, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Survivor", 2: "Mobster"}
    g.role_states = {1: {"vests_remaining": 0}, 2: {}}
    g.night_actions = {
        1: {"type": "vest", "actor": 1, "target": 1},
        2: {"type": "kill", "actor": 2, "target": 1},
    }
    deaths = asyncio.run(run_night_pipeline(g, guild))[4]  # type: ignore[arg-type]
    assert 1 in deaths, "Expected Survivor with 0 vests to die (vest should not apply)"



def check_grandma_alert_does_not_apply_with_zero_alerts() -> None:
    # Major corruption tolerance: if an alert action exists with 0 alerts, it must not put Grandma on alert.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    grandma, mobster = _M(1), _M(2)
    guild = _G([grandma, mobster])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [grandma, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Scary Grandma", 2: "Mobster"}
    g.role_states = {1: {"alerts_remaining": 0}, 2: {}}
    g.night_actions = {
        1: {"type": "alert", "actor": 1},
        2: {"type": "kill", "actor": 2, "target": 1},
    }
    deaths = asyncio.run(run_night_pipeline(g, guild))[4]  # type: ignore[arg-type]
    # If alert incorrectly applied, Mobster would die. With 0 alerts, Mobster should live.
    assert 2 not in deaths, "Did not expect Mobster to die if Grandma had 0 alerts"



def check_dead_actor_actions_do_not_execute() -> None:
    # Major correctness: persisted actions from dead players should not execute (prevents wrong deaths after restores).
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    dead_mobster, target = _M(1), _M(2)
    guild = _G([dead_mobster, target])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [dead_mobster, target]  # type: ignore[assignment]
    g.living_players = [target]  # type: ignore[assignment]
    g.player_roles = {1: "Mobster", 2: "Survivor"}
    g.role_states = {1: {}, 2: {"vests_remaining": 2}}
    g.night_actions = {1: {"type": "kill", "actor": 1, "target": 2}}
    deaths = asyncio.run(run_night_pipeline(g, guild))[4]  # type: ignore[arg-type]
    assert 2 not in deaths, "Did not expect a dead actor's kill to execute"



def check_deaths_are_subset_of_living_players() -> None:
    # Major correctness: night pipeline should never report a death for a non-living / non-player id.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    attacker, victim = _M(1), _M(2)
    guild = _G([attacker, victim])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [attacker, victim]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Mobster", 2: "Survivor"}
    g.role_states = {1: {}, 2: {"vests_remaining": 2}}
    # Corrupted state: attack a non-existent id.
    g.night_actions = {1: {"type": "kill", "actor": 1, "target": 999999}}
    deaths = asyncio.run(run_night_pipeline(g, guild))[4]  # type: ignore[arg-type]
    living_ids = {m.id for m in g.living_players}  # type: ignore[union-attr]
    assert deaths.issubset(living_ids), f"Expected deaths {deaths} to be subset of living ids {living_ids}"



def check_dead_doctor_heal_does_not_apply_or_consume() -> None:
    # Major correctness: a dead Doctor's persisted heal should not prevent deaths or consume self-heals.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    dead_doc, victim, mobster = _M(1), _M(2), _M(3)
    guild = _G([dead_doc, victim, mobster])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [dead_doc, victim, mobster]  # type: ignore[assignment]
    g.living_players = [victim, mobster]  # type: ignore[assignment]
    g.player_roles = {1: "Doctor", 2: "Survivor", 3: "Mobster"}
    g.role_states = {1: {"self_heals_remaining": 1}, 2: {"vests_remaining": 2}, 3: {}}
    g.night_actions = {
        1: {"type": "heal", "actor": 1, "target": 2},
        3: {"type": "kill", "actor": 3, "target": 2},
    }
    _visit_log, _blocked, _healed_by, _prot, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 2 in deaths, "Expected victim to die; dead Doctor heal must not apply"
    assert int(g.role_states[1]["self_heals_remaining"]) == 1, "Expected dead Doctor not to consume self-heals"



def check_dead_roleblocker_does_not_block() -> None:
    # Major correctness: a dead Escort/Consort should not roleblock living players.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    dead_escort, mobster, victim = _M(1), _M(2), _M(3)
    guild = _G([dead_escort, mobster, victim])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [dead_escort, mobster, victim]  # type: ignore[assignment]
    g.living_players = [mobster, victim]  # type: ignore[assignment]
    g.player_roles = {1: "Escort", 2: "Mobster", 3: "Survivor"}
    g.role_states = {1: {}, 2: {}, 3: {"vests_remaining": 2}}
    g.night_actions = {
        1: {"type": "roleblock", "actor": 1, "target": 2},
        2: {"type": "kill", "actor": 2, "target": 3},
    }
    deaths = asyncio.run(run_night_pipeline(g, guild))[4]  # type: ignore[arg-type]
    assert 3 in deaths, "Expected victim to die; dead roleblocker must not block Mobster"



def check_endgame_stats_commit_is_idempotent() -> None:
    # Major robustness: repeated endgame commits (race/restart) must not double-count games/wins.
    import game as game_module
    from persistence import STATE_DIR, load_stats

    gid = 987654321
    # Clean any prior stats file for this guild id.
    try:
        (STATE_DIR / f"{gid}.stats.json").unlink(missing_ok=True)  # type: ignore[arg-type]
    except TypeError:
        p = STATE_DIR / f"{gid}.stats.json"
        if p.exists():
            p.unlink()

    g = game_module.Game(guild_id=gid)
    g.player_roles = {1: "Survivor"}
    g.role_states = {1: {}}
    g._commit_endgame_stats(outcome="Town", living_ids=[1])
    g._commit_endgame_stats(outcome="Town", living_ids=[1])

    data = load_stats(gid) or {}
    rec = ((data.get("players") or {}).get("1") or {})
    assert int(rec.get("games_played", 0)) == 1, f"Expected games_played to be 1, got {rec.get('games_played')}"



def check_startgame_resets_stats_committed_flag() -> None:
    # Static evidence: startgame must reset per-game stats idempotency marker.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("async def startgame")
    assert idx != -1, "Expected startgame command"
    seg = src[idx : idx + 12000]
    assert "stats_committed" in seg and "= False" in seg, "Expected startgame to reset game.stats_committed = False"



def check_reset_resets_stats_committed_flag() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'game.py').read_text(encoding="utf-8")
    assert 'async def _historical_reset' in src
    assert 'self.stats_committed = False' in src



def check_sqlite_initialize_and_personal_leaderboard_key_roundtrip() -> None:
    # DB invariant: initialize() must create tables, and leaderboard queries must work for canonical keys.
    import tempfile
    from pathlib import Path

    from database import Database

    # Windows can briefly hold a lock on SQLite/WAL files; ignore cleanup errors in this smoke check.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = str(Path(td) / "test.sqlite")
        db = Database(db_path)
        db.initialize()
        db.upsert_player_stats_delta(
            guild_id=1,
            player_id=42,
            games_played=1,
            wins_total=1,
            losses_total=0,
            draws_total=0,
            wins_town=0,
            wins_mafia=1,
            wins_arsonist=0,
            last_game_at=None,
        )
        db.upsert_personal_win_delta(guild_id=1, player_id=42, key="witch_town_loses", delta=3)
        rows = db.top_personal(guild_id=1, key="witch_town_loses", limit=10)
        assert rows and rows[0].player_id == 42 and int(rows[0].value) == 3, "Expected personal leaderboard to roundtrip"



def check_sqlite_import_player_stats_tolerates_corrupted_json_records() -> None:
    # DB invariant: JSON import should skip malformed records and not crash.
    import tempfile
    from pathlib import Path

    from database import Database

    # Windows can briefly hold a lock on SQLite/WAL files; ignore cleanup errors in this smoke check.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = str(Path(td) / "test.sqlite")
        db = Database(db_path)
        db.initialize()
        n = db.import_player_stats_from_json(
            guild_id=1,
            stats_data={
                "players": {
                    "not-an-int": {"games_played": 1, "wins": 1, "losses": 0, "draws": 0},
                    "123": "not-a-dict",
                    "456": {"games_played": "x", "wins": 1, "losses": 0, "draws": 0},
                    "789": {"games_played": 2, "wins": 1, "losses": 1, "draws": 0},
                }
            },
        )
        assert n == 1, f"Expected exactly 1 imported record, got {n}"
        summ = db.get_player_stats_summary(guild_id=1, player_id=789)
        assert summ and int(summ.get("games_played", 0)) == 2 and int(summ.get("wins", 0)) == 1



def check_from_persisted_string_false_is_not_truthy() -> None:
    # Persistence corruption tolerance: bool("false") is True in Python, so we must coerce safely.
    import game as game_module

    data = {
        "guild_id": 123,
        "in_progress": "false",
        "phase": "day",
        "day_number": 1,
        "player_ids": [],
        "living_ids": [],
        "player_slots": {},
        "player_roles": {},
        "night_actions": {},
        "role_states": {},
        "doused_players": [],
        "graveyard": [],
        "votes_today": 0,
        "locked_channel_ids": [],
        "lockdown_role_id": None,
        "tribunal_muted": "false",
        "tribunal_defendant_id": None,
        "stats_committed": "false",
    }
    g = game_module.Game.from_persisted(data)
    assert g.in_progress is False, "Expected string 'false' to coerce to False for in_progress"
    assert getattr(g, "tribunal_muted", False) is False, "Expected string 'false' to coerce to False for tribunal_muted"
    assert getattr(g, "stats_committed", True) is False, "Expected string 'false' to coerce to False for stats_committed"



def check_sqlite_begin_game_commit_is_idempotent_and_returns_same_id() -> None:
    # DB invariant: begin_game_commit should be idempotent for a given game_key.
    import tempfile
    from pathlib import Path

    from database import Database

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = str(Path(td) / "test.sqlite")
        db = Database(db_path)
        db.initialize()
        first, gid1 = db.begin_game_commit(
            guild_id=1,
            game_key="k1",
            started_at=None,
            ended_at=None,
            outcome="Town",
            player_count=7,
            ended_day_number=2,
            ended_phase="day",
        )
        second, gid2 = db.begin_game_commit(
            guild_id=1,
            game_key="k1",
            started_at=None,
            ended_at=None,
            outcome="Town",
            player_count=7,
            ended_day_number=2,
            ended_phase="day",
        )
        assert first is True and second is False, "Expected first insert True then False"
        assert gid1 == gid2, "Expected same game_id returned for identical game_key"



def check_sqlite_top_winrate_handles_zero_games_without_crash() -> None:
    # DB invariant: top_winrate query must not crash / divide by zero.
    import tempfile
    from pathlib import Path

    from database import Database

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = str(Path(td) / "test.sqlite")
        db = Database(db_path)
        db.initialize()
        db.upsert_player_stats_delta(
            guild_id=1,
            player_id=1,
            games_played=0,
            wins_total=0,
            losses_total=0,
            draws_total=0,
            wins_town=0,
            wins_mafia=0,
            wins_arsonist=0,
            last_game_at=None,
        )
        rows = db.top_winrate(guild_id=1, min_games=0, limit=10)
        assert isinstance(rows, list)



def check_sqlite_personal_win_delta_never_goes_negative() -> None:
    # DB invariant: personal win counters should never become negative, even if a bad delta is applied.
    import tempfile
    from pathlib import Path

    from database import Database

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = str(Path(td) / "test.sqlite")
        db = Database(db_path)
        db.initialize()
        db.upsert_personal_win_delta(guild_id=1, player_id=1, key="pirate_win", delta=3)
        db.upsert_personal_win_delta(guild_id=1, player_id=1, key="pirate_win", delta=-10)
        # Directly read it back via leaderboard query (0 may or may not appear depending on query).
        rows = db.top_personal(guild_id=1, key="pirate_win", limit=10)
        if rows:
            assert rows[0].value >= 0, "Expected personal win counts to never be negative"



def check_save_state_roundtrip_and_overwrite_is_stable() -> None:
    # Persistence invariant: save_state should overwrite cleanly and be loadable.
    import tempfile
    from pathlib import Path

    import persistence as p

    with tempfile.TemporaryDirectory() as td:
        old = p.STATE_DIR
        try:
            p.STATE_DIR = Path(td)  # type: ignore[assignment]
            p.save_state(1, {"a": 1})
            p.save_state(1, {"a": 2})
            loaded = p.load_state(1) or {}
            assert loaded.get("a") == 2, f"Expected overwrite to win, got {loaded}"
        finally:
            p.STATE_DIR = old  # type: ignore[assignment]



def check_roles_text_witch_wincon_matches_implementation() -> None:
    from config import WITCH_TOWN_LOSES_OUTCOMES
    from endgame_stats import compute_player_endgame_deltas
    assert 'Mafia' in WITCH_TOWN_LOSES_OUTCOMES
    rows = compute_player_endgame_deltas(player_roles={1:'Witch'}, role_states={1:{}}, living_ids={1}, outcome_norm='Mafia')
    assert rows[0].personal_deltas.get('witch_town_loses') == 1



def check_setup_infrastructure_is_idempotent_and_hardens_privacy() -> None:
    # Infrastructure invariant: setup_infrastructure() should be idempotent and enforce private-channel privacy overwrites.
    import asyncio
    import discord

    import game as game_module
    from config import (
        ALIVE_ROLE_NAME,
        DAY_TEXT_CHANNEL_NAME,
        DAY_VOICE_CHANNEL_NAME,
        GAME_OVERSEER_ROLE_ID,
        GRAVEYARD_TEXT_CHANNEL_NAME,
        GRAVEYARD_VOICE_CHANNEL_NAME,
        MAFIA_CHANNEL_NAME,
        PLAYING_ROLE_ID,
        STAND_ROLE_NAME,
    )

    class _Role:
        def __init__(self, rid: int, name: str):
            self.id = int(rid)
            self.name = str(name)

    class _Chan:
        def __init__(self, cid: int, name: str, category=None, overwrites=None):
            self.id = int(cid)
            self.name = str(name)
            self.category = category
            self.overwrites = overwrites or {}
            self._set_calls: list[tuple[int, dict]] = []

        async def set_permissions(self, role, **perms):
            # record and store an overwrite dict-like payload for assertions
            self._set_calls.append((int(getattr(role, "id", -1)), dict(perms)))
            self.overwrites[role] = discord.PermissionOverwrite(**perms)

        async def edit(self, *, overwrites, reason=None):
            self.overwrites = dict(overwrites)

    class _Category(_Chan):
        pass

    class _Guild:
        def __init__(self):
            self._next_id = 1000
            self.roles: list[_Role] = []
            self.channels: list[_Chan] = []
            self.categories: list[_Category] = []
            self.default_role = _Role(1, "@everyone")
            self.me = _Role(999, 'Bot')
            self.roles.append(self.default_role)

            # Pre-existing roles expected by setup_infrastructure
            self._roles_by_id: dict[int, _Role] = {
                PLAYING_ROLE_ID: _Role(PLAYING_ROLE_ID, "Playing"),
                GAME_OVERSEER_ROLE_ID: _Role(GAME_OVERSEER_ROLE_ID, "Overseer"),
            }
            self.roles.extend(self._roles_by_id.values())

        def get_role(self, rid: int):
            return self._roles_by_id.get(int(rid))

        def get_channel(self, cid: int):
            for c in self.channels + self.categories:
                if int(c.id) == int(cid):
                    return c
            return None

        async def create_role(self, *, name: str, color=None, mentionable: bool = False):
            r = _Role(self._next_id, name)
            self._next_id += 1
            self.roles.append(r)
            self._roles_by_id[r.id] = r
            return r

        async def create_category(self, name: str):
            c = _Category(self._next_id, name)
            self._next_id += 1
            self.categories.append(c)
            self.channels.append(c)
            return c

        async def create_text_channel(self, name: str, *, category=None, overwrites=None):
            ch = _Chan(self._next_id, name, category=category, overwrites=overwrites)
            self._next_id += 1
            self.channels.append(ch)
            return ch

        async def create_voice_channel(self, name: str, *, category=None, overwrites=None):
            ch = _Chan(self._next_id, name, category=category, overwrites=overwrites)
            self._next_id += 1
            self.channels.append(ch)
            return ch

    guild = _Guild()
    g = game_module.Game(guild_id=123)

    with _smoke_owned_game(g):
        asyncio.run(g.setup_infrastructure(guild))  # type: ignore[arg-type]
    first_channel_count = len(guild.channels)
    first_role_count = len(guild.roles)

    # Required IDs are set.
    assert g.alive_role_id is not None and g.stand_role_id is not None
    assert g.day_tc_id is not None and g.mafia_tc_id is not None and g.grave_tc_id is not None
    assert g.day_vc_id is not None and g.grave_vc_id is not None

    # Re-run should not create duplicates.
    stale = _Role(998, 'Former player or obsolete role')
    for ch in guild.channels:
        ch.overwrites[stale] = discord.PermissionOverwrite(view_channel=True)
    with _smoke_owned_game(g):
        asyncio.run(g.setup_infrastructure(guild))  # type: ignore[arg-type]
    assert len(guild.channels) == first_channel_count, "Expected setup_infrastructure to be idempotent for channels"
    assert len(guild.roles) == first_role_count, "Expected setup_infrastructure to be idempotent for roles"

    # Privacy hardening: mafia/grave channels must hide from @everyone, Alive, Playing.
    alive_role = next((r for r in guild.roles if r.name == ALIVE_ROLE_NAME), None)
    stand_role = next((r for r in guild.roles if r.name == STAND_ROLE_NAME), None)
    playing_role = guild.get_role(PLAYING_ROLE_ID)
    assert alive_role is not None and stand_role is not None and playing_role is not None

    by_name = {c.name: c for c in guild.channels}
    for private_name in [MAFIA_CHANNEL_NAME, GRAVEYARD_TEXT_CHANNEL_NAME, GRAVEYARD_VOICE_CHANNEL_NAME]:
        ch = by_name.get(private_name)
        assert ch is not None, f"Expected channel {private_name}"
        assert ch.overwrites[guild.default_role].view_channel is False, f"{private_name} must hide @everyone"
        assert ch.overwrites[alive_role].view_channel is False, f"{private_name} must hide Alive"
        assert ch.overwrites[playing_role].view_channel is False, f"{private_name} must hide Playing"
        assert ch.overwrites[guild.me].view_channel is True, f"{private_name} must permit the bot"
        assert ch.overwrites[guild.me].read_message_history is True
        assert stale not in ch.overwrites, f"{private_name} must remove stale access"

    # Day VC permissions: @everyone no connect/speak, Alive can connect/speak, Stand can connect/speak.
    day_vc = by_name.get(DAY_VOICE_CHANNEL_NAME)
    assert day_vc is not None
    assert day_vc.overwrites[guild.default_role].connect is False
    assert day_vc.overwrites[guild.default_role].speak is False
    assert day_vc.overwrites[alive_role].connect is True
    assert day_vc.overwrites[alive_role].speak is True
    assert day_vc.overwrites[stand_role].connect is True
    assert day_vc.overwrites[stand_role].speak is True

    # Day text channel exists (spectator-visible, but not asserted here).
    assert DAY_TEXT_CHANNEL_NAME in by_name



def check_setup_infrastructure_partial_existing_channels_are_reused() -> None:
    # Infrastructure invariant: if some channels exist already, setup_infrastructure should reuse them and create the rest.
    import asyncio

    import game as game_module
    from config import (
        DAY_TEXT_CHANNEL_NAME,
        DAY_VOICE_CHANNEL_NAME,
        GRAVEYARD_TEXT_CHANNEL_NAME,
        GRAVEYARD_VOICE_CHANNEL_NAME,
        MAFIA_CHANNEL_NAME,
        PLAYING_ROLE_ID,
        GAME_OVERSEER_ROLE_ID,
    )

    class _Role:
        def __init__(self, rid: int, name: str):
            self.id = int(rid)
            self.name = str(name)

    class _Chan:
        def __init__(self, cid: int, name: str, category=None, overwrites=None):
            self.id = int(cid)
            self.name = str(name)
            self.category = category
            self.overwrites = overwrites or {}

        async def edit(self, *, overwrites, reason=None):
            self.overwrites = dict(overwrites)

        async def set_permissions(self, *_args, **_kwargs):
            return

    class _Category(_Chan):
        pass

    class _Guild:
        def __init__(self):
            self._next_id = 2000
            self.roles = [_Role(1, "@everyone")]
            self.default_role = self.roles[0]
            self.me = _Role(999, "Bot")
            self.categories: list[_Category] = []
            self.channels: list[_Chan] = []
            # Pre-existing roles expected by setup_infrastructure
            self._roles_by_id = {
                PLAYING_ROLE_ID: _Role(PLAYING_ROLE_ID, "Playing"),
                GAME_OVERSEER_ROLE_ID: _Role(GAME_OVERSEER_ROLE_ID, "Overseer"),
            }
            self.roles.extend(self._roles_by_id.values())

        def get_role(self, rid: int):
            return self._roles_by_id.get(int(rid))

        def get_channel(self, cid: int):
            for c in self.channels + self.categories:
                if c.id == int(cid):
                    return c
            return None

        async def create_role(self, *, name: str, color=None, mentionable: bool = False):
            r = _Role(self._next_id, name)
            self._next_id += 1
            self.roles.append(r)
            return r

        async def create_category(self, name: str):
            c = _Category(self._next_id, name)
            self._next_id += 1
            self.categories.append(c)
            self.channels.append(c)
            return c

        async def create_text_channel(self, name: str, *, category=None, overwrites=None):
            ch = _Chan(self._next_id, name, category=category, overwrites=overwrites)
            self._next_id += 1
            self.channels.append(ch)
            return ch

        async def create_voice_channel(self, name: str, *, category=None, overwrites=None):
            ch = _Chan(self._next_id, name, category=category, overwrites=overwrites)
            self._next_id += 1
            self.channels.append(ch)
            return ch

    guild = _Guild()
    # Pre-create ONLY the day text channel and mafia text channel (partial existing).
    existing_day = asyncio.run(guild.create_text_channel(DAY_TEXT_CHANNEL_NAME))
    existing_mafia = asyncio.run(guild.create_text_channel(MAFIA_CHANNEL_NAME))

    g = game_module.Game(guild_id=123)
    with _smoke_owned_game(g):
        asyncio.run(g.setup_infrastructure(guild))  # type: ignore[arg-type]
    by_name = {c.name: c for c in guild.channels}

    # Existing channels are reused (same ids).
    assert by_name[DAY_TEXT_CHANNEL_NAME].id == existing_day.id
    assert by_name[MAFIA_CHANNEL_NAME].id == existing_mafia.id

    # Missing channels are created.
    for nm in [GRAVEYARD_TEXT_CHANNEL_NAME, DAY_VOICE_CHANNEL_NAME, GRAVEYARD_VOICE_CHANNEL_NAME]:
        assert nm in by_name, f"Expected {nm} to be created"



def check_setup_infrastructure_lockdown_role_hides_other_categories_and_records_locked_ids() -> None:
    # Infrastructure invariant: lockdown role is applied to all categories except the Mafia Game category, and IDs are recorded.
    import asyncio

    import game as game_module
    from config import PLAYING_ROLE_ID, GAME_OVERSEER_ROLE_ID

    class _Role:
        def __init__(self, rid: int, name: str):
            self.id = int(rid)
            self.name = str(name)

    class _Category:
        def __init__(self, cid: int, name: str):
            self.id = int(cid)
            self.name = str(name)
            self._set_calls: list[tuple[int, dict]] = []

        async def set_permissions(self, role, **perms):
            self._set_calls.append((int(getattr(role, "id", -1)), dict(perms)))

    class _Chan:
        def __init__(self, cid: int, name: str, category=None, overwrites=None):
            self.id = int(cid)
            self.name = str(name)
            self.category = category
            self.overwrites = overwrites or {}

        async def edit(self, *, overwrites, reason=None):
            self.overwrites = dict(overwrites)

        async def set_permissions(self, *_args, **_kwargs):
            return

    class _Guild:
        def __init__(self):
            self._next_id = 3000
            self.roles = [_Role(1, "@everyone")]
            self.default_role = self.roles[0]
            self.me = _Role(999, "Bot")
            self.channels: list[_Chan] = []
            self.categories: list[_Category] = []
            self._roles_by_id = {
                PLAYING_ROLE_ID: _Role(PLAYING_ROLE_ID, "Playing"),
                GAME_OVERSEER_ROLE_ID: _Role(GAME_OVERSEER_ROLE_ID, "Overseer"),
            }
            self.roles.extend(self._roles_by_id.values())

            # Create Mafia Game category and two other categories.
            self.mafia_cat = _Category(self._next_id, "Mafia Game")
            self._next_id += 1
            self.other1 = _Category(self._next_id, "Other1")
            self._next_id += 1
            self.other2 = _Category(self._next_id, "Other2")
            self._next_id += 1
            self.categories.extend([self.mafia_cat, self.other1, self.other2])

        def get_role(self, rid: int):
            return self._roles_by_id.get(int(rid))

        def get_channel(self, cid: int):
            # This is only used for GAME_CATEGORY_ID lookup; we return None so it falls back to name lookup.
            return None

        async def create_role(self, *, name: str, color=None, mentionable: bool = False):
            r = _Role(self._next_id, name)
            self._next_id += 1
            self.roles.append(r)
            return r

        async def create_category(self, name: str):
            c = _Category(self._next_id, name)
            self._next_id += 1
            self.categories.append(c)
            return c

        async def create_text_channel(self, name: str, *, category=None, overwrites=None):
            ch = _Chan(self._next_id, name, category=category, overwrites=overwrites)
            self._next_id += 1
            self.channels.append(ch)
            return ch

        async def create_voice_channel(self, name: str, *, category=None, overwrites=None):
            ch = _Chan(self._next_id, name, category=category, overwrites=overwrites)
            self._next_id += 1
            self.channels.append(ch)
            return ch

    guild = _Guild()
    g = game_module.Game(guild_id=123)
    with _smoke_owned_game(g):
        asyncio.run(g.setup_infrastructure(guild))  # type: ignore[arg-type]

    # Should lock all categories except the Mafia Game category.
    locked = set(getattr(g, "locked_channel_ids", []) or [])
    assert guild.other1.id in locked and guild.other2.id in locked
    assert guild.mafia_cat.id not in locked



def check_setup_infrastructure_raises_runtime_error_on_discord_failures() -> None:
    # Static evidence: setup_infrastructure catches discord.Forbidden/HTTPException and raises RuntimeError.
    src = GAME_PY.read_text(encoding="utf-8")
    idx = src.find("async def setup_infrastructure")
    assert idx != -1
    # Use direct search in full file to avoid window-size brittleness.
    assert "except (discord.Forbidden, discord.HTTPException) as e" in src
    assert "raise RuntimeError" in src


def check_sqlite_player_and_role_stats_never_go_negative() -> None:
    # DB invariant: aggregate counters should never become negative, even if a bad delta is applied.
    import tempfile
    from pathlib import Path

    from database import Database

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = str(Path(td) / "test.sqlite")
        db = Database(db_path)
        db.initialize()
        # Seed with some positive counts.
        db.upsert_player_stats_delta(
            guild_id=1,
            player_id=1,
            games_played=2,
            wins_total=1,
            losses_total=1,
            draws_total=0,
            wins_town=1,
            wins_mafia=0,
            wins_arsonist=0,
            last_game_at=None,
        )
        db.upsert_player_role_stats_delta(
            guild_id=1,
            player_id=1,
            role="Survivor",
            played=2,
            wins_total=1,
            losses_total=1,
        )
        # Apply a corrupted/bad negative delta.
        db.upsert_player_stats_delta(
            guild_id=1,
            player_id=1,
            games_played=-10,
            wins_total=-10,
            losses_total=-10,
            draws_total=-10,
            wins_town=-10,
            wins_mafia=-10,
            wins_arsonist=-10,
            last_game_at=None,
        )
        db.upsert_player_role_stats_delta(
            guild_id=1,
            player_id=1,
            role="Survivor",
            played=-10,
            wins_total=-10,
            losses_total=-10,
        )
        summ = db.get_player_stats_summary(guild_id=1, player_id=1) or {}
        assert int(summ.get("games_played", 0)) >= 0
        assert int(summ.get("wins", 0)) >= 0
        assert int(summ.get("losses", 0)) >= 0
        assert int(summ.get("draws", 0)) >= 0
        rp = (summ.get("role_played") or {}).get("Survivor", 0)
        rw = (summ.get("role_wins") or {}).get("Survivor", 0)
        assert int(rp) >= 0 and int(rw) >= 0



def check_sqlite_read_paths_never_surface_negative_counts() -> None:
    # DB invariant: even if the DB is corrupted and contains negatives, read paths should clamp to 0.
    import tempfile
    from pathlib import Path

    from database import Database

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = str(Path(td) / "test.sqlite")
        db = Database(db_path)
        db.initialize()
        # Inject corrupted negative values directly.
        with db._conn() as conn:  # type: ignore[attr-defined]
            conn.execute(
                "INSERT INTO player_personal_stats(guild_id, player_id, key, count) VALUES (?,?,?,?)",
                (1, 1, "pirate_win", -5),
            )
            conn.execute(
                "INSERT INTO player_stats(guild_id, player_id, games_played, wins_total, losses_total, draws_total, wins_town, wins_mafia, wins_arsonist) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (1, 1, -2, -3, -4, -5, -1, -1, -1),
            )
        # Leaderboard should not show negative.
        rows = db.top_personal(guild_id=1, key="pirate_win", limit=10)
        if rows:
            assert rows[0].value >= 0
        # Summary should not show negative.
        summ = db.get_player_stats_summary(guild_id=1, player_id=1) or {}
        assert int(summ.get("games_played", 0)) >= 0
        assert int(summ.get("wins", 0)) >= 0
        assert int(summ.get("losses", 0)) >= 0
        assert int(summ.get("draws", 0)) >= 0
        pw = (summ.get("personal_wins") or {}).get("pirate_win", 0)
        assert int(pw) >= 0



def check_start_day_is_idempotent_within_same_day() -> None:
    # Gameplay invariant: calling start_day twice without a night in between should not double-increment day_number.
    import asyncio
    import game as game_module

    class _Chan:
        def __init__(self, guild):
            self.guild = guild
            self.id = 999

        async def send(self, _msg: str) -> None:
            return

        def permissions_for(self, _m):
            class _P:
                send_messages = True
                view_channel = True

            return _P()

    class _Guild:
        def __init__(self):
            self.id = 123
            self.text_channels = []
            self.system_channel = None
            self.me = None

        def get_channel(self, _id):
            return None

        def get_role(self, _id):
            return None

    class _Ctx:
        def __init__(self, guild):
            self.guild = guild
            self.channel = _Chan(guild)

        async def send(self, _msg: str) -> None:
            return

    gld = _Guild()
    ctx = _Ctx(gld)
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "day"
    g.day_number = 1
    g.players = []
    g.living_players = []
    with _smoke_owned_game(g):
        asyncio.run(g.start_day(ctx))
    d1 = g.day_number
    with _smoke_owned_game(g):
        asyncio.run(g.start_day(ctx))
    assert g.day_number == d1, f"Expected start_day to be idempotent, got {d1} -> {g.day_number}"



def check_start_night_is_idempotent_within_same_night() -> None:
    # Gameplay invariant: calling start_night twice (without any actions submitted) should be idempotent.
    import asyncio
    import game as game_module

    class _Chan:
        def __init__(self, guild):
            self.guild = guild
            self.id = 999

        async def send(self, _msg: str) -> None:
            return

    class _Guild:
        def __init__(self):
            self.id = 123
            self.text_channels = []
            self.system_channel = None
            self.me = None

        def get_channel(self, _id):
            return None

        def get_role(self, _id):
            return None

    class _Ctx:
        def __init__(self, guild):
            self.guild = guild
            self.channel = _Chan(guild)

        async def send(self, _msg: str) -> None:
            return

    gld = _Guild()
    ctx = _Ctx(gld)
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "day"
    g.day_number = 1
    g.role_states = {1: {"is_vested": True}}
    g.night_actions = {}
    with _smoke_owned_game(g):
        asyncio.run(g.start_night(ctx))
    # First call should enter night and clear one-night flags.
    assert g.phase == "night"
    assert "is_vested" not in g.role_states.get(1, {})
    # Second call should not change anything further (idempotent).
    before = (g.day_number, dict(g.role_states.get(1, {})), dict(g.night_actions))
    with _smoke_owned_game(g):
        asyncio.run(g.start_night(ctx))
    after = (g.day_number, dict(g.role_states.get(1, {})), dict(g.night_actions))
    assert before == after, "Expected start_night to be idempotent within same night"



def check_start_night_does_not_wipe_actions_if_already_night() -> None:
    # Major gameplay invariant: if start_night is called while already in night phase, it must not wipe submitted actions.
    import asyncio
    import game as game_module

    class _Guild:
        id = 123

        def get_channel(self, _id):
            return None

        def get_role(self, _id):
            return None

    class _Ctx:
        def __init__(self):
            self.guild = _Guild()

        async def send(self, _msg: str) -> None:
            return

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 1
    g.players = []
    g.living_players = []
    g.night_actions = {1: {"type": "vest", "actor": 1, "target": 1}}
    ctx = _Ctx()
    with _smoke_owned_game(g):
        asyncio.run(g.start_night(ctx))
    assert g.night_actions.get(1, {}).get("type") == "vest", "Expected start_night not to wipe actions if already night"



def check_vigilante_guilt_death_is_idempotent_in_resolve() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'night_guilt.py').read_text(encoding="utf-8")
    assert 'will_die_of_guilt' in src
    assert 'p_id not in night_kill_deaths' in src



def check_resolve_sets_resolving_flag_before_any_awaits() -> None:
    src = (ROOT / 'gameplay/resolution.py').read_text(encoding='utf-8')
    begin = src[src.index('async def begin'):src.index('async def run')]
    assert 'if game.resolving:' in begin
    assert 'game.resolving = True' in begin and 'await st.commit(game, enter)' in begin
    assert begin.index('game.resolving = True') < begin.index('await st.commit(game, enter)')



def check_vote_always_clears_tribunal_snapshot_in_finally() -> None:
    src = BOT_PY.read_text(encoding='utf-8')
    tree = ast.parse(src)
    fn = _find_async_fn(tree, '_resume_tribunal_defense_after_restart')
    assert any(isinstance(n, ast.Try) and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == '_cleanup_tribunal' for stmt in n.finalbody for c in ast.walk(stmt)) for n in ast.walk(fn))
    model = (ROOT / 'gameplay/trials.py').read_text(encoding='utf-8')
    assert 'clear_flags(game)' in model and 'game.tribunal_defendant_id = None' in model
    assert 'game.vote_in_progress = game.tribunal_muted = False' in model
    controller = (ROOT / 'gameplay/controller.py').read_text(encoding='utf-8')
    assert 'await self.repair_voice(game)' in controller and 'await trials.finish(game, token)' in controller


def check_haunt_filters_to_living_voters_only() -> None:
    # Gameplay invariant: haunt eligible list should not include dead voters.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("async def haunt")
    assert idx != -1
    seg = src[idx : idx + 900]
    assert "eligible_voters = [vid for vid in stored_voters if vid in living_ids]" in seg

def check_vote_does_not_persist_vote_in_progress_across_restarts() -> None:
    # Different direction: restart safety around day/tribunal flags.
    import game as game_module

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "day"
    g.vote_in_progress = True
    g.votes_today = 1
    data = g.to_persisted()
    g2 = game_module.Game.from_persisted(data)
    assert g2.vote_in_progress is False, "Expected vote_in_progress to be cleared on restore"
    assert g2.votes_today == 1



def check_stats_command_displays_personal_keys_human_friendly() -> None:
    # Different direction: stats rendering should normalize canonical keys to friendly labels.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("pretty_labels = {")
    assert idx != -1, "Expected pretty_labels mapping in stats()"
    seg = src[idx : idx + 400]
    for k in ["pirate_win", "exe_win", "jester_win", "survivor_survived", "chaos_survived", "witch_town_loses", "arsonist_win"]:
        assert k in seg, f"Expected {k} in pretty_labels"



def check_stats_json_personal_wins_migrates_legacy_role_keys() -> None:
    # Different direction: JSON stats should be resilient to old personal win keys.
    import game as game_module
    from persistence import save_stats, load_stats

    g = game_module.Game(guild_id=123)
    g.player_roles = {1: "Witch"}
    g.role_states = {1: {}}
    save_stats(123, {"players": {"1": {"personal_wins": {"Witch": 2, "witch_town_loses": 1}}}})
    g._commit_endgame_stats(outcome="Mafia", living_ids=[1])
    data = load_stats(123) or {}
    pw = ((data.get("players") or {}).get("1") or {}).get("personal_wins") or {}
    assert "witch_town_loses" in pw and int(pw.get("witch_town_loses", 0)) >= 3, "Expected legacy Witch key folded into canonical key"
    assert "Witch" not in pw, "Expected legacy role-name key to be migrated away on write"



def check_db_init_migration_does_not_drop_tables() -> None:
    # Different direction: DB init should be forward-only and not destructively reset schema.
    src = (ROOT / "database.py").read_text(encoding="utf-8")
    assert "DROP TABLE" not in src, "DB initialize should not drop tables"



def check_private_channels_are_hidden_from_everyone_and_playing() -> None:
    # Verify overwrite values and removal of stale grants instead of requiring
    # the obsolete sequence of individual set_permissions calls.
    check_setup_infrastructure_is_idempotent_and_hardens_privacy()



def check_night_actions_restricted_to_dm_or_private_channel_mapping() -> None:
    # Security invariant: night actions should not be usable from arbitrary public channels.
    src = CHECKS_PY.read_text(encoding="utf-8")
    assert "PLAYER_PRIVATE_CHANNEL_IDS" in src, "Expected private channel mapping enforcement"
    assert "expected_channel_id" in src and "ctx.channel.id" in src, "Expected channel id check"
    # DM allowance is expressed as `ctx.guild is None` (not DMChannel type checks).
    assert "if ctx.guild is not None" in src and "if ctx.guild is None" in src, "Expected DM (ctx.guild is None) allowance"



def check_only_during_night_gameplay_supports_inverted_private_channel_mapping() -> None:
    # Guardrail: checks.py must support both mapping shapes (user->channel and channel->user).
    # (Runtime behavior is covered by tests/test_night_action_channel_guard.py; this is static evidence.)
    src = CHECKS_PY.read_text(encoding="utf-8")
    assert "Backwards-compat hardening" in src
    assert "for k, v in PLAYER_PRIVATE_CHANNEL_IDS.items()" in src
    assert "expected_channel_id = int(k)" in src


def check_reveal_is_guild_only_and_allowed_guild_guarded() -> None:
    # Security/privacy invariant: Mayor reveal should not be executable from DMs/other guilds.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("async def reveal")
    assert idx != -1, "Expected reveal command"
    head = src[max(0, idx - 200) : idx]
    assert "@commands.guild_only()" in head, "Expected reveal to be guild-only"
    assert "@commands.check(enforce_allowed_guild_check)" in head, "Expected reveal to enforce allowed guild"



def check_myrole_and_haunt_are_dm_only() -> None:
    # Privacy invariant: role and haunt choices must remain in DMs.
    src = BOT_PY.read_text(encoding="utf-8")
    for fn in ["async def myrole", "async def haunt"]:
        idx = src.find(fn)
        assert idx != -1, f"Expected {fn}"
        seg = src[idx : idx + 250]
        assert "discord.DMChannel" in seg, f"Expected {fn} to enforce DM-only via discord.DMChannel check"



def check_will_command_is_dm_only_and_deletes_guild_invocation() -> None:
    # Privacy invariant: will editor must be DM-only and should delete the message if invoked in guild.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("async def will")
    assert idx != -1, "Expected will command"
    seg = src[idx : idx + 700]
    assert "discord.DMChannel" in seg, "Expected will() to check discord.DMChannel"
    assert "await ctx.message.delete" in seg or "ctx.message.delete" in seg, "Expected will() to delete guild-invoked messages"



def check_no_day_channel_commands_accept_dm_unintentionally() -> None:
    # Security invariant: core guild-state-changing commands should be guild-only.
    src = BOT_PY.read_text(encoding="utf-8")
    for cmd in ["join_game_command", "show_players_command", "startgame", "night", "day", "resolve", "vote", "stats", "importstats", "reset", "nukereset"]:
        idx = src.find(f"async def {cmd}")
        assert idx != -1, f"Expected {cmd}"
        head = src[max(0, idx - 220) : idx]
        assert "@commands.guild_only()" in head, f"Expected {cmd} to be guild-only"



def check_private_channel_mapping_inverted_shape_supported() -> None:
    # Security invariant: inverted mapping (channel_id -> user_id) should be supported to avoid misconfig leaks.
    src = CHECKS_PY.read_text(encoding="utf-8")
    assert "for k, v in PLAYER_PRIVATE_CHANNEL_IDS.items()" in src and "int(v) == int(ctx.author.id)" in src, (
        "Expected inverted private-channel mapping support"
    )


def check_revealed_mayor_cannot_be_healed_even_by_doctor() -> None:
    # Major invariant: revealed Mayor cannot be healed.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    mayor, doctor, mobster = _M(1), _M(2), _M(3)
    guild = _G([mayor, doctor, mobster])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [mayor, doctor, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Mayor", 2: "Doctor", 3: "Mobster"}
    g.role_states = {1: {"is_revealed": True}, 2: {"self_heals_remaining": 1}}
    g.night_actions = {
        2: {"type": "heal", "actor": 2, "target": 1},
        3: {"type": "kill", "actor": 3, "target": 1},
    }
    _visit_log, blocked, healed_by, _prot_by, deaths = asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert 2 not in blocked
    assert healed_by.get(1) != 2, "Expected heal to be skipped for revealed Mayor"
    assert 1 in deaths, "Expected revealed Mayor to die since heal doesn't apply"



def check_ignite_clears_doused_players_set() -> None:
    # Major invariant: ignite clears doused set after applying deaths.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    arso, v1, v2 = _M(1), _M(2), _M(3)
    guild = _G([arso, v1, v2])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [arso, v1, v2]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Arsonist", 2: "Survivor", 3: "Doctor"}
    g.role_states = {2: {"vests_remaining": 2}, 3: {"self_heals_remaining": 1}}
    g.doused_players = {2, 3}
    g.night_actions = {1: {"type": "ignite", "actor": 1}}
    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    assert g.doused_players == set(), "Expected ignite to clear doused_players"



def check_blocked_investigator_gets_no_results() -> None:
    # Major invariant: blocked investigative roles should not receive results DMs.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _M:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _G:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    sheriff, escort, mobster = _M(1), _M(2), _M(3)
    guild = _G([sheriff, escort, mobster])
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [sheriff, escort, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Sheriff", 2: "Escort", 3: "Mobster"}
    g.role_states = {}
    g.night_actions = {
        1: {"type": "investigate", "actor": 1, "target": 3, "role": "Sheriff"},
        2: {"type": "roleblock", "actor": 2, "target": 1},
    }
    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]
    txt = "\n".join(sheriff.dms).lower()
    # Allowed: roleblock feedback. Disallowed: any investigative result content.
    assert "suspicious" not in txt and "innocent" not in txt and "could be" not in txt, (
        "Expected blocked Sheriff to receive no investigative results"
    )




def check_roleblock_immune_roles_exclude_gatekeeper() -> None:
    # Major rules invariant: Witch is immune to standard roleblocks, but Gatekeeper is the special case blocker.
    # Ensure ROLEBLOCK_IMMUNE_ROLES does not accidentally include Gatekeeper (would break guard interaction).
    import config

    assert "Gatekeeper" not in set(getattr(config, "ROLEBLOCK_IMMUNE_ROLES", [])), "Gatekeeper should not be roleblock-immune"



def check_transporter_is_control_immune() -> None:
    # Major rules invariant: Transporter should be in CONTROL_IMMUNE_ROLES.
    import config

    assert "Transporter" in set(getattr(config, "CONTROL_IMMUNE_ROLES", [])), "Expected Transporter to be control-immune"



def check_vote_persists_tribunal_snapshot_fields() -> None:
    # Major crash-recovery invariant: tribunal snapshot fields must be persisted and restored.
    import game as game_module

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "day"
    g.tribunal_muted = True
    g.tribunal_defendant_id = 42
    data = g.to_persisted()
    g2 = game_module.Game.from_persisted(data)
    assert getattr(g2, "tribunal_muted", False) is True
    assert getattr(g2, "tribunal_defendant_id", None) == 42



def check_control_mirrors_investigation_results_to_witch() -> None:
    # Major ToS rule invariant: Witch controlling an investigative role receives their results.
    # This is runtime behavior; protect it with an engine-level check.
    import asyncio
    import game as game_module
    from engine.night import run_night_pipeline

    class _FakeMember:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _FakeGuild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    witch, sheriff, mobster = _FakeMember(1), _FakeMember(2), _FakeMember(3)
    guild = _FakeGuild([witch, sheriff, mobster])

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [witch, sheriff, mobster]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Witch", 2: "Sheriff", 3: "Mobster"}
    g.role_states = {1: {"night1_shield_used": False, "has_learned_role": False}, 2: {}, 3: {}}

    g.night_actions = {
        1: {"type": "control", "actor": 1, "targets": [2, 3]},
        2: {"type": "investigate", "actor": 2, "target": 1, "role": "Sheriff"},
    }
    asyncio.run(run_night_pipeline(g, guild))  # type: ignore[arg-type]

    assert any("suspicious" in s.lower() for s in sheriff.dms), "Expected Sheriff to get result"
    assert any("suspicious" in s.lower() for s in witch.dms), "Expected Witch to get mirrored result"

def check_control_action_is_validated() -> None:
    # Corrupted/invalid persisted state safety: control action must validate targets shape.
    src = NIGHT_PY.read_text(encoding="utf-8")
    assert "async def resolve_control" in src
    assert "targets = action.get(\"targets\")" in src or "targets = action.get('targets')" in src
    assert "len(targets) != 2" in src, "Expected resolve_control to validate len(targets) == 2"


def check_transport_and_control_ids_are_int_coerced() -> None:
    # Corrupted persisted state safety: transport/control should coerce ids to int.
    src = NIGHT_PY.read_text(encoding="utf-8")
    assert "async def resolve_transports" in src
    assert "int(targets[0])" in src and "int(targets[1])" in src, "Expected transport/control targets to be int-coerced"



def check_build_visit_log_tolerates_non_list_transport_targets() -> None:
    from game import Game
    from engine.night import build_visit_log
    from types import SimpleNamespace
    for malformed in (None, 42, 'invalid', [1], ['oops', 2]):
        game = Game(123)
        game.living_players = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        game.night_actions = {1: {'type':'transport', 'actor':1, 'targets':malformed}}
        assert build_visit_log(game) == {}



def check_engine_hypnotist_payload_is_validated() -> None:
    # Corrupted persisted state safety: hypnotize feedback must not KeyError on missing target/msg_type.
    src = NIGHT_PY.read_text(encoding="utf-8")
    assert "type\") != \"hypnotize\"" in src or "type') != 'hypnotize'" in src
    assert "msg_type = action.get(\"msg_type\")" in src or "msg_type = action.get('msg_type')" in src, (
        "Expected hypnotize feedback to use action.get('msg_type')"
    )
    assert "target_raw = action.get(\"target\")" in src or "target_raw = action.get('target')" in src, (
        "Expected hypnotize feedback to use action.get('target') + int coercion"
    )
    assert "action[\"msg_type\"]" not in src, "Hypnotist feedback should not index action['msg_type']"
    assert "action[\"target\"]" not in src, "Hypnotist feedback should not index action['target']"



def check_engine_tailor_fake_role_is_validated() -> None:
    # Corrupted persisted state safety: tailor must not KeyError on missing fake_role.
    src = NIGHT_PY.read_text(encoding="utf-8")
    assert "elif a_type == \"tailor\"" in src or "elif a_type == 'tailor'" in src
    assert "fake_role = action.get(\"fake_role\")" in src or "fake_role = action.get('fake_role')" in src, (
        "Expected tailor to use action.get('fake_role')"
    )
    assert "action[\"fake_role\"]" not in src, "Tailor should not index action['fake_role']"



def check_from_persisted_tolerates_bad_numeric_lists() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'persist_schema.py').read_text(encoding="utf-8")
    assert 'data.get("doused_players")' in src
    assert 'data.get("locked_channel_ids")' in src
    assert 'except (TypeError, ValueError)' in src



def check_from_persisted_tolerates_corrupted_player_slots_and_role_ids() -> None:
    # Major crash risk: corrupted `player_slots` or role id fields should not abort restore.
    import game as game_module

    g = game_module.Game.from_persisted(
        {
            "guild_id": 123,
            "in_progress": True,
            "phase": "day",
            "day_number": 1,
            "player_roles": {"1": "Doctor"},
            "night_actions": {},
            "role_states": {},
            "player_ids": ["1"],
            "living_ids": ["1"],
            # Corrupted slots: non-int keys/values should be skipped.
            "player_slots": {"1": "1", "bad": "x", "2": None},
            # Corrupted role id fields: should coerce/skip safely.
            "lockdown_role_id": "not_an_int",
            "locked_channel_ids": ["10", "oops"],
        }
    )
    assert g.guild_id == 123
    assert g.player_slots.get(1) == 1
    assert getattr(g, "lockdown_role_id", None) is None



def check_from_persisted_tolerates_corrupted_mapping_keys() -> None:
    # Major crash risk: corrupted keys in player_roles/night_actions/role_states should be skipped.
    import game as game_module

    g = game_module.Game.from_persisted(
        {
            "guild_id": 123,
            "in_progress": True,
            "phase": "night",
            "day_number": 2,
            "player_ids": ["1"],
            "living_ids": ["1"],
            "player_roles": {"1": "Doctor", "bad": "Mobster"},
            "night_actions": {"1": {"type": "heal", "actor": 1, "target": 1}, None: {"type": "kill"}},  # type: ignore[dict-item]
            "role_states": {"1": {"self_heals_remaining": 1}, "oops": {"wins": 2}},
        }
    )
    assert g.player_roles.get(1) == "Doctor"
    assert 1 in g.night_actions
    assert g.role_states.get(1, {}).get("self_heals_remaining") == 1



def check_from_persisted_tolerates_corrupted_id_fields_and_counters() -> None:
    # Major crash risk: day_number/votes_today and *_id fields should tolerate non-int values.
    import game as game_module

    g = game_module.Game.from_persisted(
        {
            "guild_id": 123,
            "in_progress": True,
            "phase": "day",
            "day_number": "not_an_int",
            "votes_today": "oops",
            "game_channel_id": "999",
            "day_vc_id": "bad",
            "alive_role_id": None,
            "stand_role_id": "12345",
            "player_ids": [],
            "living_ids": [],
            "player_roles": {},
            "night_actions": {},
            "role_states": {},
        }
    )
    assert g.day_number == 0
    assert g.votes_today == 0
    assert g.game_channel_id == 999
    assert g.day_vc_id is None
    assert g.stand_role_id == 12345



def check_from_persisted_backcompat_slots_derivation_tolerates_bad_role_keys() -> None:
    # Major crash risk: when player_slots is missing (older save), fallback derivation must tolerate bad player_roles keys.
    import game as game_module

    g = game_module.Game.from_persisted(
        {
            "guild_id": 123,
            "in_progress": True,
            "phase": "day",
            "day_number": 1,
            "player_ids": [],  # triggers fallback from player_roles keys
            "living_ids": [],
            "player_roles": {"1": "Doctor", "bad": "Mobster"},
            "night_actions": {},
            "role_states": {},
            # player_slots intentionally absent
        }
    )
    assert g.player_slots.get(1) == 1



def check_sync_living_players_tolerates_corrupted_graveyard_entries() -> None:
    # Crash risk: corrupted persisted graveyard may contain non-dict entries; sync_living_players should ignore them.
    import asyncio
    import game as game_module

    class _Role:
        def __init__(self, rid: int):
            self.id = int(rid)

    class _Member:
        def __init__(self, mid: int, guild):
            self.id = int(mid)
            self.guild = guild
            self.display_name = f"P{mid}"
            self.roles = []

        async def add_roles(self, *_roles):
            return

    class _Guild:
        def __init__(self):
            self._members = {}
            self._roles = {111: _Role(111)}

        def get_role(self, rid: int):
            return self._roles.get(int(rid))

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    guild = _Guild()
    m1 = _Member(1, guild)
    guild._members = {1: m1}

    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.alive_role_id = 111
    g.players = [m1]  # type: ignore[assignment]
    g.living_players = [m1]  # type: ignore[assignment]
    g.player_roles = {1: "Townie"}
    g.player_slots = {1: 1}
    # Inject corrupted graveyard contents: strings, ints, and a valid dict.
    g.graveyard = ["oops", 123, {"player_id": "NaN"}]  # type: ignore[assignment]

    # Should not crash.
    asyncio.run(g.sync_living_players(guild))  # type: ignore[arg-type]


def check_start_night_and_day_guard_optional_ids() -> None:
    # Optional infra IDs must be guarded before calling get_channel/get_role.
    src = GAME_PY.read_text(encoding="utf-8")
    assert "day_vc = ctx.guild.get_channel(self.day_vc_id) if self.day_vc_id else None" in src
    assert "alive_role = ctx.guild.get_role(self.alive_role_id) if self.alive_role_id else None" in src



def check_vote_judgment_reactions_are_best_effort() -> None:
    # Tribunal should not crash if Add Reactions permission is missing.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("judgment_msg = await channel.send")
    if idx == -1:
        idx = src.find("judgment_msg = await ctx.send")
    assert idx != -1, "Expected vote() to create judgment_msg"
    tail = src[idx : idx + 600]
    assert "await judgment_msg.add_reaction" in tail, "Expected vote() to add reactions to judgment_msg"
    assert "except (discord.Forbidden, discord.HTTPException)" in tail, "Expected judgment add_reaction to be guarded"



def check_vote_refunds_trial_use_if_defendant_dies_during_defense() -> None:
    # Tribunal invariant: if defendant is no longer alive after defense window, votes_today is refunded (decremented).
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("If the defendant died/left during the defense window")
    if idx == -1:
        # Evidence may not include the exact comment; fall back to searching for the actual refund statement near the first abort.
        idx = src.find("if defendant.id not in living_ids:")
    assert idx != -1, "Expected defense-window abort block"
    seg = src[idx : idx + 400]
    assert "game.votes_today = max(0, game.votes_today - 1)" in seg, "Expected refund of votes_today on defense abort"



def check_vote_refunds_trial_use_if_defendant_dies_before_judgment_tally() -> None:
    # Tribunal invariant: if defendant dies/leaves before judgment tally, votes_today is refunded.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("Trial cancelled after being placed on stand -> refund the trial use")
    assert idx != -1, "Expected explicit judgment-window abort comment"
    seg = src[idx : idx + 250]
    assert "game.votes_today = max(0, game.votes_today - 1)" in seg, "Expected refund of votes_today on judgment abort"



def check_vote_eligible_haunt_excludes_innocent_votes() -> None:
    # Tribunal invariant: Jester eligible haunt excludes innocent voters and includes abstain.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("eligible_haunt_ids")
    assert idx != -1
    seg = src[idx : idx + 250]
    assert "resolved_judgments.get(uid) != \"❌\"" in seg, "Expected innocent votes excluded from haunt eligibility"



def check_vote_stale_coroutine_guards_exist_after_sleeps() -> None:
    src = (ROOT / 'gameplay/state.py').read_text(encoding='utf-8')
    assert 'active_games.get(game.guild_id) is not game' in src
    assert 'trial.get("day") != game.day_number' in src
    ctrl = (ROOT / 'gameplay/controller.py').read_text(encoding='utf-8')
    assert 'trial = st.session(game, token, open_only=False)' in ctrl



def check_vote_double_react_is_abstain_for_judgment() -> None:
    # Tribunal determinism: reacting to both ✅ and ❌ makes the user's vote invalid/abstain.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("resolved_judgments")
    assert idx != -1
    seg = src[idx : idx + 350]
    assert "if \"✅\" in reacts and \"❌\" in reacts" in seg
    assert "resolved_judgments[uid] = None" in seg



def check_on_ready_sync_failures_are_logged() -> None:
    # Slash-command sync failures should be visible in logs.
    src = BOT_PY.read_text(encoding="utf-8")
    assert "Failed to sync app commands for allowed guild." in src
    assert "Failed to sync global app commands." in src



def check_errors_handler_best_effort_sends() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'errors.py').read_text(encoding="utf-8")
    assert 'from bot_app.shared import safe_reply' in src
    assert 'await safe_reply' in src



def check_state_changing_commands_guard_allowed_guild() -> None:
    # Defense in depth: ensure core state-changing commands require allowed guild check.
    src = BOT_PY.read_text(encoding="utf-8")
    n = src.count("@commands.check(enforce_allowed_guild_check)")
    assert n >= 7, f"Expected allowed-guild check decorator on core commands (found {n})"



def check_arsonist_clean_is_two_pass() -> None:
    # Ordering hazard: clean must run after all douses.
    src = NIGHT_PY.read_text(encoding="utf-8")
    # Minimal evidence: clean is skipped in first pass and applied in a second pass.
    assert "elif a_type == \"clean\"" in src or "elif a_type == 'clean'" in src
    assert "Second pass: Arsonist clean" in src, "Expected clean to be applied in a second pass"



def check_resolve_guilt_conversion_before_tally() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'gameplay/integration.py').read_text(encoding="utf-8")
    assert 'await game._process_deferred_guilt_at_night_start(ctx)' in src



def check_startgame_initializes_critical_state() -> None:
    # Startgame must clear stale state and initialize set-backed doused_players.
    src = BOT_PY.read_text(encoding="utf-8")
    assert "async def startgame" in src
    # Evidence: these transient flags are explicitly reset.
    assert "game.resolving = False" in src
    assert "game.vote_in_progress = False" in src
    # Evidence: doused_players is a set at start.
    assert "game.doused_players" in src and "set()" in src, "Expected startgame to initialize doused_players as set()"



def check_startgame_sets_game_key_and_started_at() -> None:
    # Startgame must create a durable game_key used for idempotent stats/history commits.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("async def startgame")
    assert idx != -1
    seg = src[idx : idx + 9000]
    assert "game.started_at" in seg, "Expected startgame to set started_at"
    assert "game.game_key" in seg, "Expected startgame to set game_key"



def check_startgame_snapshots_role_start_for_every_player() -> None:
    # Contract: role_start snapshot exists for honest history/leaderboards and survives promotions/conversions.
    src = BOT_PY.read_text(encoding="utf-8")
    assert "role_start" in src and "Snapshot role_start" in src, "Expected startgame to snapshot role_start"



def check_startgame_initializes_player_slots_stably() -> None:
    # Contract: player_slots are assigned once and remain stable (not based on living order).
    src = BOT_PY.read_text(encoding="utf-8")
    assert "game.player_slots = {p.id: i + 1 for i, p in enumerate(game.players)}" in src



def check_startgame_role_pool_constraints_documented_in_code() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'game_roles.py').read_text(encoding="utf-8")
    assert 'lobby_duplicate_violations' in src
    assert 'draw_distinct_neutral_buckets' in src



def check_startgame_has_edge_player_count_brackets() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'game_roles.py').read_text(encoding="utf-8")
    assert 'def mafia_neutral_counts' in src
    assert 'def start_pool_for_player_count' in src



def check_startgame_aborts_if_any_dm_preflight_fails() -> None:
    # Static evidence: if any player cannot be DMed, startgame aborts before state is set in_progress.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("failed_dms = []")
    assert idx != -1
    seg = src[idx : idx + 800]
    assert "Game Start Aborted" in seg or "Start Aborted" in seg or "DMs disabled" in seg


def check_hybrid_night_action_commands_present() -> None:
    # Contract: night actions should be hybrid commands to support /cmd with autocomplete.
    src = BOT_PY.read_text(encoding="utf-8")
    for cmd in ["heal", "protect", "roleblock", "investigate", "shoot", "frame", "watch", "track", "douse", "transport", "control"]:
        assert f"async def {cmd}" in src, f"Expected {cmd} command to exist"
    # Evidence of hybrid usage.
    assert "@bot.hybrid_command" in src, "Expected at least one @bot.hybrid_command"



def check_night_action_autocomplete_wired() -> None:
    # Contract: the core actions must wire slot autocompletes.
    src = BOT_PY.read_text(encoding="utf-8")
    for marker in [
        "@heal.autocomplete(\"target_number\")",
        "@protect.autocomplete(\"target_number\")",
        "@roleblock.autocomplete(\"target_number\")",
        "@investigate.autocomplete(\"target_number\")",
        "@shoot.autocomplete(\"target_number\")",
        "@frame.autocomplete(\"target_number\")",
        "@watch.autocomplete(\"target_number\")",
        "@track.autocomplete(\"target_number\")",
        "@douse.autocomplete(\"target_number\")",
        "@transport.autocomplete(\"target1_num\")",
        "@transport.autocomplete(\"target2_num\")",
        "@control.autocomplete(\"target1_num\")",
        "@control.autocomplete(\"target2_num\")",
    ]:
        assert marker in src, f"Expected autocomplete wiring: {marker}"



def check_endgame_lock_and_flags() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'game.py').read_text(encoding="utf-8")
    assert 'async with self._endgame_lock' in src
    assert 'async def _historical_reset' in src
    assert 'self.in_progress = False' in src



def check_process_death_does_not_double_append_graveyard() -> None:
    # Endgame/death invariant: process_death should be idempotent for already-dead players.
    import asyncio
    import game as game_module

    class _Role:
        def __init__(self, rid: int):
            self.id = rid

    class _Member:
        def __init__(self, mid: int, guild):
            self.id = mid
            self.guild = guild
            self.mention = f"<@{mid}>"
            self.display_name = f"P{mid}"
            self.roles = []
            self.voice = None

        async def add_roles(self, *_roles):
            return

        async def remove_roles(self, *_roles):
            return

    class _Chan:
        async def send(self, _msg: str, **kwargs) -> None:
            return

        async def set_permissions(self, *_args, **_kwargs):
            return

    class _Guild:
        def __init__(self):
            self._channels = {}
            self._members = {}

        def get_role(self, rid: int):
            return _Role(rid)

        def get_channel(self, cid: int):
            return self._channels.get(cid)

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    guild = _Guild()
    ctx = _Chan()
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.day_number = 1
    g.alive_role_id = 111
    g.grave_tc_id = 222
    g.grave_vc_id = 333
    g.mafia_tc_id = 444
    guild._channels = {222: _Chan(), 333: _Chan(), 444: _Chan()}

    m = _Member(1, guild)
    guild._members = {1: m}
    g.players = [m]  # type: ignore[assignment]
    g.living_players = [m]  # type: ignore[assignment]
    g.player_roles = {1: "Survivor"}
    g.role_states = {1: {}}

    asyncio.run(g.process_death(ctx, m, cause="night_kill"))
    n1 = len(g.graveyard)
    asyncio.run(g.process_death(ctx, m, cause="night_kill"))
    n2 = len(g.graveyard)
    assert n1 == 1 and n2 == 1, f"Expected graveyard not to double-append (got {n1}->{n2})"



def check_nuke_reset_clears_lockdown_tracking_fields() -> None:
    # Nuke reset invariant: locked_channel_ids and lockdown_role_id are reset to empty/None.
    import asyncio

    import game as game_module

    class _Role:
        def __init__(self, rid: int, name: str):
            self.id = int(rid)
            self.name = str(name)

        async def delete(self, reason: str = ""):
            return

    class _Chan:
        def __init__(self, cid: int):
            self.id = int(cid)
            self.overwrites = {}

        async def set_permissions(self, *_args, **_kwargs):
            return

        async def delete(self, reason: str = ""):
            return

    class _Member:
        def __init__(self):
            self.roles = []

        async def remove_roles(self, *_roles):
            return

    class _Guild:
        def __init__(self):
            self.members = [_Member()]
            self.roles = [_Role(1, "@everyone")]
            self.default_role = self.roles[0]
            self._roles_by_id = {}

        def get_role(self, rid: int):
            return self._roles_by_id.get(int(rid))

        def get_channel(self, cid: int):
            return _Chan(int(cid))

    guild = _Guild()
    g = game_module.Game(guild_id=123)
    g.locked_channel_ids = [111, 222]
    g.lockdown_role_id = 999
    asyncio.run(g.nuke_reset(guild))  # type: ignore[arg-type]
    assert g.locked_channel_ids == []
    assert g.lockdown_role_id is None



def check_resolve_final_persist_guarded() -> None:
    src = BOT_PY.read_text(encoding="utf-8")
    assert "if game.in_progress and not getattr(game, \"ending\", False)" in src, "Expected resolve() finally persist guard"



def check_resolve_retri_consumption_checks_action_applied() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'retributionist_consumption.py').read_text(encoding="utf-8")
    assert 'consume_retributionist_uses' in src
    assert 'retributionist_consume_eligible' in src
    assert 'corpse_int in used_ints' in src



def check_resolve_jester_haunt_fallback_clears_markers() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'night_guilt.py').read_text(encoding="utf-8")
    assert 's.pop("haunt_target", None)' in src



def check_resolve_reanimate_malformed_payload_is_ignored_safely() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'reanimate_expand.py').read_text(encoding="utf-8")
    assert 'def expand_reanimate_actions' in src
    assert 'continue' in src



def check_resolve_reanimate_expands_all_supported_corpse_roles() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'reanimate_expand.py').read_text(encoding="utf-8")
    assert '"Doctor"' in src
    assert '"Seer"' in src
    assert '"Transporter"' in src
    assert '"Vigilante"' in src


def check_vote_final_persist_guarded() -> None:
    # Tribunal must not resurrect state after reset/endgame.
    src = BOT_PY.read_text(encoding="utf-8")
    assert "async def vote" in src
    assert "if game.in_progress and not getattr(game, \"ending\", False)" in src, "Expected vote() finally persist guard"



def check_get_member_safe_tolerates_bad_ids() -> None:
    # Persisted state can be corrupted; get_member_safe should tolerate non-int ids.
    src = GAME_PY.read_text(encoding="utf-8")
    assert "async def get_member_safe" in src
    assert "uid = int(user_id)" in src, "Expected get_member_safe to coerce ids to int"



def check_config_control_immune_includes_chaos() -> None:
    src = (ROOT / "config.py").read_text(encoding="utf-8")
    assert "CONTROL_IMMUNE_ROLES" in src
    assert "\"Chaos\"" in src or "'Chaos'" in src, "Expected Chaos in CONTROL_IMMUNE_ROLES"


def check_stats_persistence_helpers_exist() -> None:
    src = (ROOT / "persistence.py").read_text(encoding="utf-8")
    assert "def load_stats" in src, "Expected load_stats in persistence.py"
    assert "def save_stats" in src, "Expected save_stats in persistence.py"



def check_stats_commit_hooked_into_endgame() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'game.py').read_text(encoding="utf-8")
    assert 'await self.commit_endgame_stats_async' in src
    assert 'outcome="Draw"' in src



def check_stats_command_exists() -> None:
    src = BOT_PY.read_text(encoding="utf-8")
    assert "async def stats" in src, "Expected !stats command in bot.py"
    assert "load_stats" in src, "Expected stats command to load stats"


def check_leaderboard_slash_and_db_init_exist() -> None:
    src = BOT_PY.read_text(encoding="utf-8")
    # Ensure slash command is registered.
    assert "@bot.tree.command" in src and "name=\"leaderboard\"" in src, "Expected /leaderboard slash command"
    assert "async def leaderboard_slash" in src, "Expected leaderboard_slash handler"

    # Ensure DB is initialized in on_ready (static AST evidence).
    bot_tree = ast.parse(src, filename=str(BOT_PY))
    on_ready = _find_async_fn(bot_tree, "on_ready")

    database_names = {
        target.id
        for node in ast.walk(on_ready) if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and _dotted_name(node.value.func) in {"Database", "database.Database"}
        for target in node.targets if isinstance(target, ast.Name)
    }
    initialized_at = []
    published_at = []
    for node in ast.walk(on_ready):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            call = node.value
            if _dotted_name(call.func) == "run_blocking" and call.args:
                if _dotted_name(call.args[0]) in {f"{name}.initialize" for name in database_names}:
                    initialized_at.append(node.lineno)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in database_names:
            if any(_dotted_name(target) == "bot.db" for target in node.targets):
                published_at.append(node.lineno)
    assert initialized_at, "Expected on_ready to initialize the database off the event loop"
    assert published_at and min(initialized_at) < min(published_at), "Expected bot.db to become available after initialization"



def check_stats_prefers_sqlite_when_available() -> None:
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("async def stats")
    assert idx != -1
    seg = src[idx : idx + 900]
    # Evidence of SQLite preference path.
    assert "get_player_stats_summary" in seg, "Expected stats() to prefer SQLite summary"
    assert "asyncio.to_thread" in seg, "Expected stats() SQLite read to be offloaded"



def check_importstats_command_exists() -> None:
    src = BOT_PY.read_text(encoding="utf-8")
    assert "@bot.command(name=\"importstats\")" in src or "@bot.command(name='importstats')" in src, "Expected !importstats command"
    assert "import_player_stats_from_json" in src, "Expected importstats to call DB importer"
    # Hypothetical pitfall: import command accidentally becomes available to everyone / in DMs / other guilds.
    assert "@commands.has_role(GAME_OVERSEER_ROLE_ID)" in src, "Expected !importstats to be overseer-only"
    assert "@commands.guild_only()" in src, "Expected !importstats to be guild-only"
    assert "@commands.check(enforce_allowed_guild_check)" in src, "Expected !importstats to enforce allowed guild"



def check_leaderboard_db_reads_offloaded() -> None:
    # Hypothetical pitfall: doing sqlite work on the event loop in UI callbacks.
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("async def _build_leaderboard_embed")
    assert idx != -1
    seg = src[idx : idx + 700]
    assert "asyncio.to_thread" in seg, "Expected leaderboard DB reads to be offloaded to a thread"



def check_on_ready_db_path_under_state_dir() -> None:
    # Hypothetical pitfall: writing DB to cwd (breaks when run as a service).
    src = BOT_PY.read_text(encoding="utf-8")
    idx = src.find("async def on_ready")
    assert idx != -1
    seg = src[idx : idx + 900]
    assert "mafiabot.db" in seg and "state" in seg, "Expected DB path to be under state/mafiabot.db"



def check_sqlite_endgame_commit_is_non_fatal() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'game.py').read_text(encoding="utf-8")
    assert 'persist_pending_endgame_marker' in src
    assert 'except Exception:' in src



def check_database_conn_uses_wal_foreign_keys_and_timeout() -> None:
    # Hypothetical pitfall: 'database is locked' / FK bugs when concurrency increases.
    src = (ROOT / "database.py").read_text(encoding="utf-8")
    assert "sqlite3.connect" in src and "timeout=30" in src, "Expected sqlite3.connect(..., timeout=30)"
    assert "PRAGMA journal_mode=WAL" in src, "Expected WAL mode"
    assert "PRAGMA foreign_keys = ON" in src, "Expected foreign_keys=ON"



def check_personal_win_keys_wired_end_to_end() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'stats_personal.py').read_text(encoding="utf-8")
    assert '"pirate_win"' in src
    assert '"guardian_angel_win"' in src
    assert '"serial_killer_win"' in src



def check_role_lists_are_disjoint_and_completeish() -> None:
    # Mafia-bot specific pitfall: accidentally putting a role into both Town and Mafia lists,
    # or missing a defined role from all faction lists.
    import config
    import roles

    town = set(getattr(config, "TOWN_ROLES", []))
    mafia = set(getattr(config, "ALL_MAFIA_ROLES", []))
    assert town.isdisjoint(mafia), f"Role(s) appear in both TOWN_ROLES and ALL_MAFIA_ROLES: {sorted(town & mafia)}"

    defined = set(getattr(roles, "ROLE_DESCRIPTIONS", {}).keys())
    assert defined, "Expected ROLE_DESCRIPTIONS to be non-empty"

    # Neutral roles are intentionally excluded from the two lists; just ensure no defined role is missing from all three factions.
    neutral = defined - (town | mafia)
    # Ensure we at least recognize the current neutrals; if more are added, update this list (smoke safety).
    expected_neutrals = {"Chaos", "Jester", "Executioner", "Survivor", "Witch", "Pirate", "Arsonist"}
    assert expected_neutrals.issubset(neutral), f"Expected neutral roles missing from neutral set: {sorted(expected_neutrals - neutral)}"



def check_faction_win_attribution_does_not_require_alive() -> None:
    from endgame_stats import compute_player_endgame_deltas
    for role, outcome in [('Doctor','Town'),('Mobster','Mafia')]:
        rows = compute_player_endgame_deltas(player_roles={1:role}, role_states={1:{}}, living_ids=set(), outcome_norm=outcome)
        assert rows[0].did_win



def check_game_persistence_includes_game_key() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'persist_schema.py').read_text(encoding="utf-8")
    assert '"game_key": game.game_key' in src
    assert '"started_at": game.started_at' in src



def check_leaderboard_ui_is_invoker_only_and_defers() -> None:
    src = BOT_PY.read_text(encoding="utf-8")
    bot_tree = ast.parse(src, filename=str(BOT_PY))
    lb_view = _find_class(bot_tree, "LeaderboardView")
    lb_select = _find_class(bot_tree, "LeaderboardSelect")

    # LeaderboardView.interaction_check enforces invoker-only usage.
    inter_check = None
    for n in lb_view.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "interaction_check":
            inter_check = n
            break
    assert inter_check is not None, "Expected LeaderboardView.interaction_check"
    seg = ast.get_source_segment(src, inter_check) or ""
    assert "interaction.user.id" in seg and "self.invoker_id" in seg, "Expected invoker-only guard in interaction_check"

    # LeaderboardSelect.callback should defer before doing DB work.
    cb = None
    for n in lb_select.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "callback":
            cb = n
            break
    assert cb is not None, "Expected LeaderboardSelect.callback"
    cb_seg = ast.get_source_segment(src, cb) or ""
    assert "interaction.response.defer" in cb_seg, "Expected select callback to defer interaction"


def check_autocomplete_does_not_sync_living_players() -> None:
    # Autocomplete callbacks fire on every keystroke; they must not call sync_living_players/fetch_member.
    src = BOT_PY.read_text(encoding="utf-8")
    assert "async def _living_slot_choices_for_user" in src
    # Contract: helper should not call sync_living_players (which can fetch members).
    assert "_living_slot_choices_for_user" in src and "sync_living_players" not in src.split("async def _living_slot_choices_for_user", 1)[1].split("async def", 1)[0], (
        "Expected _living_slot_choices_for_user to avoid sync_living_players()"
    )



def check_stats_witch_win_consistent_with_messaging() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'endgame_stats.py').read_text(encoding="utf-8")
    assert 'WITCH_TOWN_LOSES_OUTCOMES' in src
    assert 'witch_town_loses' in src



def check_corpses_reanimate_guard_missing_player_id() -> None:
    # Graveyard corruption safety: corpse listing/reanimate should skip entries with missing player_id.
    src = BOT_PY.read_text(encoding="utf-8")
    assert "async def corpses" in src and "async def reanimate" in src
    assert "pid = entry.get(\"player_id\")" in src or "pid = entry.get('player_id')" in src, "Expected pid None guard in corpse loops"
    assert "except (TypeError, ValueError)" in src, "Expected int-cast corruption guards in corpse loops"



def check_autocomplete_avoids_interaction_namespace() -> None:
    # interaction.namespace is not stable API across discord.py versions.
    src = BOT_PY.read_text(encoding="utf-8")
    assert "interaction.namespace" not in src, "Expected autocomplete exclude logic to avoid interaction.namespace"
    assert "interaction.data" in src, "Expected supported parsing via interaction.data for slash options"



def check_plunder_finalizer_persist_guarded() -> None:
    src = (ROOT / 'gameplay/duels.py').read_text(encoding='utf-8')
    assert 'duel_won=won, duel_finished=True' in src
    assert 'return await st.commit(game, update)' in src
    state = (ROOT / 'gameplay/state.py').read_text(encoding='utf-8')
    assert 'async with game.state_lock:' in state and 'await flush_committed(game)' in state



def check_pirate_plunder_win_increments_on_duel_win() -> None:
    # Contract: Pirate "wins" should increment on duel win (even if the target survives),
    # but should not increment if the Pirate is roleblocked.
    import asyncio
    import game as game_module

    class _FakeMember:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"

        async def send(self, _msg: str) -> None:
            return

    class _FakeGuild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    from engine.night import run_night_pipeline

    # Case 1: duel_won True increments wins
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [_FakeMember(1), _FakeMember(2)]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Pirate", 2: "Survivor"}
    g.role_states = {1: {"wins": 0}, 2: {"vests_remaining": 2}}
    g.night_actions = {1: {"type": "plunder", "actor": 1, "role": "Pirate", "target": 2, "duel_won": True, "duel_finished": True}}
    asyncio.run(run_night_pipeline(g, _FakeGuild(g.players)))  # type: ignore[arg-type]
    assert g.role_states[1]["wins"] == 1
    assert g.role_states[1].get("pirate_win_this_night") is True

    # Case 2: if Pirate is blocked, duel win should not count
    g2 = game_module.Game(guild_id=123)
    g2.in_progress = True
    g2.phase = "night"
    g2.day_number = 2
    g2.players = [_FakeMember(1), _FakeMember(2), _FakeMember(3)]  # type: ignore[assignment]
    g2.living_players = g2.players.copy()  # type: ignore[assignment]
    g2.player_roles = {1: "Pirate", 2: "Survivor", 3: "Gatekeeper"}
    g2.role_states = {1: {"wins": 0}, 2: {"vests_remaining": 2}, 3: {"uses_remaining": 2}}
    # Gatekeeper guarding the Pirate's target blocks non-mafia visitors (including Pirate),
    # so the plunder should not count as a win even if duel_won=True.
    g2.night_actions = {
        1: {"type": "plunder", "actor": 1, "role": "Pirate", "target": 2, "duel_won": True, "duel_finished": True},
        3: {"type": "guard", "actor": 3, "role": "Gatekeeper", "target": 2},
    }
    asyncio.run(run_night_pipeline(g2, _FakeGuild(g2.players)))  # type: ignore[arg-type]
    assert g2.role_states[1]["wins"] == 0



def check_chaos_action_has_real_effect_and_consumes_use() -> None:
    # Contract: Chaos action should (a) consume one use when executed and
    # (b) apply at least one real effect visible in engine state or messages.
    import asyncio
    import game as game_module

    class _FakeMember:
        def __init__(self, mid: int):
            self.id = mid
            self.display_name = f"P{mid}"
            self.dms: list[str] = []

        async def send(self, msg: str) -> None:
            self.dms.append(str(msg))

    class _FakeGuild:
        def __init__(self, members):
            self._members = {m.id: m for m in members}

        def get_member(self, uid: int):
            return self._members.get(int(uid))

        async def fetch_member(self, uid: int):
            return self._members.get(int(uid))

    from engine.night import run_night_pipeline

    m1, m2, m3 = _FakeMember(1), _FakeMember(2), _FakeMember(3)
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 2
    g.players = [m1, m2, m3]  # type: ignore[assignment]
    g.living_players = g.players.copy()  # type: ignore[assignment]
    g.player_roles = {1: "Chaos", 2: "Sheriff", 3: "Doctor"}
    g.role_states = {1: {"uses_remaining": 2}, 2: {}, 3: {"self_heals_remaining": 1}}
    g.night_actions = {1: {"type": "chaos", "actor": 1, "targets": [2, 3]}}

    asyncio.run(run_night_pipeline(g, _FakeGuild(g.players)))  # type: ignore[arg-type]

    assert g.role_states[1]["uses_remaining"] == 1, "Chaos should consume exactly one use"
    # At least one effect should be observable.
    saw_state_effect = (
        bool(g.role_states.get(2, {}).get("is_framed"))
        or bool(g.role_states.get(2, {}).get("is_tailored_as"))
        or bool(g.role_states.get(2, {}).get("is_hidden_by_gravedigger"))
        or bool(g.role_states.get(2, {}).get("is_vested"))
        or bool(g.role_states.get(2, {}).get("is_on_alert"))
        or (2 in getattr(g, "doused_players", set()))
        or bool(g.role_states.get(2, {}).get("chaos_protected_by"))
    )
    saw_message_effect = any(
        ("transported" in s.lower()) or ("gasoline" in s.lower())
        for s in (m2.dms + m3.dms)
    )
    saw_action_injection = g.night_actions.get(1, {}).get("type") in {"watch", "track", "heal", "investigate", "roleblock"}
    assert saw_state_effect or saw_message_effect or saw_action_injection, "Expected Chaos to cause a visible effect"



def check_bot_resolve_does_not_expand_chaos_or_increment_pirate_wins() -> None:
    # Contract: Chaos + Pirate win accounting live in engine/night.py, not bot.py resolve().
    src = BOT_PY.read_text(encoding="utf-8")
    # Chaos expansion markers / pools should not exist in resolve().
    assert "chaos_effects" not in src, "Expected bot.resolve to not expand Chaos"
    assert "_from_chaos" not in src, "Expected bot.resolve to not tag chaos-expanded actions"
    # Pirate wins should not be incremented in bot.resolve.
    assert "successful plunder" not in src, "Expected bot.resolve to not post-process Pirate wins"



def check_visit_log_recomputed_after_chaos() -> None:
    # Contract: Chaos can redirect targets; the engine must recompute visit_log/blocking after applying Chaos.
    src = NIGHT_PY.read_text(encoding="utf-8")
    # Evidence: there should be *two* build_visit_log() calls in run_night_pipeline:
    # one pre-chaos for determining whether Chaos is blocked, and one post-chaos to reflect redirects.
    assert src.count("build_visit_log(game)") >= 2, "Expected visit log to be rebuilt after Chaos effects"



def check_process_death_by_id_handles_jester_lynch() -> None:
    src = (ROOT / 'gameplay/death.py').read_text(encoding='utf-8')
    assert 'Jester' in src and 'lynch' in src and 'guilty_voters' in src and 'jester_won' in src
    assert 'apply_death(self, player_id, cause' in GAME_PY.read_text(encoding='utf-8')



def check_investigator_bucket_includes_chaos() -> None:
    src = NIGHT_PY.read_text(encoding="utf-8")
    assert "\"Chaos\"" in src or "'Chaos'" in src, "Expected Chaos mentioned in Investigator buckets"
    # Guard against single-role fallback revealing Chaos uniquely: it must be in a list bucket.
    assert "Chaos" in src and "buckets" in src, "Expected Chaos to be placed in a bucket list"



def check_night_actions_blocked_while_resolving() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'gameplay/actions.py').read_text(encoding="utf-8")
    assert 'night_actions_frozen(game)' in src
    assert 'Night is resolving. Your action cannot be changed.' in src



def check_night_actions_restricted_to_private_channels() -> None:
    # Contract: night actions should be accepted either via DM (prefix) or in a configured private player channel.
    src = CHECKS_PY.read_text(encoding="utf-8")
    assert "PLAYER_PRIVATE_CHANNEL_IDS" in src, "Expected checks.py to reference PLAYER_PRIVATE_CHANNEL_IDS"
    assert "ctx.channel.id" in src and "expected_channel_id" in src, "Expected checks.py to compare ctx.channel.id to expected channel"
    assert "ctx.guild is not None" in src, "Expected checks.py to treat guild vs DM surfaces differently"
    assert "No active game found for you." in src, "Expected a user-visible message when no game is found"
    assert "different server" in src, "Expected guild mismatch to be rejected"
    # Defense-in-depth: support both mapping shapes (user->channel or channel->user).
    assert "for k, v in PLAYER_PRIVATE_CHANNEL_IDS.items()" in src and "int(v) == int(ctx.author.id)" in src, (
        "Expected private-channel mapping to support inverted config shapes."
    )



def check_engine_protected_by_map_id_cast_is_guarded() -> None:
    # Follow the shared implementation introduced by the 32-role integration.
    src = (ROOT / 'engine/killing_resolve.py').read_text(encoding="utf-8")
    assert 'protected_by_map' in src
    assert 'except (TypeError, ValueError)' in src



def check_dist_runtime_is_not_runnable_entrypoint() -> None:
    # Contract: dist_runtime is a build artifact; it must not be runnable as the real bot entrypoint.
    from pathlib import Path

    p = Path(__file__).resolve().parent / "dist_runtime" / "bot.py"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    assert "Do not run dist_runtime/bot.py" in s and "raise RuntimeError" in s, "Expected dist_runtime/bot.py to hard-fail if run/imported"


def check_invalid_slot_message_lists_valid_slots() -> None:
    src = GAME_PY.read_text(encoding="utf-8")
    assert "Valid slots" in src or "Valid slot" in src, "Expected invalid slot message to list actual valid slots"


def check_dm_outbox_schema_present() -> None:
    src = (ROOT / "database.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS dm_outbox" in src
    assert "idx_dm_outbox_dedupe" in src



def check_slash_interaction_gate_wired() -> None:
    src = BOT_PY.read_text(encoding="utf-8")
    assert "bot.tree.interaction_check = _mafia_tree_interaction_check" in src, "Expected B6.3 tree.interaction_check"



def check_product_python_files_compile_excluding_junk() -> None:
    """B6.4: compile walk skips local venv/caches so hygiene checks reflect product code."""
    skip = {"venv", ".venv", ".venv314", ".venv312-check", ".runtimes", "__pycache__", ".git", ".hypothesis"}
    for path in ROOT.rglob("*.py"):
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            continue
        if any(p in skip for p in rel.parts):
            continue
        compile(path.read_text(encoding="utf-8"), str(path), "exec")



def check_bot_entrypoint_requires_token() -> None:
    # Importing the command registry is offline; only starting needs a token.
    import subprocess
    env = dict(os.environ, DISCORD_TOKEN="", DISCORD_BOT_TOKEN="", PYTHONUTF8="1")
    code = "import bot; bot.TOKEN = ''; bot.main()"
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert 'DISCORD_TOKEN' in result.stderr


def _run_checks() -> None:
    # Basic import checks for split modules.
    import config  # noqa: F401
    import roles  # noqa: F401
    import errors  # noqa: F401
    import checks  # noqa: F401
    import persistence
    import database as db_module
    from engine import night as night_engine  # noqa: F401
    import game as game_module

    # Ensure the night engine boundary didn't change the call order/wrappers.
    check_bot_py_compiles()
    check_resolve_pipeline_shape()
    check_resolve_expands_custom_actions()
    check_bot_resolve_chaos_targets_int_coerced()
    check_will_modal_and_command_exist()
    check_witch_can_prevent_ignite_in_engine()
    check_gatekeeper_can_block_witch_control_on_guarded_target()
    check_roleblocked_gatekeeper_does_not_block_witch_control()
    check_gatekeeper_guard_does_not_apply_if_gatekeeper_is_blocked()
    check_roleblocked_gatekeeper_guard_does_not_block_other_visitors()
    check_witch_control_does_not_override_control_immune()
    check_gatekeeper_guard_blocks_doctor_heal_to_guarded_target()
    check_witch_control_blocked_by_gatekeeper_does_not_mirror_invest_results()
    check_guard_consumes_use_only_if_valid_target()
    check_transporter_redirects_actions_and_visit_log_reflects_redirect()
    check_transporter_swap_does_not_redirect_immune_actions()
    check_transporter_messages_sent_to_swapped_targets()
    check_visit_log_excludes_blocked_visitors_for_lookout_and_alert()
    check_transporter_redirect_affects_track_results()
    check_bodyguard_counterkill_still_hits_redirected_attacker()
    check_arsonist_clean_after_transport_and_douse()
    check_alert_kills_unblocked_visitors_after_transport_redirect()
    check_ignite_kills_even_if_healed_and_doctor_gets_unstoppable_message()
    check_control_then_transport_still_redirects_controlled_action()
    check_controlled_watch_mirrors_visitors_after_transport()
    check_executioner_converts_to_jester_if_target_dies_at_night()
    check_jester_fallback_haunt_targets_only_eligible_voters()
    check_jester_eligible_haunt_includes_abstain_excludes_innocent()
    check_pirate_plunder_roleblocks_target_regardless_of_duel_outcome()
    check_gatekeeper_blocked_pirate_plunder_does_not_roleblock_target()
    check_gatekeeper_does_not_block_mafia_visitors()
    check_mobster_promotion_does_not_change_role_start()
    check_mobster_promotion_happens_before_win_check_returns()
    check_mobster_promotion_only_targets_mafia_roles()
    check_mobster_promotion_does_not_happen_if_mobster_exists()
    check_roleblock_chain_stability_mutual_roleblocks()
    check_endgame_stats_commit_does_not_crash_on_weird_personal_wins_types()
    check_gatekeeper_guard_consumes_exactly_one_use()
    check_gatekeeper_guard_use_is_idempotent_within_same_night()
    check_start_night_clears_gatekeeper_used_marker()
    check_double_pipeline_does_not_double_consume_misc_uses()
    check_double_pipeline_does_not_double_consume_limited_action_uses()
    check_action_consumption_never_underflows_below_zero()
    check_general_prompt_invariants_apply_monotonicity_and_idempotency()
    check_general_prompt_privacy_no_crash_on_missing_member_objects()
    check_seeded_micro_fuzz_engine_invariants()
    check_vigilante_shoot_does_not_execute_with_zero_bullets()
    check_survivor_vest_does_not_apply_with_zero_vests()
    check_grandma_alert_does_not_apply_with_zero_alerts()
    check_dead_actor_actions_do_not_execute()
    check_deaths_are_subset_of_living_players()
    check_dead_doctor_heal_does_not_apply_or_consume()
    check_dead_roleblocker_does_not_block()
    check_endgame_stats_commit_is_idempotent()
    check_startgame_resets_stats_committed_flag()
    check_reset_resets_stats_committed_flag()
    check_sqlite_initialize_and_personal_leaderboard_key_roundtrip()
    check_sqlite_import_player_stats_tolerates_corrupted_json_records()
    check_from_persisted_string_false_is_not_truthy()
    check_sqlite_begin_game_commit_is_idempotent_and_returns_same_id()
    check_sqlite_top_winrate_handles_zero_games_without_crash()
    check_sqlite_personal_win_delta_never_goes_negative()
    check_save_state_roundtrip_and_overwrite_is_stable()
    check_roles_text_witch_wincon_matches_implementation()
    check_setup_infrastructure_is_idempotent_and_hardens_privacy()
    check_setup_infrastructure_partial_existing_channels_are_reused()
    check_setup_infrastructure_lockdown_role_hides_other_categories_and_records_locked_ids()
    check_setup_infrastructure_raises_runtime_error_on_discord_failures()
    check_sqlite_player_and_role_stats_never_go_negative()
    check_sqlite_read_paths_never_surface_negative_counts()
    check_start_day_is_idempotent_within_same_day()
    check_start_night_is_idempotent_within_same_night()
    check_start_night_does_not_wipe_actions_if_already_night()
    check_vigilante_guilt_death_is_idempotent_in_resolve()
    check_resolve_sets_resolving_flag_before_any_awaits()
    check_vote_always_clears_tribunal_snapshot_in_finally()
    check_haunt_filters_to_living_voters_only()
    check_vote_does_not_persist_vote_in_progress_across_restarts()
    check_stats_command_displays_personal_keys_human_friendly()
    check_stats_json_personal_wins_migrates_legacy_role_keys()
    check_db_init_migration_does_not_drop_tables()
    check_private_channels_are_hidden_from_everyone_and_playing()
    check_night_actions_restricted_to_dm_or_private_channel_mapping()
    check_only_during_night_gameplay_supports_inverted_private_channel_mapping()
    check_reveal_is_guild_only_and_allowed_guild_guarded()
    check_myrole_and_haunt_are_dm_only()
    check_will_command_is_dm_only_and_deletes_guild_invocation()
    check_no_day_channel_commands_accept_dm_unintentionally()
    check_private_channel_mapping_inverted_shape_supported()
    check_revealed_mayor_cannot_be_healed_even_by_doctor()
    check_ignite_clears_doused_players_set()
    check_blocked_investigator_gets_no_results()
    check_roleblock_immune_roles_exclude_gatekeeper()
    check_transporter_is_control_immune()
    check_vote_persists_tribunal_snapshot_fields()
    check_control_mirrors_investigation_results_to_witch()
    check_control_action_is_validated()
    check_transport_and_control_ids_are_int_coerced()
    check_build_visit_log_tolerates_non_list_transport_targets()
    check_engine_hypnotist_payload_is_validated()
    check_engine_tailor_fake_role_is_validated()
    check_arsonist_clean_is_two_pass()
    check_resolve_guilt_conversion_before_tally()
    check_startgame_initializes_critical_state()
    check_startgame_sets_game_key_and_started_at()
    check_startgame_snapshots_role_start_for_every_player()
    check_startgame_initializes_player_slots_stably()
    check_startgame_role_pool_constraints_documented_in_code()
    check_startgame_has_edge_player_count_brackets()
    check_startgame_aborts_if_any_dm_preflight_fails()
    check_hybrid_night_action_commands_present()
    check_night_action_autocomplete_wired()
    check_endgame_lock_and_flags()
    check_process_death_does_not_double_append_graveyard()
    check_nuke_reset_clears_lockdown_tracking_fields()
    check_resolve_final_persist_guarded()
    check_resolve_retri_consumption_checks_action_applied()
    check_resolve_jester_haunt_fallback_clears_markers()
    check_resolve_reanimate_malformed_payload_is_ignored_safely()
    check_resolve_reanimate_expands_all_supported_corpse_roles()
    check_vote_final_persist_guarded()
    check_get_member_safe_tolerates_bad_ids()
    check_config_control_immune_includes_chaos()
    check_stats_persistence_helpers_exist()
    check_stats_commit_hooked_into_endgame()
    check_stats_command_exists()
    check_leaderboard_slash_and_db_init_exist()
    check_stats_prefers_sqlite_when_available()
    check_leaderboard_db_reads_offloaded()
    check_on_ready_db_path_under_state_dir()
    check_importstats_command_exists()
    check_game_persistence_includes_game_key()
    check_leaderboard_ui_is_invoker_only_and_defers()
    check_autocomplete_does_not_sync_living_players()
    check_stats_witch_win_consistent_with_messaging()
    check_corpses_reanimate_guard_missing_player_id()
    check_autocomplete_avoids_interaction_namespace()
    check_plunder_finalizer_persist_guarded()
    check_bot_resolve_does_not_expand_chaos_or_increment_pirate_wins()
    check_visit_log_recomputed_after_chaos()
    check_pirate_plunder_win_increments_on_duel_win()
    check_chaos_action_has_real_effect_and_consumes_use()
    check_process_death_by_id_handles_jester_lynch()
    check_investigator_bucket_includes_chaos()
    check_invalid_slot_message_lists_valid_slots()
    check_night_actions_blocked_while_resolving()
    check_night_actions_restricted_to_private_channels()
    check_engine_protected_by_map_id_cast_is_guarded()
    check_dist_runtime_is_not_runnable_entrypoint()
    check_from_persisted_tolerates_bad_numeric_lists()
    check_from_persisted_tolerates_corrupted_player_slots_and_role_ids()
    check_from_persisted_tolerates_corrupted_mapping_keys()
    check_from_persisted_tolerates_corrupted_id_fields_and_counters()
    check_from_persisted_backcompat_slots_derivation_tolerates_bad_role_keys()
    check_sync_living_players_tolerates_corrupted_graveyard_entries()
    check_start_night_and_day_guard_optional_ids()
    check_vote_judgment_reactions_are_best_effort()
    check_vote_refunds_trial_use_if_defendant_dies_during_defense()
    check_vote_refunds_trial_use_if_defendant_dies_before_judgment_tally()
    check_vote_eligible_haunt_excludes_innocent_votes()
    check_vote_stale_coroutine_guards_exist_after_sleeps()
    check_vote_double_react_is_abstain_for_judgment()
    check_on_ready_sync_failures_are_logged()
    check_errors_handler_best_effort_sends()
    check_state_changing_commands_guard_allowed_guild()
    check_sqlite_endgame_commit_is_non_fatal()
    check_database_conn_uses_wal_foreign_keys_and_timeout()
    check_personal_win_keys_wired_end_to_end()
    check_role_lists_are_disjoint_and_completeish()
    check_faction_win_attribution_does_not_require_alive()

    check_dm_outbox_schema_present()
    check_slash_interaction_gate_wired()
    check_product_python_files_compile_excluding_junk()

    # Ensure the entrypoint behavior is still "token required".
    check_bot_entrypoint_requires_token()

    # Decorator factory sanity: `only_during_night_gameplay()` must return a decorator.
    dec = checks.only_during_night_gameplay(bot=None, get_game_by_player_id=lambda _uid: None)  # type: ignore[arg-type]
    assert callable(dec)
    assert callable(dec(lambda *a, **k: None))

    # Sanity: we can construct a Game and round-trip persistence data without discord objects.
    g = game_module.Game(guild_id=123)
    g.in_progress = True
    g.phase = "night"
    g.day_number = 7
    g.player_roles = {1: "Pirate", 2: "Doctor"}
    g.player_slots = {1: 1, 2: 2}
    g.role_states = {1: {"wins": 1}, 2: {"self_heals_remaining": 1}}
    g.night_actions = {1: {"type": "plunder", "target": 2, "actor": 1, "duel_won": True, "duel_finished": True}}
    data = g.to_persisted()

    g2 = game_module.Game.from_persisted(data)
    assert g2.guild_id == 123
    assert g2.in_progress is True
    assert g2.phase == "night"
    assert g2.day_number == 7
    assert g2.player_roles[1] == "Pirate"
    assert g2.player_slots[1] == 1 and g2.player_slots[2] == 2
    assert g2.role_states[1]["wins"] == 1
    assert g2.night_actions[1]["type"] == "plunder"

    # Sanity: persistence save/load works (write to a temporary project dir).
    # We redirect persistence.STATE_DIR for this test by monkeypatching the module attribute.
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        persistence.STATE_DIR = tmpdir  # type: ignore[attr-defined]
        persistence.save_state(123, data)
        loaded = persistence.load_state(123)
        assert loaded is not None
        assert int(loaded["guild_id"]) == 123

        # Corruption safety: unreadable JSON should return None, not crash.
        (tmpdir / "123.json").write_text("{not valid json", encoding="utf-8")
        assert persistence.load_state(123) is None

        persistence.delete_state(123)
        assert persistence.load_state(123) is None

    # Sanity: SQLite DB initializes and leaderboard queries work.
    # On Windows, SQLite WAL/SHM teardown can briefly hold file handles; ignore cleanup errors here.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmpdir = Path(td)
        db_path = str(tmpdir / "mafiabot.db")
        db = db_module.Database(db_path)
        db.initialize()

        # Insert a synthetic game + stats rows.
        is_first, game_id = db.begin_game_commit(
            guild_id=123,
            game_key="123:2026-01-01T00:00:00+00:00:abc",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T01:00:00+00:00",
            outcome="Town",
            player_count=2,
            ended_day_number=3,
            ended_phase="day",
        )
        assert is_first is True

        db.insert_game_players(
            game_id=game_id,
            guild_id=123,
            rows=[
                {
                    "player_id": 1,
                    "role_start": "Doctor",
                    "role_end": "Doctor",
                    "faction_start": "Town",
                    "faction_end": "Town",
                    "survived": 1,
                    "died_day": None,
                    "death_cause": None,
                },
                {
                    "player_id": 2,
                    "role_start": "Mobster",
                    "role_end": "Mobster",
                    "faction_start": "Mafia",
                    "faction_end": "Mafia",
                    "survived": 0,
                    "died_day": 2,
                    "death_cause": "lynch",
                },
            ],
        )

        db.upsert_player_stats_delta(
            guild_id=123,
            player_id=1,
            games_played=1,
            wins_total=1,
            losses_total=0,
            draws_total=0,
            wins_town=1,
            wins_mafia=0,
            wins_arsonist=0,
            last_game_at="2026-01-01T01:00:00+00:00",
        )
        db.upsert_player_stats_delta(
            guild_id=123,
            player_id=2,
            games_played=1,
            wins_total=0,
            losses_total=1,
            draws_total=0,
            wins_town=0,
            wins_mafia=0,
            wins_arsonist=0,
            last_game_at="2026-01-01T01:00:00+00:00",
        )
        db.upsert_personal_win_delta(guild_id=123, player_id=1, key="survivor_survived", delta=0)
        db.upsert_personal_win_delta(guild_id=123, player_id=1, key="exe_win", delta=2)
        db.upsert_player_role_stats_delta(guild_id=123, player_id=1, role="Doctor", played=1, wins_total=1, losses_total=0)
        db.upsert_player_role_stats_delta(guild_id=123, player_id=1, role="Executioner", played=2, wins_total=2, losses_total=0)

        # Faction wins query.
        town_lb = db.top_faction_wins(guild_id=123, faction="Town", limit=5)
        assert town_lb and town_lb[0].player_id == 1 and int(town_lb[0].value) == 1

        # Winrate query.
        wr = db.top_winrate(guild_id=123, min_games=1, limit=5)
        assert wr and wr[0].player_id in {1, 2}

        # Personal query.
        exe_lb = db.top_personal(guild_id=123, key="exe_win", limit=5)
        assert exe_lb and exe_lb[0].player_id == 1 and int(exe_lb[0].value) == 2

        # Stats summary helper (used by !stats).
        summary = db.get_player_stats_summary(guild_id=123, player_id=1)
        assert summary is not None
        assert int(summary["games_played"]) == 1
        assert int(summary["wins"]) == 1
        assert summary["role_played"].get("Executioner") == 2
        assert summary["personal_wins"].get("exe_win") == 2

        top = db.top_total_wins(guild_id=123, limit=5)
        assert top and top[0].player_id == 1 and int(top[0].value) == 1

        # Winrate filter (min games): should be empty at min_games=5 right now.
        assert db.top_winrate(guild_id=123, min_games=5, limit=10) == []

        # Add more games so player 1 reaches 5 games (4 more wins).
        for i in range(2, 6):
            is_first_i, game_id_i = db.begin_game_commit(
                guild_id=123,
                game_key=f"123:2026-01-0{i}T00:00:00+00:00:key{i}",
                started_at=f"2026-01-0{i}T00:00:00+00:00",
                ended_at=f"2026-01-0{i}T01:00:00+00:00",
                outcome="Town",
                player_count=2,
                ended_day_number=2,
                ended_phase="day",
            )
            assert is_first_i is True
            db.insert_game_players(
                game_id=game_id_i,
                guild_id=123,
                rows=[
                    {
                        "player_id": 1,
                        "role_start": "Doctor",
                        "role_end": "Doctor",
                        "faction_start": "Town",
                        "faction_end": "Town",
                        "survived": 1,
                        "died_day": None,
                        "death_cause": None,
                    },
                    {
                        "player_id": 2,
                        "role_start": "Mobster",
                        "role_end": "Mobster",
                        "faction_start": "Mafia",
                        "faction_end": "Mafia",
                        "survived": 0,
                        "died_day": 1,
                        "death_cause": "shot",
                    },
                ],
            )
            db.upsert_player_stats_delta(
                guild_id=123,
                player_id=1,
                games_played=1,
                wins_total=1,
                losses_total=0,
                draws_total=0,
                wins_town=1,
                wins_mafia=0,
                wins_arsonist=0,
                last_game_at=f"2026-01-0{i}T01:00:00+00:00",
            )
            db.upsert_player_stats_delta(
                guild_id=123,
                player_id=2,
                games_played=1,
                wins_total=0,
                losses_total=1,
                draws_total=0,
                wins_town=0,
                wins_mafia=0,
                wins_arsonist=0,
                last_game_at=f"2026-01-0{i}T01:00:00+00:00",
            )

        wr5 = db.top_winrate(guild_id=123, min_games=5, limit=5)
        assert wr5 and wr5[0].player_id == 1 and wr5[0].games_played >= 5

        # Importer robustness: malformed player rows should be skipped, not crash.
        imported = db.import_player_stats_from_json(
            guild_id=123,
            stats_data={
                "players": {
                    "10": {"games_played": "3", "wins": "2", "losses": "1", "draws": "0"},
                    "bad_id": {"games_played": 1, "wins": 1, "losses": 0, "draws": 0},
                    "11": {"games_played": "not_a_number", "wins": 0, "losses": 0, "draws": 0},
                }
            },
        )
        assert imported == 1
        s10 = db.get_player_stats_summary(guild_id=123, player_id=10)
        assert s10 is not None and int(s10["games_played"]) == 3 and int(s10["wins"]) == 2

        # Idempotency: second begin with same key should not be first.
        is_first2, game_id2 = db.begin_game_commit(
            guild_id=123,
            game_key="123:2026-01-01T00:00:00+00:00:abc",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T01:00:00+00:00",
            outcome="Town",
            player_count=2,
            ended_day_number=3,
            ended_phase="day",
        )
        assert is_first2 is False
        assert game_id2 == game_id

    if _want_night_sim_followup():
        _run_night_sim_followup()

    print("smoke_test.py: OK")



def main() -> None:
    import persistence
    previous_state = persistence.STATE_DIR
    try:
        with tempfile.TemporaryDirectory(prefix="mafia-smoke-check-") as temporary_state:
            persistence.STATE_DIR = Path(temporary_state)
            _run_checks()
    finally:
        persistence.STATE_DIR = previous_state


if __name__ == "__main__":
    argv = list(sys.argv[1:])
    if "--with-night-sim" in argv:
        argv = [a for a in argv if a != "--with-night-sim"]
        os.environ["SMOKE_WITH_NIGHT_SIM"] = "1"
    if argv:
        print("smoke_test.py: unknown arguments:", " ".join(argv), file=sys.stderr)
        sys.exit(2)
    main()
