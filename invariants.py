from __future__ import annotations

"""
Shared runtime invariants for:
- smoke tests (targeted scenarios)
- sim/fuzz/property tests (high-volume bug finders)

Design goals:
- No Discord API calls; pure assertions over in-memory Game state and pipeline artifacts.
- Strict on internal consistency (types, shapes, monotonic counters).
- Broad enough to catch "unknown unknowns" without being flaky.
"""

from typing import Any, Dict, Iterable, Mapping


def _iter_ints(x: Any) -> Iterable[int]:
    if isinstance(x, (list, tuple, set, frozenset)):
        for v in x:
            if isinstance(v, int):
                yield v


def assert_game_runtime_sanity(game: Any) -> None:
    """
    Invariants that should hold at essentially all times during a running game.
    """
    living_players = list(getattr(game, "living_players", []) or [])
    living_ids = {getattr(p, "id", None) for p in living_players}
    living_ids_int = {int(pid) for pid in living_ids if isinstance(pid, int)}

    player_roles = getattr(game, "player_roles", {}) or {}
    if isinstance(player_roles, Mapping):
        missing_roles = [pid for pid in living_ids_int if pid not in player_roles]
        assert not missing_roles, f"Missing roles for living players: {missing_roles}"

    night_actions = getattr(game, "night_actions", {}) or {}
    if isinstance(night_actions, Mapping):
        assert all(isinstance(k, int) for k in night_actions.keys()), "night_actions keys must be int"


def assert_no_negative_counters(game: Any) -> None:
    """
    Generic, role-agnostic underflow guard.
    """
    role_states = getattr(game, "role_states", {}) or {}
    if not isinstance(role_states, Mapping):
        return
    for pid, state in role_states.items():
        if not isinstance(state, Mapping):
            continue
        for k, v in state.items():
            if not isinstance(v, int):
                continue
            # Common patterns in this codebase
            if (
                str(k).endswith("_remaining")
                or str(k) in {"uses_remaining", "shots_remaining"}
                or str(k).endswith("_remaining_today")
                or str(k).endswith("_remaining_this_night")
            ):
                assert v >= 0, f"Negative counter: player={pid} {k}={v}"


def assert_post_night_pipeline_invariants(game: Any, out: Dict[str, Any]) -> None:
    """
    Strong invariants that should hold after any night pipeline run.
    `out` is the artifact dict returned by scripts/sim_test.run_night_pipeline().
    """
    assert_game_runtime_sanity(game)
    assert_no_negative_counters(game)

    # Required keys/types
    for k in ["visit_log_raw", "visit_log", "blocked", "healed_by_map", "protected_by_map", "deaths"]:
        assert k in out, f"Missing pipeline artifact: {k}"

    visit_log_raw = out["visit_log_raw"]
    visit_log = out["visit_log"]
    blocked = out["blocked"]
    deaths = out["deaths"]

    assert isinstance(visit_log_raw, dict)
    assert isinstance(visit_log, dict)
    assert all(isinstance(k, int) for k in visit_log_raw.keys()), "visit_log_raw keys must be int"
    assert all(isinstance(k, int) for k in visit_log.keys()), "visit_log keys must be int"
    assert all(isinstance(v, list) for v in visit_log_raw.values()), "visit_log_raw values must be lists"
    assert all(isinstance(v, list) for v in visit_log.values()), "visit_log values must be lists"
    assert all(isinstance(x, int) for x in blocked), "blocked entries must be int"
    assert all(isinstance(x, int) for x in deaths), "deaths entries must be int"

    # Effective visit log semantics: blocked visitors should not appear in effective visit log.
    blocked_set = set(_iter_ints(blocked))
    for _t, visitors in visit_log.items():
        assert all(v not in blocked_set for v in visitors if isinstance(v, int)), "blocked visitor leaked into visit_log"

    # Deaths should be subset of living at start (best-effort: infer from game.living_players)
    living_ids = {int(getattr(p, "id")) for p in getattr(game, "living_players", []) or [] if hasattr(p, "id")}
    if living_ids:
        assert set(_iter_ints(deaths)).issubset(living_ids), f"Deaths not subset of living: deaths={deaths} living={living_ids}"

    healed_by_map = out["healed_by_map"]
    protected_by_map = out["protected_by_map"]
    assert isinstance(healed_by_map, dict)
    assert isinstance(protected_by_map, dict)
    assert all(isinstance(k, int) for k in healed_by_map.keys()), "healed_by_map keys must be int"
    assert all(isinstance(v, int) for v in healed_by_map.values()), "healed_by_map values must be int healer ids"
    assert all(isinstance(k, int) for k in protected_by_map.keys()), "protected_by_map keys must be int"
    for v in protected_by_map.values():
        assert isinstance(v, list)
        for entry in v:
            assert isinstance(entry, dict)
            assert "id" in entry


def assert_post_phase_transition_invariants(before_snapshot: Mapping[str, Any], after: Any) -> None:
    """
    Phase-fuzz invariants: designed to catch "wrong outcome" regressions without requiring
    Discord IO or full day/night mechanics.

    `before_snapshot` must capture pre-mutation values (not the same Game object as `after`).
    Keys: day_number, votes_today, players (sequence of objects with .id).
    """
    # Basic runtime sanity on the resulting game.
    assert_game_runtime_sanity(after)
    assert_no_negative_counters(after)

    # Persisted state should remain JSON-serializable.
    persisted = after.to_persisted()
    assert isinstance(persisted, dict)

    # Common counters should not go backwards across "start_*" operations.
    for k in ["day_number", "votes_today"]:
        b = before_snapshot.get(k)
        a = getattr(after, k, None)
        if isinstance(b, int) and isinstance(a, int):
            assert a >= b, f"{k} went backwards: before={b} after={a}"

    # Players should not silently duplicate.
    b_players = before_snapshot.get("players") or []
    if isinstance(b_players, str):
        b_players = []
    b_ids = (
        [getattr(p, "id", None) for p in b_players]
        if isinstance(b_players, (list, tuple))
        else []
    )
    a_ids = [getattr(p, "id", None) for p in getattr(after, "players", []) or []]
    if b_ids and a_ids and all(isinstance(x, int) for x in b_ids + a_ids):
        assert len(set(a_ids)) == len(a_ids), "players contains duplicate ids"



def assert_repro_payload_shape(payload: Dict[str, Any]) -> None:
    """
    Minimal guard so repro files remain replayable.
    """
    kind = payload.get("kind")
    roles = payload.get("roles")
    assert isinstance(roles, list) and roles, "repro payload must include non-empty roles list"
    # Only "engine" repros are replayable through scripts/sim_test.py.
    if kind in {None, "systematic_action_coverage", "exhaustive_role_combos_no_crash", "fuzz_night_actions_no_throw"}:
        night_actions = payload.get("night_actions", {})
        assert isinstance(night_actions, dict), "repro payload night_actions must be dict"
