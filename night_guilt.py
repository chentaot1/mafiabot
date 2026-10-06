"""Guilt / Jester haunt tally shared by live ``!resolve`` and MC bridge."""
from __future__ import annotations

import random
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from game import Game


async def tally_guilt_and_jester_deaths(
    game: "Game",
    guild: object,
    deaths: Set[int],
    night_kill_deaths: Set[int],
) -> Tuple[List[int], List[int]]:
    """Apply guilt conversion + jester haunt picks; mutates ``deaths``."""
    from night_resume import coerce_snap_id_list, normalize_night_completion_snapshot

    await game.sync_living_players(guild)  # type: ignore[arg-type]
    living_ids = await game.get_living_ids(guild)  # type: ignore[arg-type]

    snap = normalize_night_completion_snapshot(getattr(game, "night_completion_snapshot", None))
    pending_haunts = coerce_snap_id_list(snap, "pending_jester_haunts") if snap else []

    # ``guilty_tomorrow`` is converted at ``start_night`` (ToS following-night guilt).
    # Tally only processes ``will_die_of_guilt`` (resume / lynch safety net).

    if pending_haunts:
        jester_haunts = list(pending_haunts)
        deaths.update(jester_haunts)
        guilty_vigs = [
            p_id
            for p_id, s in game.role_states.items()
            if s.get("will_die_of_guilt") and p_id in living_ids and p_id not in night_kill_deaths
        ]
        deaths.update(guilty_vigs)
        if isinstance(snap, dict):
            updated = dict(snap)
            updated.pop("pending_jester_haunts", None)
            game.night_completion_snapshot = updated
        return guilty_vigs, jester_haunts

    guilty_vigs = [
        p_id
        for p_id, s in game.role_states.items()
        if s.get("will_die_of_guilt") and p_id in living_ids and p_id not in night_kill_deaths
    ]
    deaths.update(guilty_vigs)

    for _j_id, s in list(game.role_states.items()):
        if not s.get("can_haunt"):
            continue
        if "haunt_target" in s:
            continue
        eligible = [vid for vid in s.get("guilty_voters", []) if vid in living_ids]
        if eligible:
            chosen = random.choice(eligible)
            s["haunt_target"] = chosen
            s["can_haunt"] = False
            from night_engine_checkpoint import _merge_snap

            prior = coerce_snap_id_list(snap, "pending_jester_haunts") if snap else []
            _merge_snap(
                game,
                {"pending_jester_haunts": sorted({int(x) for x in prior} | {int(chosen)})},
            )

    jester_haunts: List[int] = []
    for s in game.role_states.values():
        if "haunt_target" not in s:
            continue
        try:
            jester_haunts.append(int(s["haunt_target"]))
        except (TypeError, ValueError):
            continue
    deaths.update(jester_haunts)

    if jester_haunts:
        from night_engine_checkpoint import _merge_snap

        _merge_snap(game, {"pending_jester_haunts": sorted({int(x) for x in jester_haunts})})

    for _p_id, s in list(game.role_states.items()):
        s.pop("haunt_target", None)

    return guilty_vigs, jester_haunts


async def apply_guilt_and_haunt_deaths(
    game: "Game",
    guild: object,
    channel_or_ctx: object,
    *,
    night_kill_deaths: Optional[Set[int]] = None,
) -> Tuple[List[int], List[int]]:
    """
    Run guilt/haunt tally and apply Discord deaths (lynch-day or post-resolve).

    Skips ids already handled as night kills. Used after a Jester lynch so haunt
    victims die before ``check_win_conditions``.
    """
    from messages import tos as tos_msg

    nk = {int(x) for x in (night_kill_deaths or set())}
    deaths: Set[int] = set()
    guilty_vigs, jester_haunts = await tally_guilt_and_jester_deaths(
        game, guild, deaths, nk  # type: ignore[arg-type]
    )
    jh_set = {int(x) for x in jester_haunts}
    gv_set = {int(x) for x in guilty_vigs}
    to_apply = sorted(
        (int(x) for x in deaths if int(x) not in nk),
        key=lambda pid: game.player_slots.get(pid, 9999),
    )
    for p_id in to_apply:
        if p_id in jh_set:
            cause = "haunt"
            custom = f"👻 The Jester's spirit has claimed its revenge! **<@{p_id}>** was found dead."
        elif p_id in gv_set:
            cause = "guilt"
            custom = f"Overcome with guilt, <@{p_id}> took their own life."
        else:
            continue
        member = await game.get_member_safe(guild, p_id)  # type: ignore[arg-type]
        if member:
            await game.process_death(channel_or_ctx, member, cause, custom_message=custom)
        else:
            await game.process_death_by_id(
                channel_or_ctx, guild, p_id, cause, custom_message=custom  # type: ignore[arg-type]
            )
    return guilty_vigs, jester_haunts
