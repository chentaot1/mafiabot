from __future__ import annotations

import asyncio
import random
from copy import copy, deepcopy
from types import SimpleNamespace

import discord

from async_work import finish_pending
from engine.night import run_night_pipeline, deliver_psychic_visions
from . import state as st
from .death import apply_death


async def evaluate(game, guild):
    from night_resolve_prep import expand_reanimate_for_night_resolve, notify_reanimate_expand_failures
    from retributionist_consumption import consume_retributionist_uses
    from night_guilt import tally_guilt_and_jester_deaths
    from night_resume import parse_night_resume_state, coerce_snap_id_list, night_kill_deaths_from_snap
    resume = parse_night_resume_state(game.night_completion_snapshot, day_number=game.day_number, game_key=game.game_key)
    if resume.resume_post_pipeline_only:
        # A legacy completed engine is a result, not permission to execute spent actions again.
        snap = game.night_completion_snapshot
        deaths = set(coerce_snap_id_list(snap, 'deaths')) | night_kill_deaths_from_snap(snap)
        blocked = coerce_snap_id_list(snap, 'blocked')
        if not snap.get('retri_consumption_done'):
            consume_retributionist_uses(game, blocked, {})
        guilt, haunts = await tally_guilt_and_jester_deaths(game, guild, deaths, night_kill_deaths_from_snap(snap))
        game.living_players = [p for p in game.living_players if p.id not in deaths]
        if not snap.get('psychic_visions_delivered'):
            await deliver_psychic_visions(game, guild, blocked)
        return deaths, guilt, haunts
    failed = expand_reanimate_for_night_resolve(game)
    await notify_reanimate_expand_failures(game, guild, failed)
    visits, blocked, healed, protected, deaths = await run_night_pipeline(game, guild)
    consume_retributionist_uses(game, blocked, healed)
    deaths = set(deaths)
    guilt, haunts = await tally_guilt_and_jester_deaths(game, guild, deaths, set(deaths))
    # Visions describe survivors after casualties; the roster and feedback
    # remain isolated until the durable result is committed.
    game.living_players = [p for p in game.living_players if p.id not in deaths]
    await deliver_psychic_visions(game, guild, blocked)
    game.psychic_visions_delivered_this_night = True
    return deaths, guilt, haunts


class BufferedMember:
    def __init__(self, member, feedback):
        self.id, self.display_name = member.id, member.display_name
        self.mention, self.roles = getattr(member, "mention", f"<@{member.id}>"), getattr(member, "roles", [])
        self.feedback = feedback

    async def send(self, text):
        self.feedback.append({"user_id": self.id, "text": text})


async def begin(game, guild):
    expected = st.identity(game)
    await game.sync_living_players(guild)
    entered = False
    def enter():
        nonlocal entered
        st.require_current(game, phase="night", expected=expected)
        if game.resolving:
            raise st.Rejected("Resolution is already in progress.")
        if any(a.get("type") == "plunder" and not a.get("duel_finished") for a in game.night_actions.values()):
            raise st.Rejected("A Pirate duel is still in progress. Wait for it to finish.")
        game.resolving = True
        working = copy(game)
        working.night = deepcopy(game.night)
        working.tribunal_state = deepcopy(game.tribunal_state)
        async def no_persist():
            return None
        working.persist_flush = no_persist
        for field in ("role_states", "night_actions", "graveyard", "doused_players", "player_roles", "gameplay",
                      "night_transport_swaps", "_transport_pairs_seen", "night_transport_dm_pairs",
                      "_effective_visit_destinations_cache"):
            setattr(working, field, deepcopy(getattr(game, field, None)))
        entered = True
        return working
    # Resolution is calculated on an isolated model. Network feedback is buffered.
    try:
        working = await st.commit(game, enter)
    except asyncio.CancelledError:
        # Cancellation after the entry save drains can precede returning the
        # working model to run(). Only the call that entered owns this flag.
        if entered:
            await release_unapplied(game, expected)
        raise
    return expected, working


async def release_unapplied(game, expected):
    from game import active_games
    def release():
        if (active_games.get(game.guild_id) is game and st.identity(game) == expected
                and game.in_progress and not game.ending):
            record = game.gameplay.get('resolution') or {}
            committed = (record.get('applied') and record.get('night_token') == expected[2]
                         and not record.get('progressed'))
            if not committed:
                game.resolving = False
    await st.commit(game, release, persist=(active_games.get(game.guild_id) is game
                    and st.identity(game) == expected and game.in_progress and not game.ending), allow_recovery=True)


