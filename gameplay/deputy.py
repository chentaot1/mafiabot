"""Transactional Deputy day shots using the engine's tamper and combat rules."""
from deputy_rules import deputy_gun_sees_evil, deputy_shot_blocked_by_defense
from . import state as st
from .death import apply_death


async def fire(game, actor_id, target_id, *, guild=None):
    expected = st.identity(game)
    if guild:
        await game.sync_living_players(guild)
    def update():
        st.require_current(game, phase='day', expected=expected)
        if game.resolving or game.vote_in_progress:
            raise st.Rejected('Wait until resolution or the tribunal has finished.')
        living = {p.id for p in game.living_players}
        if game.player_roles.get(actor_id) != 'Deputy' or actor_id not in living:
            raise st.Rejected('Only a living Deputy can fire during the day.')
        if game.day_number < 2:
            raise st.Rejected('The Deputy can fire starting on Day 2.')
        if target_id not in living or target_id == actor_id:
            raise st.Rejected('Choose another living player.')
        state = game.role_states.setdefault(actor_id, {})
        if game.deputy_fired_today(actor_id) or state.get('deputy_shots_remaining', 0) <= 0:
            raise st.Rejected('You have no shot available.')
        evil = deputy_gun_sees_evil(game, target_id)
        armored = deputy_shot_blocked_by_defense(game, target_id)
        game.mark_deputy_shot_today(actor_id)
        receipts = []
        if evil and armored:
            message = 'Your shot was absorbed by the target’s defense.'
        elif evil:
            receipts.append(apply_death(game, target_id, 'deputy_shoot'))
            message = 'Your shot struck an evil target.'
        else:
            receipts.append(apply_death(game, target_id, 'deputy_friendly_fire'))
            receipts.append(apply_death(game, actor_id, 'deputy_guilt'))
            message = 'You shot an innocent target and died from guilt.'
        return message, [r for r in receipts if r]
    return await st.commit(game, update)
