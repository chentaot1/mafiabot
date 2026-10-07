"""Modern UI transitions over the recovered 32-role game model."""
from __future__ import annotations

from copy import deepcopy
import secrets
import discord

from game import try_get_bot
from per_night_state import all_keys_cleared_at_start_night
from . import state as st, trials


async def start_night(game, ctx, *, trial_token=None):
    st.require_current(game)
    expected = st.identity(game)
    previous_panels = deepcopy(game.gameplay.get('panels', {}))
    if game.phase == 'day' and game.in_progress and not game.ending:
        if await game._close_bloodless_cycle_and_maybe_draw():
            return
    if game.phase != 'night' and game.in_progress and not game.ending:
        await game._process_deferred_guilt_at_night_start(ctx)
    def transition():
        st.require_current(game)
        if not game.in_progress or game.ending or game.phase == 'night':
            return False
        if expected != st.identity(game):
            raise st.Rejected('The phase already changed.')
        if game.resolving:
            raise st.Rejected('Night is resolving.')
        trial = game.gameplay.get('trial')
        if trial and trial.get('applied') and not trial.get('progressed') and trial_token != trial.get('id'):
            raise st.Rejected('The verdict is being delivered. Please wait.')
        if trial and trial_token == trial.get('id'):
            trial['progressed'] = True
        elif trial and trial.get('stage') not in {'done', 'cancelled'}:
            trials.cancel_model(game, trial, 'The day ended.', refund=True)
        game.phase = 'night'
        trials.clear_flags(game)
        game.night_actions = {}
        game.night_transport_swaps = []
        game._transport_pairs_seen = set()
        game.night_transport_dm_pairs = set()
        game._effective_visit_destinations_cache = None
        game.night_completion_snapshot = None
        game.psychic_visions_delivered_this_night = False
        game.gameplay['night_token'] = secrets.token_hex(8)
        game.gameplay['panels'] = {}
        for state in game.role_states.values():
            for key in all_keys_cleared_at_start_night():
                state.pop(key, None)
        return True
    if not await st.commit(game, transition):
        return
    controller = getattr(try_get_bot(), 'gameplay_controller', None)
    if controller:
        await controller.close_night_panels(previous_panels)
    day_vc = ctx.guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    alive_role = ctx.guild.get_role(game.alive_role_id) if game.alive_role_id else None
    if day_vc and alive_role:
        try:
            await day_vc.set_permissions(alive_role, speak=False)
        except discord.HTTPException:
            pass
    try:
        await ctx.send(f'🌙 It is now **Night {game.day_number}**. Use /actions or your private night panel.')
    except discord.HTTPException:
        pass  # Panels and the committed phase do not depend on public send access.
    await game.sync_living_players(ctx.guild)
    if controller:
        await controller.send_night_panels(game)


async def start_day(game, ctx, *, resolution_token=None):
    st.require_current(game)
    expected = st.identity(game)
    previous_panels = deepcopy(game.gameplay.get('panels', {}))
    def transition():
        st.require_current(game)
        if not game.in_progress or game.ending or game.phase == 'day':
            return False
        if expected != st.identity(game):
            raise st.Rejected('The phase already changed.')
        record = game.gameplay.get('resolution')
        if game.resolving and (not record or not record.get('applied') or resolution_token != record.get('night_token')):
            raise st.Rejected('Night is resolving.')
        if record and resolution_token == record.get('night_token'):
            record['progressed'] = True
        game.resolving = False
        game.phase = 'day'
        game.day_number += 1
        game.gameplay['panels'] = {}
        game.votes_today = 0
        trials.clear_flags(game)
        game.night_completion_snapshot = None
        game.psychic_visions_delivered_this_night = False
        return True
    if not await st.commit(game, transition):
        return
    await game.sync_living_players(ctx.guild)
    controller = getattr(try_get_bot(), 'gameplay_controller', None)
    if controller:
        await controller.close_night_panels(previous_panels)
        await controller.repair_voice(game)
    else:
        vc = ctx.guild.get_channel(game.day_vc_id) if game.day_vc_id else None
        alive = ctx.guild.get_role(game.alive_role_id) if game.alive_role_id else None
        if vc and alive:
            await vc.set_permissions(alive, connect=True, speak=True)
    try:
        await ctx.send(f'☀️ The sun rises on **Day {game.day_number}**. Remaining players: {len(game.living_players)}')
    except discord.HTTPException:
        if resolution_token:
            def pending():
                st.require_current(game, phase='day')
                record = game.gameplay.get('resolutions', {}).get(resolution_token) or game.gameplay.get('resolution')
                if record and record.get('night_token') == resolution_token:
                    record['public_delivery_pending'] = True
            await st.commit(game, pending)
    if controller:
        await controller.send_day_panels(game)
    from messages import tos
    for uid, role in game.player_roles.items():
        state = game.role_states.get(uid, {})
        if role == 'Guardian Angel' and state.get('ga_announce_pending'):
            await ctx.send(tos.ga_protected(tos.format_player(game, state.get('ga_target_id'))))
            def clear_notice():
                state['ga_announce_pending'] = False
            await st.commit(game, clear_notice)
        if controller is None and role == 'Deputy' and game.day_number >= 2 and state.get('deputy_shots_remaining', 0) > 0:
            member = await game.get_member_safe(ctx.guild, uid)
            if member and uid in {p.id for p in game.living_players}:
                try:
                    await member.send(tos.deputy_day_prompt(game.day_number))
                except discord.HTTPException:
                    pass


async def reset(game, guild, *, nuke=False, _from_check_win=False):
    def invalidate():
        game.ending = True
        game.gameplay['night_token'] = None
    await st.commit(game, invalidate, persist=False, allow_recovery=True)
    controller = getattr(try_get_bot(), 'gameplay_controller', None)
    if controller:
        await controller.stop_game(game)
    # Drain startup API requests before removing roles; never hold this lock
    # while waiting for endgame, whose owner may itself request cleanup.
    async with game._startup_lock:
        pass
    from .lifecycle import message_lock
    async with message_lock(game.guild_id):
        pass
    if nuke:
        await game._historical_nuke_reset(guild)
    else:
        await game._historical_reset(guild, _from_check_win=_from_check_win)
    game.gameplay = {'version': 1, 'trial': None, 'panels': {}, 'night_token': None, 'deaths': {}, 'duels': {}}
    game.action_cooldowns.clear()