async def run(game, ctx):
    expected, working = await begin(game, ctx.guild)
    applied = False
    try:
        feedback = []
        buffered = {p.id: BufferedMember(p, feedback) for p in game.players}
        working.players = list(buffered.values())
        working.living_players = [buffered[p.id] for p in working.living_players if p.id in buffered]
        async def member(guild, uid):
            return buffered.get(uid)
        async def sync(guild):
            return None
        working.get_member_safe, working.sync_living_players = member, sync
        class BufferedGuild:
            def __getattr__(self, name):
                return getattr(ctx.guild, name)
            def get_member(self, uid):
                return buffered.get(uid)
            async def fetch_member(self, uid):
                return buffered.get(uid)
        deaths, guilt, haunts = await evaluate(working, BufferedGuild())
        def apply():
            st.require_current(game, phase="night", expected=expected)
            if not game.resolving:
                raise st.Rejected("This resolution was cancelled.")
            for field in ("role_states", "night_actions", "graveyard", "doused_players", "player_roles", "night_death_causes"):
                setattr(game, field, getattr(working, field))
            for uid in deaths:
                cause = "haunt" if uid in haunts else "guilt" if uid in guilt else "night_kill"
                custom = (f"The Jester's spirit claimed <@{uid}>." if cause == "haunt" else
                          f"Overcome with guilt, <@{uid}> took their own life." if cause == "guilt" else None)
                apply_death(game, uid, cause, custom_message=custom)
            game.gameplay["resolution"] = {"night_token": expected[2], "applied": True,
                "day":game.day_number, "feedback": feedback, "feedback_index": 0, "death_ids": sorted(deaths),
                "progressed": False, "public_delivery_pending":False}
            game.gameplay.setdefault('resolutions', {})[expected[2]] = game.gameplay['resolution']
        await st.commit(game, apply)
        applied = True
        await finish(game, ctx)
    finally:
        if not applied:
            await release_unapplied(game, expected)


async def finish(game, ctx):
    # A command and restart recovery may request the same delivery concurrently.
    # This lock serializes delivery only; it never holds the model's state lock.
    key = ('resolution', game.gameplay.get('resolution', {}).get('night_token'))
    lock = game.delivery_locks.setdefault(key, asyncio.Lock())
    async with lock:
        await _finish(game, ctx)


async def _deliver_feedback(game, ctx, item, index, expected, day):
    """Deliver one saved result and checkpoint it before cancellation returns."""
    st.require_current(game, phase="night", expected=expected)
    member = await game.get_member_safe(ctx.guild, item["user_id"])
    st.require_current(game, phase="night", expected=expected)
    if member:
        try:
            from game import try_get_bot
            from . import reports
            from .views import ReportCard, NO_MENTIONS
            controller = getattr(try_get_bot(), 'gameplay_controller', None)
            report = reports.for_feedback(game, item['user_id'], item['text'], day)
            if report and controller:
                destination = await controller.private_destination(game, item['user_id'])
                st.require_current(game, phase="night", expected=expected)
                await destination.send(view=ReportCard(game, report), allowed_mentions=NO_MENTIONS)
            else:
                await member.send(item["text"])
        except discord.Forbidden:
            pass
    def delivered():
        st.require_current(game, phase="night", expected=expected)
        game.gameplay["resolution"]["feedback_index"] = index+1
    await st.commit(game, delivered)


async def _finish(game, ctx):
    record = game.gameplay.get("resolution")
    if not record or record.get("progressed"):
        return
    expected = st.identity(game)
    st.require_current(game, phase="night")
    if record.get("night_token") != expected[2]:
        raise st.Rejected("This night resolution is obsolete.")
    public_available = getattr(ctx, 'public_available', True)
    if not public_available:
        def defer_public():
            st.require_current(game,phase='night',expected=expected)
            game.gameplay['resolution']['public_delivery_pending']=True
        await st.commit(game,defer_public)
    for uid in record["death_ids"]:
        receipt = game.gameplay["deaths"].get(str(uid))
        if receipt and not receipt.get("delivered"):
            await game.deliver_death_receipt(ctx if public_available else None, ctx.guild, receipt)
            if not receipt.get('delivered'):
                def pending():
                    st.require_current(game, phase='night', expected=expected)
                    game.gameplay['resolution']['public_delivery_pending'] = True
                await st.commit(game, pending)
    while record["feedback_index"] < len(record["feedback"]):
        index = record["feedback_index"]
        item = record["feedback"][index]
        from .lifecycle import message_lock
        async with message_lock(game.guild_id):
            # Drain only this delivery and its receipt. Phase advancement stays
            # in the owning task so shutdown cannot start the next day's work.
            await finish_pending(_deliver_feedback(game, ctx, item, index, expected, record.get('day', game.day_number)))
        record = game.gameplay["resolution"]
    if await game.check_win_conditions():
        return
    await game.start_day(ctx, resolution_token=expected[2])
    from game import try_get_bot
    controller = getattr(try_get_bot(), 'gameplay_controller', None)
    if controller and record.get('public_delivery_pending') and game.in_progress:
        controller.start_job((game.guild_id, 'resolution-delivery', expected[2]),
            lambda: controller.deliver_deferred_resolution(game, expected[2]))
