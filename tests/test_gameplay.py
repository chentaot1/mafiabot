import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import game as gm
import persistence
from config import GAME_OVERSEER_ROLE_ID
from game import Game
from gameplay import actions, duels, trials, state as st, resolution
from gameplay.controller import Controller, channel_is_private
from gameplay.views import Draft, DuelView, NightPanel, TrialView, NominationPicker, trial_text


class Message:
    def __init__(self, channel, mid):
        self.channel, self.id, self.view = channel, mid, None
        self.edit = AsyncMock(side_effect=self.update)

    async def update(self, **kwargs):
        self.view = kwargs.get('view')


class Channel:
    def __init__(self, cid, guild=None):
        self.id, self.guild = cid, guild
        self.messages, self.sent, self.overwrites = {}, [], {}
        self.set_permissions = AsyncMock()
        self.readers = set()

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=member.id in self.readers, send_messages=True)

    async def send(self, text=None, **kwargs):
        self.sent.append((text, kwargs))
        message = Message(self, len(self.messages)+1)
        message.view = kwargs.get('view')
        self.messages[message.id] = message
        return message

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'missing')
        return self.messages[mid]


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(gm, 'active_games', {})
    game = Game(123)
    game.in_progress, game.phase, game.day_number, game.game_key = True, 'night', 1, 'test-match'
    game.gameplay['night_token'] = 'night-one'
    channels = {10: Channel(10), 11: Channel(11)}
    players = []
    for uid in range(1, 5):
        dm = Channel(100+uid)
        player = SimpleNamespace(id=uid, display_name=f'Player {uid}', mention=f'<@{uid}>', roles=[], bot=False,
            guild_permissions=SimpleNamespace(administrator=False), create_dm=AsyncMock(return_value=dm),
            send=AsyncMock(), add_roles=AsyncMock(), remove_roles=AsyncMock(), voice=None)
        players.append(player)
        channels[dm.id] = dm
    guild = SimpleNamespace(id=123, members=players, default_role=SimpleNamespace(id=0), chunked=True,
        get_member=lambda uid: next((p for p in players if p.id==uid), None),
        get_channel=lambda cid: channels.get(cid), get_role=lambda rid: SimpleNamespace(id=rid))
    for player in players:
        player.guild = guild
    for channel in channels.values():
        if channel.id < 100:
            channel.guild = guild
    game.players, game.living_players = players[:], players[:]
    game.player_slots = {p.id:p.id for p in players}
    game.player_roles = {1:'Doctor', 2:'Mayor', 3:'Jester', 4:'Executioner'}
    game.role_states = {1:{'self_heals_remaining':1}, 2:{'is_revealed':True}, 4:{'exe_target':3}}
    game.game_channel_id, game.day_vc_id, game.alive_role_id, game.stand_role_id = 10, 11, 20, 21
    gm.active_games[123] = game
    bot = SimpleNamespace(get_guild=lambda gid: guild, get_channel=lambda cid: channels.get(cid), add_view=lambda *a,**k:None)
    controller = Controller(bot)
    bot.gameplay_controller = controller
    monkeypatch.setattr(gm, '_BOT', bot)
    game.check_win_conditions = AsyncMock(return_value=False)
    return game, guild, controller, channels, players


