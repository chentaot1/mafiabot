"""Idempotent delivery of a committed role assignment, including restart recovery."""
from config import ALL_MAFIA_ROLES, PLAYING_ROLE_ID, GAME_OVERSEER_ROLE_ID
from game import try_get_bot
from roles import role_start_dm_supplements
from async_work import run_blocking, finish_pending
from . import state as st


async def resume(game, guild, *, client=None):
    async with game._startup_lock:
        await deliver(game, guild, client=client)
        await announce(game, guild)
    controller = getattr(client or try_get_bot(), 'gameplay_controller', None)
    if controller:
        await controller.recover(game)


async def deliver(game, guild, *, client=None):
    record = game.gameplay.get('startup')
    if not record or record.get('complete'):
        return
    match = game.game_key
    if record.get('match') != match:
        raise st.Rejected('The saved setup belongs to an earlier match. Reset this incomplete game.')
    def current():
        from game import active_games
        if active_games.get(game.guild_id) is not game or game.ending or game.game_key != match:
            raise st.Rejected('This startup was cancelled.')
    bot = client or try_get_bot()
    db = getattr(bot, 'db', None)
    for uid, role in list(game.player_roles.items()):
        current()
        if uid in record['completed_players']:
            continue
        member = await game.lookup_member(guild, uid)
        current()
        if member is None:
            from .death import apply_death
            def departure():
                current()
                apply_death(game, uid, 'left')
                game.gameplay['startup']['completed_players'].append(uid)
            await st.commit(game, departure)
            continue
        if uid not in {p.id for p in game.living_players}:
            await st.commit(game, lambda: game.gameplay['startup']['completed_players'].append(uid))
            continue
        for rid in (game.alive_role_id, PLAYING_ROLE_ID, game.lockdown_role_id):
            grant = guild.get_role(rid) if rid else None
            if rid == game.lockdown_role_id and (member.guild_permissions.administrator or
                    any(r.id == GAME_OVERSEER_ROLE_ID for r in member.roles)):
                continue
            if grant and grant not in member.roles:
                await finish_pending(member.add_roles(grant))
                current()
        mafia = guild.get_channel(game.mafia_tc_id) if game.mafia_tc_id else None
        if mafia and role in ALL_MAFIA_ROLES:
            await finish_pending(mafia.set_permissions(member, view_channel=True, send_messages=True))
            current()
        state = game.role_states.get(uid, {})
        messages = [('role_deal', f'--- GAME STARTED ---\nYour role is: **{role}**\nUse `!myrole` to see your abilities.')]
        if role == 'Executioner' and state.get('exe_target'):
            target = await game.lookup_member(guild, state['exe_target'])
            if target:
                messages.append(('exe_target', f'Your target is **{target.display_name}**. Convince the Town to lynch them to win.'))
        messages.extend(role_start_dm_supplements(role, bind_slot=game.player_slots.get(state.get('ga_target_id'))))
        for kind, text in messages:
            current()
            if db:
                await run_blocking(db.enqueue_dm_outbox, guild_id=game.guild_id, kind=kind,
                    dedupe_key=f'mafia_{kind}:{game.guild_id}:{match}:{uid}', match_key=match,
                    target_user_id=uid, content=text)
            else:
                await finish_pending(member.send(text))
            current()
        def checkpoint():
            current()
            game.gameplay['startup']['completed_players'].append(uid)
        await st.commit(game, checkpoint)
        record = game.gameplay['startup']
    def complete():
        current()
        game.gameplay['startup']['complete'] = True
    await st.commit(game, complete)


async def announce(game, guild):
    record = game.gameplay.get('startup', {})
    if record.get('announced') or not record.get('complete') or game.ending:
        return
    channel = guild.get_channel(game.game_channel_id)
    if channel is None and not getattr(guild, '_monte_carlo_fake', False):
        raise OSError('The game announcement channel is unavailable.')
    if not record.get('mafia_announced'):
        mafia = guild.get_channel(game.mafia_tc_id) if game.mafia_tc_id else None
        if mafia:
            await finish_pending(mafia.send('Welcome, Mafiosi. This is your private channel.'))
        await st.commit(game, lambda: game.gameplay['startup'].update(mafia_announced=True))
    if channel:
        await finish_pending(channel.send('**Game Started!** Roles have been assigned secretly. It is now **Day 1**.'))
    def announced():
        if game.game_key != record.get('match') or game.ending:
            raise st.Rejected('This startup was cancelled.')
        game.gameplay['startup']['announced'] = True
    await st.commit(game, announced)
