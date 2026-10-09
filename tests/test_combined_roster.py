"""Regression checks at the boundary between modern controls and the 32-role engine."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import game as gm
from game import Game
from gameplay import actions, trials, resolution, state as st
from gameplay.deputy import fire
from gameplay.views import Draft
from scripts.monte_carlo import bridge


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(gm, 'active_games', {})
    monkeypatch.setattr(gm, '_BOT', None)
    game = Game(123)
    game.in_progress, game.phase, game.day_number, game.game_key = True, 'night', 2, 'combined'
    game.gameplay['night_token'] = 'night-2'
    members = [bridge._FakeMember(i) for i in range(1, 7)]
    guild = bridge._FakeGuild(members)
    for p in members:
        p.send = AsyncMock()
        p.guild = guild
    game.players, game.living_players = members, members.copy()
    game.player_slots = {p.id:p.id for p in members}
    game.player_roles = {1:'Seer', 2:'Doctor', 3:'Mobster', 4:'Sheriff', 5:'Survivor', 6:'Psychic'}
    game.role_states = {p.id:{} for p in members}
    gm.active_games[123] = game
    return game, guild, members


@pytest.mark.asyncio
async def test_dead_guardian_can_ward_only_bound_player_and_protection_survives_resolution(model):
    game, guild, members = model
    game.player_roles[1] = 'Guardian Angel'
    game.role_states[1] = {'ga_ward_charges':1, 'ga_target_id':2, 'ga_defeated':False}
    game.living_players.remove(members[0])
    assert not (await actions.submit(game,1,'ward',(4,),cooldown=False)).accepted
    assert (await actions.submit(game,1,'ward',(2,),cooldown=False)).accepted
    assert (await actions.submit(game,3,'kill',(2,),cooldown=False)).accepted
    game.check_win_conditions = AsyncMock(return_value=False)
    game.start_day = AsyncMock()
    await resolution.run(game, SimpleNamespace(guild=guild,send=AsyncMock()))
    assert 2 in {p.id for p in game.living_players}
    assert game.role_states[1]['ga_ward_charges'] == 0
    assert game.role_states[2]['ga_trial_lock_day'] == 3
    assert game.gameplay['resolution']['applied']


@pytest.mark.asyncio
async def test_seer_controls_create_real_engine_gaze_and_buffer_results_until_commit(model):
    game, guild, members = model
    assert not (await actions.submit(game,1,'gaze',(2,2),cooldown=False)).accepted
    assert (await actions.submit(game,1,'gaze',(2,3),cooldown=False)).accepted
    game.check_win_conditions = AsyncMock(return_value=False)
    game.start_day = AsyncMock()
    game.persist_flush = AsyncMock()
    observed = []
    original = resolution.evaluate
    async def evaluate(working, buffered_guild):
        result = await original(working,buffered_guild)
        assert not members[0].send.called
        assert game.role_states[1].get('seer_pair_history') is None
        assert working.role_states[1]['seer_pair_history']
        observed.append(True)
        return result
    from unittest.mock import patch
    with patch.object(resolution,'evaluate',evaluate):
        await resolution.run(game,SimpleNamespace(guild=guild,send=AsyncMock()))
    assert observed
    assert members[0].send.called
    assert game.role_states[1]['seer_pair_history']


@pytest.mark.asyncio
async def test_cautious_toggle_does_not_replace_serial_killer_target(model):
    game,guild,_=model
    game.player_roles[1]='Serial Killer'
    assert (await actions.submit(game,1,'sk_kill',(2,),cooldown=False)).accepted
    action=game.night_actions[1].copy()
    assert (await actions.submit(game,1,'cautious',cooldown=False)).accepted
    assert game.role_states[1]['sk_cautious']
    assert game.night_actions[1]==action


@pytest.mark.asyncio
@pytest.mark.parametrize('target_role, expected_deaths',[('Mobster',{2}),('Doctor',{1,2})])
async def test_deputy_shot_is_atomic_and_cannot_be_repeated(model,target_role,expected_deaths):
    game,guild,_=model
    game.phase='day'
    game.player_roles[1]='Deputy'
    game.player_roles[2]=target_role
    game.role_states[1]={'deputy_shots_remaining':1,'deputy_fired_day':0}
    _,receipts=await fire(game,1,2,guild=guild)
    assert {r['player_id'] for r in receipts}==expected_deaths
    assert game.role_states[1]['deputy_shots_remaining']==0
    with pytest.raises(st.Rejected):
        await fire(game,1,3,guild=guild)
    assert 3 in {p.id for p in game.living_players}


@pytest.mark.asyncio
async def test_guardian_trial_lock_preserves_votes_but_prevents_lynch(model):
    game,_,_=model
    game.phase='day'
    game.role_states[2]['ga_trial_lock_day']=game.day_number
    from config import GAME_OVERSEER_ROLE_ID
    trial=await trials.start(game,role_ids=[GAME_OVERSEER_ROLE_ID],channel_id=10)
    trial.update(stage='judgment',defendant=2,deadline=(st.now()-timedelta(seconds=1)).isoformat(),judgments={'1':'guilty','3':'guilty'})
    await trials.advance(game,trial['id'])
    assert trial['result']['totals']['guilty']==2
    assert not trial['result']['guilty']
    await trials.apply_result(game,trial['id'])
    assert 2 in {p.id for p in game.living_players}


@pytest.mark.asyncio
async def test_concurrent_night_transitions_do_not_double_increment_stalemate_cycle(model):
    game,guild,_=model
    game.phase='day'
    ctx=SimpleNamespace(guild=guild,send=AsyncMock())
    await asyncio.gather(game.start_night(ctx),game.start_night(ctx))
    assert game.phase=='night'
    assert game.bloodless_cycle_streak==1


def test_public_commands_and_simulator_share_the_restored_roster():
    import bot
    import roles
    from scripts.monte_carlo.audit import audit_against_bot_config
    assert len(roles.ROLE_DESCRIPTIONS)==32
    assert all(bot.bot.get_command(name) for name in ('stab','ward','gaze','cautious','shoot','actions'))
    audit_against_bot_config()


def test_simulated_deputy_can_apply_death_without_discord_or_disk_io(monkeypatch):
    members=[bridge._FakeMember(i) for i in (1,2,3)]
    game=Game(555)
    game.in_progress=True
    game.phase='day'
    game.day_number=2
    game.players=members
    game.living_players=members.copy()
    game.player_roles={1:'Deputy',2:'Mobster',3:'Doctor'}
    game.role_states={1:{'deputy_shots_remaining':1},2:{},3:{}}
    game.persist_flush=AsyncMock()
    guild=bridge._FakeGuild(members)
    deaths=asyncio.run(bridge.deputy_day_shot(game,guild,1,2))
    assert deaths=={2}
    assert 2 not in {p.id for p in game.living_players}


@pytest.mark.asyncio
async def test_psychic_vision_is_buffered_and_excludes_tonights_dead(model):
    game, guild, members = model
    game.player_roles[1] = 'Doctor'
    game.player_roles[2] = 'Mobster'
    game.player_roles[3] = 'Sheriff'
    await actions.submit(game, 2, 'kill', (3,), cooldown=False)
    game.check_win_conditions = AsyncMock(return_value=False)
    game.start_day = AsyncMock()
    original = resolution.evaluate
    from unittest.mock import patch
    async def evaluate(working, buffered_guild):
        result = await original(working, buffered_guild)
        assert not members[5].send.called
        assert 3 not in {p.id for p in working.living_players}
        assert working.psychic_visions_delivered_this_night
        return result
    with patch.object(resolution, 'evaluate', evaluate):
        await resolution.run(game, SimpleNamespace(guild=guild, send=AsyncMock()))
    assert members[5].send.called


@pytest.mark.asyncio
async def test_failed_resolution_does_not_leak_transport_state_or_feedback(model):
    game, guild, members = model
    game.night_transport_swaps = [(2, 3, 1)]
    game.night_transport_dm_pairs = {frozenset({2,3})}
    game._transport_pairs_seen = {frozenset({2,3})}
    from unittest.mock import patch
    async def fail(working, buffered_guild):
        working.night_transport_swaps.append((4,5,1))
        working.night_transport_dm_pairs.add(frozenset({4,5}))
        working._transport_pairs_seen.clear()
        await working.players[0].send('private result')
        raise RuntimeError('engine interrupted')
    with patch.object(resolution, 'evaluate', fail):
        with pytest.raises(RuntimeError, match='interrupted'):
            await resolution.run(game, SimpleNamespace(guild=guild, send=AsyncMock()))
    assert game.night_transport_swaps == [(2,3,1)]
    assert game.night_transport_dm_pairs == {frozenset({2,3})}
    assert game._transport_pairs_seen == {frozenset({2,3})}
    assert not members[0].send.called
    assert not game.resolving


@pytest.mark.asyncio
async def test_transporter_corpse_reaches_shared_pipeline(model):
    game, guild, _ = model
    game.player_roles[1] = 'Retributionist'
    game.role_states[1] = {'uses_remaining':1, 'used_corpses':[]}
    game.graveyard = [{'player_id':7, 'real_role':'Transporter', 'is_hidden':False}]
    assert (await actions.submit(game,1,'reanimate',(2,4),corpse_id=7,cooldown=False)).accepted
    await actions.submit(game,3,'kill',(2,),cooldown=False)
    game.check_win_conditions = AsyncMock(return_value=False)
    game.start_day = AsyncMock()
    await resolution.run(game,SimpleNamespace(guild=guild,send=AsyncMock()))
    assert 2 in {p.id for p in game.living_players}
    assert 4 not in {p.id for p in game.living_players}
    assert game.role_states[1]['uses_remaining'] == 0
    assert game.graveyard[0]['used_by_retri']


@pytest.mark.asyncio
async def test_guardian_protection_rejects_nomination(model):
    game,_,_ = model
    game.phase='day'
    game.role_states[2]['ga_trial_lock_day']=game.day_number
    from config import GAME_OVERSEER_ROLE_ID
    trial=await trials.start(game,role_ids=[GAME_OVERSEER_ROLE_ID],channel_id=10)
    with pytest.raises(st.Rejected,match='protected'):
        await trials.cast(game,trial['id'],1,2,'nomination')
    assert not trial['nominations']


@pytest.mark.asyncio
async def test_night_shoot_dispatcher_accepts_dm_and_rejects_public_channel(model,monkeypatch):
    game,guild,members=model
    import bot as module
    guild.id=123
    guild.chunked=True
    game.player_roles[1]='Vigilante'
    game.role_states[1]={'shots_remaining':1}
    monkeypatch.setattr(module.bot,'get_guild',lambda gid:guild)
    ctx=SimpleNamespace(guild=None,author=members[0],interaction=None,send=AsyncMock())
    await module.shoot.callback(ctx,3)
    assert game.night_actions[1]['type']=='shoot'
    game.night_actions.clear()
    ctx.guild=guild
    ctx.channel=SimpleNamespace(id=88)
    await module.shoot.callback(ctx,3)
    assert not game.night_actions


@pytest.mark.asyncio
async def test_startgame_initializes_and_explains_all_five_restored_roles(model,monkeypatch):
    game,guild,members=model
    import bot as module
    from config import PLAYING_ROLE_ID
    guild.id=123
    guild.get_role=lambda rid:SimpleNamespace(id=rid) if rid==PLAYING_ROLE_ID else None
    game.in_progress=False
    game.phase=None
    game.setup_infrastructure=AsyncMock()
    selected=['Guardian Angel','Psychic','Seer','Deputy','Serial Killer','Mobster']
    monkeypatch.setattr(module.game_roles,'draw_roles_for_startgame',lambda n,rng:selected.copy())
    monkeypatch.setattr(module.random,'shuffle',lambda items:None)
    monkeypatch.setattr(module,'active_games',gm.active_games)
    monkeypatch.setattr(module.bot,'db',None,raising=False)
    ctx=SimpleNamespace(guild=guild,channel=SimpleNamespace(id=10),send=AsyncMock())
    await module._startgame(ctx,game)
    assert list(game.player_roles.values())==selected
    assert game.role_states[1]['ga_target_id'] in range(2,7)
    assert game.role_states[4]['deputy_shots_remaining']==1
    assert game.role_states[3]['seer_pair_history']==[]
    assert game.role_states[5]['sk_cautious'] is False
    assert game.gameplay['startup']['complete']
    assert all(p.send.await_count>=3 for p in members[:5])


@pytest.mark.asyncio
async def test_new_match_cannot_overwrite_uncommitted_endgame(model,monkeypatch):
    game,guild,_=model
    import bot as module
    import game_recovery
    game.in_progress=False
    old_key=game.game_key
    monkeypatch.setattr(game_recovery,'_pending_endgame_meta',lambda gid:{'game_key':'previous'})
    monkeypatch.setattr(Game,'commit_pending_endgame_if_any',staticmethod(lambda gid:False))
    ctx=SimpleNamespace(guild=guild,send=AsyncMock())
    await module._startgame(ctx,game)
    assert game.game_key==old_key and not game.in_progress
    assert 'pending' in ctx.send.call_args.args[0]