# Explicit expected engine payloads establish command parity, including special formats.
CASES = [
    ('Mobster','kill',(2,),{}, {'target':2}), ('Doctor','heal',(1,),{}, {'target':1}),
    ('Escort','roleblock',(2,),{}, {'target':2}), ('Consort','roleblock',(2,),{}, {'target':2}),
    ('Sheriff','investigate',(3,),{}, {'target':3,'role':'Sheriff'}),
    ('Investigator','investigate',(3,),{}, {'target':3,'role':'Investigator'}),
    ('Mole','investigate',(3,),{}, {'target':3,'role':'Mole'}),
    ('Vigilante','shoot',(2,),{}, {'target':2}), ('Framer','frame',(2,),{}, {'target':2}),
    ('Gravedigger','hide',(2,),{}, {'target':2}), ('Transporter','transport',(1,2),{}, {'targets':[1,2]}),
    ('Bodyguard','protect',(2,),{}, {'target':2}), ('Bodyguard','protect',(1,),{}, {'target':1,'type':'bg_vest'}),
    ('Lookout','watch',(2,),{}, {'target':2}), ('Tracker','track',(2,),{}, {'target':2}),
    ('Witch','control',(2,2),{}, {'targets':[2,2]}), ('Arsonist','douse',(2,),{}, {'target':2}),
    ('Chaos','chaos',(1,2),{}, {'targets':[1,2]}),
    ('Hypnotist','hypnotize',(2,),{'message_type':'healed'}, {'target':2,'msg_type':'healed'}),
    ('Tailor','tailor',(2,),{'fake_role':'Doctor'}, {'target':2,'fake_role':'Doctor'}),
    ('Gatekeeper','guard',(2,),{}, {'target':2}), ('Survivor','vest',(),{}, {'target':1}),
    ('Scary Grandma','alert',(),{}, {}), ('Arsonist','ignite',(),{}, {}), ('Arsonist','clean',(),{}, {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('role,ability,targets,options,extra', CASES)
async def test_payload_parity_and_resources_are_not_spent_on_submit(world,role,ability,targets,options,extra):
    game, guild, _, _, _ = world
    game.player_roles[1] = role
    game.role_states[1] = {k:2 for k in ['uses_remaining','self_protects_remaining','shots_remaining','self_heals_remaining','vests_remaining','alerts_remaining']}
    before = deepcopy(game.role_states)
    result = await actions.submit(game,1,ability,targets,guild=guild,**options)
    assert result.accepted
    assert result.action == {'type':ability,'actor':1,**extra}
    assert game.role_states == before
    assert persistence.load_state(123)['night_actions']['1'] == result.action


@pytest.mark.asyncio
@pytest.mark.parametrize('role', sorted(actions.CORPSE_ROLES))
async def test_every_retributionist_corpse_format(world,role):
    game, guild, _, _, _ = world
    game.player_roles[1]='Retributionist'
    game.role_states[1]={'uses_remaining':2,'used_corpses':[]}
    game.graveyard=[{'player_id':99,'real_role':role}]
    result = await actions.submit(game,1,'reanimate',(1,3),corpse_id=99,guild=guild)
    assert result.accepted
    assert result.action['target']==1 and result.action['corpse_role']==role
    assert result.action.get('targets') == ([1,3] if role=='Transporter' else None)


@pytest.mark.asyncio
async def test_rejection_replacement_cooldown_and_stale_identity(world):
    game, guild, _, _, _ = world
    expected=st.identity(game)
    assert (await actions.submit(game,1,'heal',(1,),guild=guild)).accepted
    assert not (await actions.submit(game,1,'heal',(3,),guild=guild)).accepted
    assert not (await actions.submit(game,1,'heal',(2,),guild=guild,cooldown=False)).accepted  # revealed Mayor
    assert (await actions.submit(game,1,'heal',(3,),guild=guild,cooldown=False)).accepted
    game.gameplay['night_token']='next-night'
    assert not (await actions.submit(game,1,'heal',(1,),expected=expected,cooldown=False)).accepted
    game.resolving=True
    assert not (await actions.submit(game,1,'heal',(1,),cooldown=False)).accepted


@pytest.mark.asyncio
async def test_failed_save_rolls_back_and_cancelled_save_finishes_before_unlock(world):
    game, _, _, _, _ = world
    original=game.persist_flush
    game.persist_flush=AsyncMock(side_effect=OSError('disk full'))
    assert not (await actions.submit(game,1,'heal',(1,))).accepted
    assert not game.night_actions and not game.action_cooldowns
    entered,release=asyncio.Event(),asyncio.Event()
    async def persist():
        entered.set()
        await release.wait()
        await original()
    game.persist_flush=persist
    task=asyncio.create_task(actions.submit(game,1,'heal',(1,)))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert game.state_lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not game.state_lock.locked()
    assert persistence.load_state(123)['night_actions']['1']['type']=='heal'


async def new_trial(world):
    game,guild,*_=world
    game.phase='day'
    return await trials.start(game,role_ids=[GAME_OVERSEER_ROLE_ID],channel_id=10,guild=guild)


async def expire(game, token):
    game.gameplay['trial']['deadline']=(st.now()-timedelta(seconds=1)).isoformat()
    return await trials.advance(game,token)


@pytest.mark.asyncio
async def test_trial_latest_votes_mayor_visibility_and_atomic_jester_execution(world):
    game,guild,_,_,players=world
    trial=await new_trial(world); token=trial['id']
    await trials.cast(game,token,1,4,'nomination')
    await trials.cast(game,token,1,3,'nomination')
    await trials.cast(game,token,2,3,'nomination')
    assert trials.nomination_tally(game,trial)[3]==3
    assert 'Player 1' in trial_text(game,trial) and 'Weighted totals' in trial_text(game,trial)
    await expire(game,token)
    assert game.votes_today==1 and trial['stage']=='defense'
    await expire(game,token)
    await trials.cast(game,token,1,'guilty','judgment')
    await trials.cast(game,token,1,'innocent','judgment')
    await trials.cast(game,token,2,'guilty','judgment')
    public=trial_text(game,trial)
    assert 'Player 1' not in public and 'Guilty: ' not in public
    await expire(game,token)
    assert trial['result']['totals']=={'guilty':2,'innocent':1,'abstain':1}
    assert trial['result']['haunt_ids']==[2,4]
    # Eligibility and weights are frozen in the saved verdict.
    game.role_states[2]['is_revealed']=False
    await trials.apply_result(game,token)
    saved=persistence.load_state(123)
    assert saved['gameplay']['trial']['applied']
    assert saved['role_states']['3']['guilty_voters']==[2,4]
    assert saved['role_states']['3']['jester_won'] and saved['role_states']['4']['exe_won']
    assert len(game.graveyard)==1 and all(p.id!=3 for p in game.living_players)
    await trials.apply_result(game,token)
    assert len(game.graveyard)==1
    players[2].remove_roles.assert_not_called()  # No Discord mutation before durable application.


@pytest.mark.asyncio
@pytest.mark.parametrize('choice',[None,3])
async def test_zero_and_tied_nominations_do_not_consume_trial(world,choice):
    game,*_=world
    trial=await new_trial(world)
    if choice:
        game.role_states[2]['is_revealed']=False
        await trials.cast(game,trial['id'],1,3,'nomination')
        await trials.cast(game,trial['id'],2,4,'nomination')
    await expire(game,trial['id'])
    assert game.votes_today==0 and not game.vote_in_progress and trial['stage']=='done'


@pytest.mark.asyncio
async def test_trial_eligibility_deadline_limit_and_single_refund(world):
    game,*_=world
    trial=await new_trial(world); token=trial['id']
    with pytest.raises(st.Rejected): await trials.cast(game,token,1,1,'nomination')
    with pytest.raises(st.Rejected): await trials.cast(game,token,99,1,'nomination')
    await trials.cast(game,token,1,3,'nomination')
    await expire(game,token)
    await expire(game,token)
    with pytest.raises(st.Rejected): await trials.cast(game,token,3,'guilty','judgment')
    game.living_players=[p for p in game.living_players if p.id!=3]
    await expire(game,token)
    assert game.votes_today==0 and trial['refunded']
    trials.cancel_model(game,trial,'again',refund=True)
    assert game.votes_today==0
    game.votes_today=2
    with pytest.raises(st.Rejected): await trials.start(game,role_ids=[GAME_OVERSEER_ROLE_ID],channel_id=10)


@pytest.mark.asyncio
@pytest.mark.parametrize('point',['close','apply'])
async def test_verdict_write_failure_retries_without_duplicate_effects(world,point):
    game,*_=world
    trial=await new_trial(world); token=trial['id']
    await trials.cast(game,token,1,3,'nomination')
    await expire(game,token); await expire(game,token)
    await trials.cast(game,token,1,'guilty','judgment')
    if point=='apply': await expire(game,token)
    else: game.gameplay['trial']['deadline']=(st.now()-timedelta(seconds=1)).isoformat()
    original=game.persist_flush
    game.persist_flush=AsyncMock(side_effect=OSError('failed'))
    with pytest.raises(OSError):
        await (trials.apply_result(game,token) if point=='apply' else trials.advance(game,token))
    assert len(game.graveyard)==0
    assert game.gameplay['trial']['stage']==('closed' if point=='apply' else 'judgment')
    game.persist_flush=original
    if point=='close': await trials.advance(game,token)
    await trials.apply_result(game,token)
    assert len(game.graveyard)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('target,won',[('scissors',True),('rock',False),('paper',False)])
async def test_duel_first_choice_hidden_atomic_completion_and_resolution_guard(world,target,won):
    game,guild,controller,_,_=world
    game.player_roles[1]='Pirate'
    result=await actions.submit(game,1,'plunder',(3,)); action=result.action; token=action['duel_token']
    await duels.choose(game,1,token,1,'rock')
    with pytest.raises(st.Rejected): await duels.choose(game,1,token,1,'paper')
    text = '\n'.join(c.content for c in DuelView(controller,game,1,3,action).walk_children() if isinstance(c,discord.ui.TextDisplay))
    assert 'rock' not in text.lower() and 'Pirate chose' not in text
    assert not (await actions.submit(game,1,'plunder',(4,),cooldown=False)).accepted
    with pytest.raises(st.Rejected,match='still in progress'): await resolution.begin(game,guild)
    await duels.choose(game,1,token,3,target)
    await duels.complete(game,1,token)
    saved=persistence.load_state(123)['night_actions']['1']
    assert saved['duel_finished'] and saved['duel_won'] is won
    await duels.complete(game,1,token)
    assert game.night_actions[1]['duel_won'] is won


@pytest.mark.asyncio
async def test_duel_timeout_random_choices_and_snapshot_recovery(world,monkeypatch):
    game,guild,_,_,_=world
    game.player_roles[1]='Pirate'
    result=await actions.submit(game,1,'plunder',(3,)); token=result.action['duel_token']
    await duels.choose(game,1,token,1,'paper')
    result.action['duel_deadline']=(st.now()-timedelta(seconds=1)).isoformat()
    await game.persist_flush()
    recovered=Game.from_persisted(persistence.load_state(123))
    await recovered.rehydrate_members(guild); gm.active_games[123]=recovered
    assert not recovered.night_actions[1]['duel_finished']
    monkeypatch.setattr(duels.random,'choice',lambda seq:'rock')
    await duels.complete(recovered,1,token)
    assert recovered.night_actions[1]['duel_choices']=={'1':'paper','3':'rock'}
    assert recovered.night_actions[1]['duel_won']


@pytest.mark.asyncio
async def test_panels_private_fallback_no_repeat_bulk_send_and_reopen(world,monkeypatch):
    game,guild,controller,channels,players=world
    monkeypatch.setattr('gameplay.controller.PLAYER_PRIVATE_CHANNEL_IDS',{1:10})
    channels[10].readers={0,1,2}
    assert not channel_is_private(channels[10],guild,1)
    await controller.send_night_panels(game)
    assert channels[10].sent==[]
    assert len(channels[101].sent)==1
    await controller.send_night_panels(game)
    assert len(channels[101].sent)==1
    assert game.gameplay['panels']['1']['channel_id']==101
    game.start_night  # Existing phase must preserve submissions and the same panel refs.
    await actions.submit(game,1,'heal',(1,))
    await game.start_night(SimpleNamespace(guild=guild,send=AsyncMock()))
    assert game.night_actions[1]['type']=='heal' and len(channels[101].sent)==1


@pytest.mark.asyncio
async def test_controls_owner_and_stale_night_and_lists_beyond_25(world):
    game,_,controller,_,players=world
    for uid in range(5,61):
        player=SimpleNamespace(id=uid,display_name=f'Player {uid}')
        game.players.append(player); game.living_players.append(player); game.player_slots[uid]=uid
    view=NightPanel(controller,game,1)
    i=SimpleNamespace(user=SimpleNamespace(id=99),response=SimpleNamespace(is_done=lambda:False,send_message=AsyncMock()))
    assert not await view.interaction_check(i)
    i.user.id=1; game.gameplay['night_token']='later'
    assert not await view.interaction_check(i)
    draft=Draft(controller,game,1,'heal')
    selects=[c for c in draft.walk_children() if isinstance(c,discord.ui.Select)]
    assert len(selects[0].options)==25
    assert any(isinstance(c,discord.ui.Button) and c.label=='Next' and not c.disabled for c in draft.walk_children())
    picker=NominationPicker(controller,game,'sample',1,page=2)
    assert len([c for c in picker.walk_children() if isinstance(c,discord.ui.Select)][0].options)==9
    assert all(len(c.custom_id)<=100 for c in draft.walk_children() if hasattr(c,'custom_id') and c.custom_id)


@pytest.mark.asyncio
async def test_recovery_preserves_expired_deadline_and_single_controller(world):
    game,guild,controller,_,_=world
    trial=await new_trial(world); token=trial['id']
    await trials.cast(game,token,1,3,'nomination')
    await expire(game,token)
    game.gameplay['trial']['deadline']=(st.now()-timedelta(seconds=100)).isoformat()
    await game.persist_flush()
    recovered=Game.from_persisted(persistence.load_state(123))
    await recovered.rehydrate_members(guild); gm.active_games[123]=recovered
    controller.run_trial=AsyncMock()
    await controller.recover(recovered); await controller.recover(recovered)
    assert len(controller.jobs)==1
    await asyncio.gather(*controller.jobs.values())
    assert controller.run_trial.await_count==1
    await trials.advance(recovered,token)
    assert 28 < st.remaining(recovered.gameplay['trial']['deadline']) <=30


@pytest.mark.asyncio
async def test_resolution_checkpoint_recovery_does_not_spend_resources_twice(world):
    game,guild,_,channels,_=world
    game.player_roles[1]='Vigilante'; game.role_states[1]={'shots_remaining':1}
    game.player_roles[3]='Mobster'
    await actions.submit(game,1,'shoot',(3,))
    ctx=SimpleNamespace(guild=guild,send=channels[10].send)
    original=resolution.finish
    # Simulate process interruption after durable application, before delivery.
    import unittest.mock
    with unittest.mock.patch.object(resolution,'finish',AsyncMock(side_effect=OSError('delivery offline'))):
        with pytest.raises(OSError): await resolution.run(game,ctx)
    saved=persistence.load_state(123)
    recovered=Game.from_persisted(saved); await recovered.rehydrate_members(guild)
    recovered.check_win_conditions=AsyncMock(return_value=False); gm.active_games[123]=recovered
    assert recovered.resolving and recovered.role_states[1]['shots_remaining']==0
    await original(recovered,ctx)
    assert recovered.phase=='day' and recovered.day_number==2
    assert len(recovered.graveyard)==1 and recovered.role_states[1]['shots_remaining']==0
    assert recovered.gameplay['resolution']['progressed']


@pytest.mark.asyncio
@pytest.mark.parametrize('role,ability,targets,options,extra',CASES)
async def test_existing_command_and_component_service_have_identical_payloads(world,monkeypatch,role,ability,targets,options,extra):
    game,guild,controller,_,players=world
    monkeypatch.setenv('DISCORD_TOKEN','test-token')
    import bot as module
    monkeypatch.setattr(module,'active_games',gm.active_games)
    monkeypatch.setattr(module,'bot',controller.bot)
    game.player_roles[1]=role
    game.role_states[1]={k:2 for k in ['uses_remaining','self_protects_remaining','shots_remaining','self_heals_remaining','vests_remaining','alerts_remaining']}
    ctx=SimpleNamespace(game=game,guild=guild,author=players[0],interaction=None,send=AsyncMock())
    command=getattr(module,ability)
    callback = getattr(command.callback, "__wrapped__", command.callback)
    if ability == 'shoot':
        # Other cases call the undecorated submission handler too. The public
        # shoot dispatcher has a separate test for its DM/privacy boundary.
        callback = module._vig_shoot_night.__wrapped__
    await callback(ctx,*targets,**options)
    from_command=deepcopy(game.night_actions[1])
    game.night_actions.clear(); game.action_cooldowns.clear()
    result=await actions.submit(game,1,ability,targets,guild=guild,**options)
    assert result.accepted and result.action==from_command
    assert ctx.send.call_args.kwargs['ephemeral'] is True


@pytest.mark.asyncio
async def test_v2_private_panel_payload_has_no_ordinary_content(world):
    from gameplay.views import private_reply
    game,_,controller,_,_=world
    interaction=SimpleNamespace(response=SimpleNamespace(is_done=lambda:True),followup=SimpleNamespace(send=AsyncMock()))
    await private_reply(interaction,'private controls',view=NightPanel(controller,game,1))
    args,kwargs=interaction.followup.send.call_args
    assert args==() and 'content' not in kwargs and kwargs['ephemeral'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('checkpoint',['result','death-announced','permissions-cleaned','finished'])
async def test_trial_restart_at_completion_checkpoints_never_reapplies_death(world,checkpoint):
    game,guild,controller,channels,_=world
    trial=await new_trial(world); token=trial['id']
    await trials.cast(game,token,1,3,'nomination'); await expire(game,token); await expire(game,token)
    await trials.cast(game,token,1,'guilty','judgment'); await expire(game,token)
    if checkpoint != 'result': await trials.apply_result(game,token)
    if checkpoint=='death-announced':
        await game.deliver_death_receipt(channels[10],guild,game.gameplay['deaths']['3'])
    if checkpoint=='permissions-cleaned':
        game.gameplay['trial']['permissions_cleaned']=True
    if checkpoint=='finished': await trials.finish(game,token)
    await game.persist_flush()
    recovered=Game.from_persisted(persistence.load_state(123)); await recovered.rehydrate_members(guild)
    gm.active_games[123]=recovered; recovered.check_win_conditions=AsyncMock(return_value=False)
    await controller.run_trial(recovered,token)
    assert len(recovered.graveyard)==1 and recovered.role_states[3]['jester_won']
    assert recovered.role_states[4]['exe_won'] and recovered.gameplay['trial']['applied']
    assert recovered.gameplay['trial']['progressed'] and recovered.phase=='night'
    announcements=[text for text,kwargs in channels[10].sent if text and 'sentenced to the gallows' in text]
    assert len(announcements)==1
    assert not recovered.state_lock.locked()


@pytest.mark.asyncio
async def test_defendant_dies_between_calculation_and_application_refunds_without_execution(world):
    from gameplay.death import apply_death
    game,_,controller,_,_=world
    trial=await new_trial(world); token=trial['id']
    await trials.cast(game,token,1,3,'nomination'); await expire(game,token); await expire(game,token)
    await trials.cast(game,token,1,'guilty','judgment'); await expire(game,token)
    await st.commit(game,lambda:apply_death(game,3,'manual'))
    await controller.run_trial(game,token)
    assert game.votes_today==0 and game.gameplay['trial']['refunded']
    assert not game.role_states[3].get('jester_won') and game.player_roles[4]=='Jester'
    assert 'cancelled' in trial_text(game,game.gameplay['trial']).lower()


@pytest.mark.asyncio
async def test_duel_completed_receipt_survives_replacement_and_recovery(world):
    game,guild,controller,_,_=world
    game.player_roles[1]='Pirate'
    first=await actions.submit(game,1,'plunder',(3,)); token=first.action['duel_token']
    await duels.choose(game,1,token,1,'rock'); await duels.choose(game,1,token,3,'scissors')
    await duels.complete(game,1,token)
    await actions.submit(game,1,'plunder',(4,),cooldown=False)
    with pytest.raises(st.Rejected): await duels.choose(game,1,token,3,'paper')
    recovered=Game.from_persisted(persistence.load_state(123)); await recovered.rehydrate_members(guild); gm.active_games[123]=recovered
    await controller.run_duel(recovered,1,token)
    assert recovered.gameplay['duels'][token]['duel_won']
    assert set(recovered.gameplay['duels'][token]['duel_delivered'])=={1,3}


@pytest.mark.asyncio
async def test_missing_trial_message_is_recreated_and_missing_channel_cleans_voice(world):
    game,_,controller,channels,_=world
    trial=await new_trial(world)
    await controller.render_trial(game)
    channels[10].messages.clear()
    await controller.render_trial(game)
    assert channels[10].messages
    channels.pop(10)
    await controller.run_trial(game,trial['id'])
    assert not game.vote_in_progress and game.gameplay['trial']['stage']=='cancelled'
    channels[11].set_permissions.assert_awaited()


@pytest.mark.asyncio
async def test_blocked_duel_dms_still_complete_at_original_timeout(world):
    game,_,controller,_,players=world
    game.player_roles[1]='Pirate'
    result=await actions.submit(game,1,'plunder',(3,)); token=result.action['duel_token']
    result.action['duel_deadline']=(st.now()-timedelta(seconds=1)).isoformat()
    for p in players:
        p.create_dm=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='blocked'),'blocked'))
    await controller.run_duel(game,1,token)
    assert game.night_actions[1]['duel_finished'] and len(game.night_actions[1]['duel_choices'])==2


@pytest.mark.asyncio
async def test_departure_lookup_cannot_resurrect_a_concurrent_death(world):
    from gameplay.death import apply_death
    game,guild,_,_,_=world
    real=game.lookup_member
    entered,release=asyncio.Event(),asyncio.Event()
    async def slow(guild,uid):
        if uid==3:
            entered.set(); await release.wait()
        return await real(guild,uid)
    game.lookup_member=slow
    sync=asyncio.create_task(game.sync_living_players(guild)); await entered.wait()
    await st.commit(game,lambda:apply_death(game,3,'manual'))
    release.set(); await sync
    assert 3 not in {p.id for p in game.living_players}


def test_malformed_recovery_records_fail_closed(world):
    from gameplay.records import load_ui,repair_duel
    raw={'version':1,'trial':{'id':'x','day':1,'stage':'judgment','deadline':'invalid','channel_id':10,
                             'nominations':{'1':[2]},'judgments':{'2':[]}},'panels':{'1':{}},'deaths':{'3':None}}
    assert load_ui(raw)['trial'] is None and load_ui(raw)['panels']=={}
    action={'type':'plunder','actor':1,'target':2,'duel_token':'x','duel_day':1,'duel_match':'x',
            'duel_deadline':st.now().isoformat(),'duel_choices':{'1':[]}}
    repair_duel(action,day=1,match='x')
    assert action['duel_choices']=={}


@pytest.mark.parametrize('stage',[{},[],0,None])
def test_invalid_trial_stage_never_crashes_recovery(world,stage):
    from gameplay.records import load_ui
    raw={'version':1,'trial':{'id':'x','day':1,'stage':stage,'channel_id':10,
                             'nominations':{},'judgments':{}}}
    assert load_ui(raw)['trial'] is None


@pytest.mark.parametrize('finished,won',[(True,'false'),('false',True),(1,True)])
def test_invalid_saved_duel_result_cannot_become_a_victory(world,finished,won):
    from gameplay.records import repair_duel
    action={'type':'plunder','actor':1,'target':2,'duel_token':'x','duel_day':1,'duel_match':'test-match',
            'duel_deadline':st.now().isoformat(),'duel_finished':finished,'duel_won':won}
    repair_duel(action,day=1,match='test-match')
    assert action['duel_finished'] is True and action['duel_won'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['new-match','different-owner','replaced-game'])
async def test_will_modal_cannot_edit_another_owner_or_later_match(world,monkeypatch,change):
    game,*_=world
    monkeypatch.setenv('DISCORD_TOKEN','test-token')
    import bot as module
    game.role_states[1]['will']='previous'
    modal=module.WillModal(game=game,owner_id=1,current_text='previous')
    modal.will._value='replacement'
    if change=='new-match': game.game_key='later-match'
    if change=='replaced-game': gm.active_games[123]=Game(123)
    i=SimpleNamespace(user=SimpleNamespace(id=2 if change=='different-owner' else 1),
        response=SimpleNamespace(defer=AsyncMock(),is_done=lambda:True),followup=SimpleNamespace(send=AsyncMock()))
    await modal.on_submit(i)
    assert game.role_states[1]['will']=='previous'
    assert 'Saved your will.' not in str(i.followup.send.call_args)


@pytest.mark.asyncio
async def test_will_modal_keeps_working_across_phases_in_the_same_match(world,monkeypatch):
    game,*_=world
    monkeypatch.setenv('DISCORD_TOKEN','test-token')
    import bot as module
    modal=module.WillModal(game=game,owner_id=1,current_text='')
    modal.will._value='my will'
    game.phase='day'; game.day_number+=1
    i=SimpleNamespace(user=SimpleNamespace(id=1),response=SimpleNamespace(defer=AsyncMock(),is_done=lambda:True),
                      followup=SimpleNamespace(send=AsyncMock()))
    await modal.on_submit(i)
    assert persistence.load_state(123)['role_states']['1']['will']=='my will'
    assert i.followup.send.call_args.kwargs['content']=='Saved your will.'


@pytest.mark.asyncio
async def test_will_button_rejects_an_editor_from_a_later_match(world,monkeypatch):
    game,*_=world
    monkeypatch.setenv('DISCORD_TOKEN','test-token')
    import bot as module
    view=module.WillView(game=game,owner_id=1,current_text='')
    game.game_key='later-match'
    i=SimpleNamespace(user=SimpleNamespace(id=1),response=SimpleNamespace(is_done=lambda:False,
                      send_modal=AsyncMock(),send_message=AsyncMock()))
    await view.edit.callback(i)
    i.response.send_modal.assert_not_awaited()
    assert i.response.send_message.call_args.kwargs['ephemeral']


@pytest.mark.asyncio
async def test_concurrent_resolution_delivery_sends_feedback_and_advances_once(world):
    game,guild,_,channels,players=world
    game.resolving=True
    game.gameplay['resolution']={'night_token':'night-one','applied':True,'progressed':False,
        'death_ids':[],'feedback':[{'user_id':1,'text':'Your result'}],'feedback_index':0}
    entered,release=asyncio.Event(),asyncio.Event()
    async def send(text):
        entered.set(); await release.wait()
    players[0].send=AsyncMock(side_effect=send)
    ctx=SimpleNamespace(guild=guild,send=channels[10].send)
    first=asyncio.create_task(resolution.finish(game,ctx)); await entered.wait()
    second=asyncio.create_task(resolution.finish(game,ctx)); await asyncio.sleep(0)
    release.set(); await asyncio.gather(first,second)
    players[0].send.assert_awaited_once()
    assert game.phase=='day' and game.day_number==2 and not game.resolving


@pytest.mark.asyncio
async def test_completed_trial_receipt_does_not_restart_on_a_later_day(world):
    game,_,controller,channels,_=world
    trial=await new_trial(world)
    trial.update(stage='done',progressed=True)
    game.vote_in_progress=False; game.day_number+=1
    game.game_channel_id=99; channels.pop(10)
    await controller.recover(game)
    assert not controller.jobs and game.gameplay['trial']['id']==trial['id']


@pytest.mark.asyncio
async def test_pending_day_death_delivery_is_supervised_after_recovery(world):
    from gameplay.death import apply_death
    game,_,controller,channels,_=world
    game.phase='day'
    await st.commit(game,lambda:apply_death(game,1,'manual'))
    await controller.recover(game)
    await asyncio.gather(*controller.jobs.values())
    assert game.gameplay['deaths']['1']['delivered']
    assert len([text for text,kwargs in channels[10].sent if text and 'was found dead' in text])==1


@pytest.mark.asyncio
async def test_duel_click_rechecks_server_departures_before_persistence(world):
    game,guild,_,_,_=world
    game.player_roles[1]='Pirate'
    result=await actions.submit(game,1,'plunder',(3,)); token=result.action['duel_token']
    game.lookup_member=AsyncMock(side_effect=lambda g,uid: g.get_member(uid) if uid!=3 else None)
    with pytest.raises(st.Rejected): await duels.choose(game,1,token,3,'rock',guild=guild)
    assert not game.night_actions[1]['duel_choices']


@pytest.mark.asyncio
async def test_day_transition_removes_completed_night_controls(world):
    game,guild,controller,channels,_=world
    await controller.send_panel(game,1)
    reference=game.gameplay['panels']['1']
    await game.start_day(SimpleNamespace(guild=guild,send=channels[10].send))
    view=channels[101].messages[reference['message_id']].view
    assert not any(isinstance(item,(discord.ui.Button,discord.ui.Select)) for item in view.walk_children())


@pytest.mark.asyncio
async def test_undelivered_archived_duel_can_reopen_after_replacement_and_at_day(world):
    game,_,controller,channels,_=world
    game.player_roles[1]='Pirate'
    first=await actions.submit(game,1,'plunder',(3,)); token=first.action['duel_token']
    await duels.choose(game,1,token,1,'rock'); await duels.choose(game,1,token,3,'scissors')
    await duels.complete(game,1,token)
    await actions.submit(game,1,'plunder',(4,),cooldown=False)
    game.phase='day'; game.day_number+=1
    view=controller.panel_for(game,3)
    assert isinstance(view,DuelView) and view.token==token
    assert all(item.disabled for item in view.walk_children() if isinstance(item,discord.ui.Button))
    await controller.reopen(game,3)
    assert 3 in game.gameplay['duels'][token]['duel_delivered'] and channels[103].sent


@pytest.mark.asyncio
async def test_submitted_panel_displays_options_and_hides_pending_duel_choices(world):
    game,_,controller,_,_=world
    def content():
        return '\n'.join(item.content for item in NightPanel(controller,game,1).walk_children() if isinstance(item,discord.ui.TextDisplay))
    game.player_roles[1]='Tailor'; game.role_states[1]={'uses_remaining':1}
    await actions.submit(game,1,'tailor',(3,),fake_role='Doctor')
    assert 'disguise: Doctor' in content()
    game.player_roles[1]='Pirate'
    result=await actions.submit(game,1,'plunder',(3,),cooldown=False)
    await duels.choose(game,1,result.action['duel_token'],3,'scissors')
    text=content().lower()
    assert 'duel in progress' in text and 'scissors' not in text


@pytest.mark.asyncio
async def test_resolution_with_deleted_public_channels_advances_and_retries_notices_after_restart(world):
    from gameplay.death import apply_death
    game,guild,controller,channels,_=world
    game.resolving=True
    game.player_roles[1]='Vigilante'; game.role_states[1]={'shots_remaining':0}
    def applied():
        apply_death(game,3,'night_kill')
        game.gameplay['resolution']={'night_token':'night-one','day':1,'applied':True,'progressed':False,
            'death_ids':[3],'feedback':[],'feedback_index':0,'public_delivery_pending':False}
    await st.commit(game,applied)
    public=channels.pop(10)
    await controller.recover_resolution(game)
    await controller.stop_game(game)
    assert game.phase=='day' and game.day_number==2 and not game.resolving
    assert not game.state_lock.locked() and not game.gameplay['deaths']['3']['delivered']
    assert game.gameplay['resolution']['public_delivery_pending']
    recovered=Game.from_persisted(persistence.load_state(123)); await recovered.rehydrate_members(guild)
    recovered.check_win_conditions=AsyncMock(return_value=False); gm.active_games[123]=recovered
    channels[10]=public
    await controller.deliver_deferred_resolution(recovered,'night-one')
    assert recovered.gameplay['deaths']['3']['delivered']
    assert not recovered.gameplay['resolution']['public_delivery_pending']
    assert len(recovered.graveyard)==1 and recovered.role_states[1]['shots_remaining']==0
    assert recovered.phase=='day' and recovered.day_number==2


@pytest.mark.asyncio
async def test_completed_resolution_receipt_survives_the_next_night_identity(world):
    game,guild,_,channels,_=world
    game.phase='day'
    game.gameplay['resolution']={'night_token':'night-one','applied':True,'progressed':True,
        'death_ids':[],'feedback':[],'feedback_index':0,'public_delivery_pending':True}
    await game.start_night(SimpleNamespace(guild=guild,send=channels[10].send))
    recovered=Game.from_persisted(persistence.load_state(123))
    assert recovered.gameplay['resolution']['night_token']=='night-one'
    assert recovered.gameplay['resolution']['public_delivery_pending'] and not recovered.resolving


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [403, 503])
async def test_modern_trial_delivery_retry_keeps_ballots_and_deadline(world, monkeypatch, status):
    game, _, controller, channels, _ = world
    trial = await new_trial(world)
    token = trial['id']
    await trials.cast(game, token, 1, 3, 'nomination')
    await expire(game, token)
    await expire(game, token)
    await trials.cast(game, token, 1, 'guilty', 'judgment')
    await controller.render_trial(game)
    saved = deepcopy(persistence.load_state(123)['gameplay']['trial'])
    channel = channels[10]
    original_fetch, original_sleep = channel.fetch_message, asyncio.sleep
    error_type = discord.Forbidden if status == 403 else discord.HTTPException
    error = error_type(SimpleNamespace(status=status, reason='unavailable'), 'unavailable')
    calls = 0

    async def fail_once(mid):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return await original_fetch(mid)

    async def retry_without_delay(delay):
        await original_sleep(0)

    monkeypatch.setattr(channel, 'fetch_message', fail_once)
    monkeypatch.setattr(asyncio, 'sleep', retry_without_delay)
    await controller.start_job((game.guild_id, 'trial-test', token), lambda: controller.render_trial(game))
    assert calls == 2 and len(channel.messages) == 1
    assert game.gameplay['trial'] == saved
    assert persistence.load_state(123)['gameplay']['trial'] == saved
    assert not game.state_lock.locked()
