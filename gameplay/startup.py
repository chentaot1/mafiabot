"""Idempotent delivery of a committed role assignment, including restart recovery."""
import discord
from config import ALL_MAFIA_ROLES, PLAYING_ROLE_ID, GAME_OVERSEER_ROLE_ID
from game import try_get_bot
from roles import role_start_dm_supplements
from async_work import run_blocking, finish_pending
from . import state as st
from .access import required_role, required_channel


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
    current()
    if guild is None:
        raise OSError('The game server is unavailable.')
    grants = {rid: required_role(guild, rid)
              for rid in (game.alive_role_id, PLAYING_ROLE_ID, game.lockdown_role_id)}
    mafia = required_channel(guild, game.mafia_tc_id)
    bot = client or try_get_bot()
    db = getattr(bot, 'db', None)
    async def deliver_message(member, uid, kind, text):
        current()
        if db:
            await run_blocking(db.enqueue_dm_outbox, guild_id=game.guild_id, kind=kind,
                dedupe_key=f'mafia_{kind}:{game.guild_id}:{match}:{uid}', match_key=match,
                target_user_id=uid, content=text)
        else:
            try:
                await member.send(text)
            except discord.Forbidden:
                # Match the queue's terminal blocked-DM behavior. Players can
                # still retrieve their role through the private role command.
                pass
        def receipt():
            current()
            completed = game.gameplay['startup'].setdefault('dm_receipts', {}).setdefault(str(uid), [])
            if kind not in completed:
                completed.append(kind)
        await st.commit(game, receipt)
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
            grant = grants[rid]
            if rid == game.lockdown_role_id and (member.guild_permissions.administrator or
                    any(r.id == GAME_OVERSEER_ROLE_ID for r in member.roles)):
                continue
            if grant and grant not in member.roles:
                await finish_pending(member.add_roles(grant))
                current()
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
            if kind in game.gameplay['startup'].get('dm_receipts', {}).get(str(uid), []):
                continue
            await finish_pending(deliver_message(member, uid, kind, text))
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
    from .lifecycle import message_lock
    match = game.game_key
    async with message_lock(game.guild_id):
        await _announce(game, guild, match)


async def _announce(game, guild, match):
    record = game.gameplay.get('startup', {})
    if record.get('announced') or not record.get('complete') or game.ending:
        return
    def current():
        from game import active_games
        if (active_games.get(game.guild_id) is not game or not game.in_progress or game.ending
                or game.game_key != match or game.gameplay.get('startup', {}).get('match') != match):
            raise st.Rejected('This startup was cancelled.')
    current()
    if guild is None:
        raise OSError('The game server is unavailable.')
    channel = guild.get_channel(game.game_channel_id)
    if channel is None and not getattr(guild, '_monte_carlo_fake', False):
        raise OSError('The game announcement channel is unavailable.')
    async def post(destination, text, flag):
        current()
        if destination:
            await destination.send(text)
        def checkpoint():
            current()
            game.gameplay['startup'][flag] = True
        await st.commit(game, checkpoint)
    if not record.get('mafia_announced'):
        mafia = required_channel(guild, game.mafia_tc_id)
        await finish_pending(post(mafia, 'Welcome, Mafiosi. This is your private channel.', 'mafia_announced'))
    text = '**Game Started!** Roles have been assigned secretly.'
    if game.phase in ('day', 'night'):
        text += f' It is now **{game.phase.title()} {game.day_number}**.'
    await finish_pending(post(channel, text, 'announced'))
