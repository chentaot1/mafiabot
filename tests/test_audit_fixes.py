"""Behavioral regressions for the verified lifecycle, recovery and output audit."""
import asyncio
import ctypes
import importlib
import os
import sqlite3
import sys
import threading
import time
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

import game as gm
import persistence
from async_work import run_blocking
from database import Database
from game import Game, MemberLookupUnavailable
from gameplay import actions, resolution, startup, state as st, trials
from gameplay.controller import Controller
from gameplay.death import apply_death
from gameplay.records import load_ui
from test_gameplay import world, Channel, new_trial, expire


@pytest.fixture
def module(world, monkeypatch):
    import bot
    monkeypatch.setattr(bot, 'active_games', gm.active_games)
    monkeypatch.setattr(bot, 'ALLOWED_GUILD_ID', 123)
    monkeypatch.setattr(bot, '_dbg', lambda *args, **kwargs: None)
    return bot


def http_error(status):
    cls = discord.Forbidden if status == 403 else discord.NotFound if status == 404 else discord.HTTPException
    return cls(S(status=status, reason='test error'), 'test error')


async def stop(controller, game):
    await controller.stop_game(game)


@pytest.mark.asyncio
async def test_startgame_command_has_a_real_startup_lock(module, world):
    game, guild, _, _, _ = world
    game.in_progress = False
    ctx = S(guild=guild, send=AsyncMock())
    await module.startgame.callback(ctx)
    assert 'at least 5' in ctx.send.call_args.args[0]
    assert not game._startup_lock.locked()


class Principal:
    def __init__(self, uid):
        self.id = uid


def test_private_acl_has_explicit_bot_access_and_no_stale_allows():
    from gameplay.access import private_overwrites
    default, alive, bot, playing, overseer = [Principal(x) for x in range(5)]
    from config import PLAYING_ROLE_ID, GAME_OVERSEER_ROLE_ID
    guild = S(default_role=default, me=bot,
        get_role=lambda uid: {PLAYING_ROLE_ID: playing, GAME_OVERSEER_ROLE_ID: overseer}.get(uid))
    acl = private_overwrites(guild, alive)
    assert set(acl) == {default, alive, bot, playing, overseer}
    assert all(acl[p].view_channel is False for p in (default, alive, playing))
    assert all(getattr(acl[bot], p) is True for p in ('view_channel', 'send_messages', 'read_message_history'))


@pytest.mark.asyncio
async def test_startup_resume_preserves_assignment_and_finishes_missing_player(world):
    game, guild, _, _, players = world
    game.phase = 'day'
    game.gameplay['startup'] = {'match': game.game_key, 'complete': False, 'announced': False, 'completed_players': []}
    roles = dict(game.player_roles)
    players[1].add_roles.side_effect = http_error(503)
    with pytest.raises(discord.HTTPException):
        await startup.deliver(game, guild, client=S(db=None))
    assert game.gameplay['startup']['completed_players'] == [1]
    assert not game.gameplay['startup']['complete']
    with pytest.raises(st.Rejected):
        await game.start_night(S(guild=guild, send=AsyncMock()))
    saved = persistence.load_state(123)
    recovered = Game.from_persisted(saved)
    await recovered.rehydrate_members(guild)
    gm.active_games[123] = recovered
    players[1].add_roles.side_effect = None
    players[0].send.reset_mock()
    await startup.deliver(recovered, guild, client=S(db=None))
    assert recovered.gameplay['startup']['complete']
    assert recovered.player_roles == roles
    players[0].send.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_confirmed_departure_is_recorded_and_does_not_stall(world):
    game, guild, _, _, _ = world
    game.gameplay['startup'] = {'match': game.game_key, 'complete': False, 'announced': False, 'completed_players': []}
    game.lookup_member = AsyncMock(side_effect=lambda guild, uid: guild.get_member(uid) if uid != 3 else None)
    await startup.deliver(game, guild, client=S(db=None))
    assert game.gameplay['startup']['complete']
    assert game.role_states[3]['death_cause'] == 'left'
    assert game.player_roles[4] == 'Jester'


@pytest.mark.asyncio
async def test_reset_drains_in_flight_startup_api_call(world):
    game, guild, controller, _, players = world
    entered, release = asyncio.Event(), asyncio.Event()
    game.gameplay['startup'] = {'match': game.game_key, 'complete': False, 'completed_players': []}
    async def grant(*roles):
        entered.set()
        await release.wait()
    players[0].add_roles.side_effect = grant
    async def initialize():
        async with game._startup_lock:
            await startup.deliver(game, guild, client=S(db=None))
    job = asyncio.create_task(initialize())
    await asyncio.wait_for(entered.wait(), 2)
    cleanup = AsyncMock()
    game._historical_reset = cleanup
    reset = asyncio.create_task(game.reset(guild))
    await asyncio.sleep(.02)
    assert game.ending and not reset.done()
    cleanup.assert_not_awaited()
    release.set()
    with pytest.raises(st.Rejected):
        await job
    await asyncio.wait_for(reset, 2)
    cleanup.assert_awaited_once()
    assert not game._startup_lock.locked() and not game.state_lock.locked()
    await stop(controller, game)


@pytest.mark.asyncio
async def test_reset_cancellation_drains_supervised_startup_request(world):
    game, guild, controller, _, players = world
    entered, release = asyncio.Event(), asyncio.Event()
    game.gameplay['startup'] = {'match':game.game_key, 'complete':False, 'announced':False, 'completed_players':[]}
    async def grant(*roles):
        entered.set()
        await release.wait()
        players[0].roles.extend(roles)
    players[0].add_roles.side_effect = grant
    controller.start_job((123,'startup',game.game_key), lambda: startup.resume(game, guild, client=S(db=None)))
    await asyncio.wait_for(entered.wait(), 2)
    async def cleanup(*args, **kwargs):
        players[0].roles.clear()
    game._historical_reset = AsyncMock(side_effect=cleanup)
    reset = asyncio.create_task(game.reset(guild))
    await asyncio.sleep(.02)
    game._historical_reset.assert_not_awaited()
    assert not reset.done()
    release.set()
    await asyncio.wait_for(reset, 2)
    assert not players[0].roles and not game._startup_lock.locked()


@pytest.mark.asyncio
async def test_slow_public_display_cannot_delay_nomination_closing(world, monkeypatch):
    game, _, controller, _, _ = world
    trial = await new_trial(world)
    await trials.cast(game, trial['id'], 1, 3, 'nomination')
    trial['deadline'] = (st.now()+timedelta(seconds=1)).isoformat()
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_display(*args, **kwargs):
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr(controller, 'render_trial', slow_display)
    task = asyncio.create_task(controller.run_trial(game, trial['id']))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(.4)
        assert trial['stage'] == 'defense' and trial['defendant'] == 3
        assert not release.is_set() and game.votes_today == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stop(controller, game)


@pytest.mark.asyncio
async def test_slow_verdict_message_cannot_delay_phase_progression(world):
    game, _, controller, channels, _ = world
    trial = await new_trial(world)
    await controller.render_trial(game)
    trial.update(stage='judgment', defendant=3, counted=True,
        deadline=(st.now()-timedelta(seconds=1)).isoformat(), judgments={'1':'guilty','2':'guilty'})
    entered, release = asyncio.Event(), asyncio.Event()
    fetch = channels[10].fetch_message
    async def slow_fetch(mid):
        entered.set()
        await release.wait()
        return await fetch(mid)
    channels[10].fetch_message = slow_fetch
    try:
        await asyncio.wait_for(controller.run_trial(game, trial['id']), 2)
        await asyncio.wait_for(entered.wait(), 2)
        assert game.phase == 'night' and trial['applied'] and not game.vote_in_progress
        assert not release.is_set()
    finally:
        release.set()
        await stop(controller, game)


@pytest.mark.asyncio
async def test_cancelled_stats_worker_cannot_observe_cleared_match(world, monkeypatch):
    game, guild, _, _, _ = world
    entered, release = threading.Event(), threading.Event()
    recorded = []
    def write(self, **kwargs):
        entered.set()
        assert release.wait(3)
        recorded.append((self.game_key, dict(self.player_roles)))
        self.stats_committed = True
    monkeypatch.setattr(Game, '_commit_endgame_stats', write)
    task = asyncio.create_task(game.commit_endgame_stats_async(outcome='town', living_ids=[1, 2]))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    game._historical_reset = AsyncMock()
    reset = asyncio.create_task(game.reset(guild))
    await asyncio.sleep(.02)
    assert not reset.done() and game.game_key == 'test-match'
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await reset
    assert recorded == [('test-match', {1:'Doctor', 2:'Mayor', 3:'Jester', 4:'Executioner'})]
    assert not game.state_lock.locked()


@pytest.mark.asyncio
async def test_hydration_blocks_mutation_and_keeps_saved_roster(world):
    original, guild, _, _, _ = world
    await original.persist_flush()
    game = gm.get_game_for_guild(123, allowed_guild_id=123)
    gm.active_games.pop(123)
    game = gm.get_game_for_guild(123, allowed_guild_id=123)
    assert game._rehydrate_pending and not game.players
    entered, release = asyncio.Event(), asyncio.Event()
    async def lookup(guild, uid):
        entered.set()
        await release.wait()
        return guild.get_member(uid)
    game.lookup_member = lookup
    hydrate = asyncio.create_task(game.ensure_rehydrated(guild))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(st.Rejected):
        await st.commit(game, lambda: setattr(game, 'day_number', 99))
    await game.persist_flush()
    assert persistence.load_state(123)['living_ids'] == [1, 2, 3, 4]
    release.set()
    await hydrate
    assert {p.id for p in game.players} == {1, 2, 3, 4}
    assert game.day_number == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [False, True])
@pytest.mark.parametrize('status', [403, 503])
async def test_membership_lookup_failure_preserves_every_death_effect(world, recovered, status):
    game, guild, _, _, _ = world
    game.player_roles[1] = 'Guardian Angel'
    game.role_states[1] = {'ga_target_id':3, 'ga_defeated':False}
    before = game.to_persisted()
    if recovered:
        game = Game.from_persisted(before)
        game._rehydrate_pending = True
        gm.active_games[123] = game
    guild.get_member = lambda uid: None
    guild.fetch_member = AsyncMock(side_effect=http_error(status))
    with pytest.raises(MemberLookupUnavailable):
        await game.sync_living_players(guild)
    after = game.to_persisted()
    for key in ('player_roles','role_states','player_ids','living_ids','graveyard'):
        assert after[key] == before[key]
    assert not game.gameplay['deaths']


@pytest.mark.asyncio
async def test_confirmed_departure_applies_shared_death_bookkeeping(world):
    game, guild, controller, _, _ = world
    game.player_roles[1] = 'Guardian Angel'
    game.role_states[1] = {'ga_target_id':3, 'ga_defeated':False}
    cached = guild.get_member
    guild.get_member = lambda uid: cached(uid) if uid != 3 else None
    guild.fetch_member = AsyncMock(side_effect=http_error(404))
    await game.sync_living_players(guild)
    assert 3 not in {p.id for p in game.living_players}
    assert game.graveyard[0]['cause'] == 'left'
    assert game.role_states[1]['ga_defeated'] and game.player_roles[4] == 'Jester'
    assert game.gameplay['deaths']['3']['access_cleaned']
    await stop(controller, game)


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [False, True])
async def test_existing_graveyard_entry_remains_dead_without_reapplying_death(world, recovered):
    game, guild, _, _, _ = world
    game.graveyard = [{'player_id':'NaN','real_role':'Doctor'}, {'player_id':3,'real_role':'Jester','cause':'lynch'}]
    game.role_states[3] = {'jester_won':True, 'death_cause':'lynch'}
    game.role_states[4]['exe_won'] = True
    if recovered:
        game = Game.from_persisted(game.to_persisted())
        gm.active_games[123] = game
        await game.rehydrate_members(guild)
    else:
        await game.sync_living_players(guild)
    assert 3 not in {p.id for p in game.living_players}
    assert game.role_states[3]['death_cause'] == 'lynch'
    assert game.role_states[4]['exe_won'] and game.player_roles[4] == 'Executioner'
    assert not game.gameplay['deaths']


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [403, 503])
async def test_expired_trial_closes_despite_public_api_failure(world, status):
    game, _, controller, channels, _ = world
    trial = await new_trial(world)
    await controller.render_trial(game)
    trial.update(stage='judgment', defendant=3, counted=True,
        deadline=(st.now()-timedelta(seconds=1)).isoformat(), judgments={'1':'guilty','2':'guilty'})
    channels[10].fetch_message = AsyncMock(side_effect=http_error(status))
    await asyncio.wait_for(controller.run_trial(game, trial['id']), 2)
    assert trial['applied'] and trial['result']['guilty']
    assert game.phase == 'night' and not game.vote_in_progress
    assert game.role_states[3]['jester_won'] and game.role_states[4]['exe_won']
    assert game.gameplay['trials'][trial['id']]['delivery_pending']
    await stop(controller, game)


@pytest.mark.asyncio
async def test_unavailable_public_channel_does_not_keep_mafia_access(world):
    game, guild, controller, channels, players = world
    game.player_roles[3] = 'Mobster'
    players[2].roles = [guild.get_role(game.alive_role_id)]
    game.mafia_tc_id = 55
    channels[55] = Channel(55, guild)
    await st.commit(game, lambda: apply_death(game, 3, 'night_kill'))
    await game.deliver_death_receipt(None, guild, game.gameplay['deaths']['3'])
    players[2].remove_roles.assert_awaited_once()
    channels[55].set_permissions.assert_awaited_with(players[2], overwrite=None)
    receipt = game.gameplay['deaths']['3']
    assert receipt['access_cleaned'] and not receipt['delivered']
    await stop(controller, game)


@pytest.mark.asyncio
async def test_old_resolution_delivery_survives_a_later_resolution(world):
    game, guild, controller, channels, _ = world
    first = {'night_token':'night-one','day':1,'applied':True,'progressed':True,
        'feedback':[],'feedback_index':0,'death_ids':[3],'public_delivery_pending':True}
    await st.commit(game, lambda: apply_death(game, 3, 'night_kill'))
    game.gameplay['resolution'] = {'night_token':'night-two','applied':True,'progressed':True,
        'feedback':[],'feedback_index':0,'death_ids':[],'public_delivery_pending':False}
    game.gameplay['resolutions'] = {'night-one':first, 'night-two':game.gameplay['resolution']}
    game.phase = 'day'
    await game.persist_flush()
    restored = Game.from_persisted(persistence.load_state(123))
    await restored.rehydrate_members(guild)
    restored.check_win_conditions = AsyncMock(return_value=False)
    gm.active_games[123] = restored
    await controller.deliver_deferred_resolution(restored, 'night-one')
    assert restored.gameplay['deaths']['3']['delivered']
    assert not restored.gameplay['resolutions']['night-one']['public_delivery_pending']
    assert restored.gameplay['resolution']['night_token'] == 'night-two'


@pytest.mark.asyncio
async def test_committed_trial_cannot_be_cancelled_by_missing_channel(world):
    game, _, controller, channels, _ = world
    trial = await new_trial(world)
    trial.update(stage='closed', defendant=3, counted=True, applied=True, result={
        'guilty':True, 'choices':{'1':'guilty'}, 'weights':{'1':1},
        'totals':{'guilty':1,'innocent':0,'abstain':0}, 'haunt_ids':[1]})
    game.votes_today = 1
    channels.pop(10)
    assert not await controller.render_trial(game)
    assert trial['stage'] == 'closed' and game.votes_today == 1 and not trial['refunded']


@pytest.mark.asyncio
async def test_archived_trial_result_is_deliverable_after_next_trial_and_restart(world):
    game, guild, controller, _, _ = world
    old = await new_trial(world)
    old.update(stage='done', progressed=True, reason='Nominations tied.', delivery_pending=True, permissions_cleaned=True)
    game.vote_in_progress = False
    await trials.finish(game, old['id'])
    from config import GAME_OVERSEER_ROLE_ID
    current = await trials.start(game, role_ids=[GAME_OVERSEER_ROLE_ID], channel_id=10)
    saved = Game.from_persisted(game.to_persisted())
    await saved.rehydrate_members(guild)
    gm.active_games[123] = saved
    await controller.deliver_deferred_trial(saved, old['id'])
    assert not saved.gameplay['trials'][old['id']]['delivery_pending']
    assert saved.gameplay['trial']['id'] == current['id'] and saved.vote_in_progress


@pytest.mark.asyncio
async def test_sdk_client_can_login_again_after_full_reconnect(module, world, monkeypatch):
    client = commands.Bot(command_prefix='!', intents=discord.Intents.none())
    client.gameplay_controller = Controller(client)
    client.http.request = AsyncMock(return_value={'id':'999', 'username':'test', 'discriminator':'0', 'avatar':None})
    client.application_info = AsyncMock(return_value=S(id=999, interactions_endpoint_url=None, flags=discord.ApplicationFlags()))
    monkeypatch.setattr(module, 'bot', client)
    try:
        await client.login('offline-test-token')
        old_connector = client.http.connector
        await module._reopen_client()
        await client.login('offline-test-token')
        assert client.loop is asyncio.get_running_loop()
        assert client.http.connector is not old_connector and not client.http.connector.closed
        assert client.user.id == 999
    finally:
        await client.close()


def test_helper_imports_never_import_or_rebind_bot_entrypoint(world, monkeypatch):
    sentinel = gm.try_get_bot()
    monkeypatch.delitem(sys.modules, 'bot', raising=False)
    for name in ('bot_app.instance', 'bot_app.shared', 'bot_app.stats_board'):
        importlib.reload(importlib.import_module(name))
    assert 'bot' not in sys.modules
    assert gm.try_get_bot() is sentinel


@pytest.mark.asyncio
async def test_live_importstats_rejects_stale_totals_without_writes(module, world, tmp_path, monkeypatch):
    game, guild, _, _, _ = world
    db = Database(str(tmp_path / 'audit.db'))
    db.initialize()
    db.import_player_stats_from_json(guild_id=123, stats_data={'players':{'1':{'games_played':3, 'wins':2}}})
    monkeypatch.setattr(module.bot, 'db', db, raising=False)
    monkeypatch.setattr(module, 'load_stats', lambda gid: {'players':{'1':{'games_played':1,'wins':1}}})
    ctx = S(guild=guild, send=AsyncMock())
    await module.importstats.callback(ctx)
    assert db.get_player_stats_summary(guild_id=123, player_id=1)['games_played'] == 3
    assert 'force' in ctx.send.call_args.args[0]
    await module.importstats.callback(ctx, 'force')
    assert db.get_player_stats_summary(guild_id=123, player_id=1)['games_played'] == 1


@pytest.mark.parametrize('counter', ['games_played', 'losses', 'faction', 'role', 'personal'])
def test_import_guard_checks_individual_rows_not_only_global_maxima(tmp_path, counter):
    db = Database(str(tmp_path / 'audit.db'))
    db.initialize()
    data = {'players':{'1':{'games_played':100,'wins':50}, '2':{
        'games_played':10,'wins':4,'losses':6,'faction_wins':{'Town':4},
        'role_played':{'Doctor':10},'role_wins':{'Doctor':4},'personal_wins':{'pirate_win':2}}}}
    db.import_player_stats_from_json(guild_id=123, stats_data=data)
    stale = deepcopy(data)
    row = stale['players']['2']
    if counter in {'games_played', 'losses'}:
        row[counter] -= 1
    elif counter == 'faction':
        row['faction_wins']['Town'] -= 1
    elif counter == 'role':
        row['role_played']['Doctor'] -= 1
    else:
        row['personal_wins']['pirate_win'] -= 1
    tables = ('player_stats', 'player_personal_stats', 'player_role_stats')
    def snapshot():
        with sqlite3.connect(db.path) as conn:
            result = [conn.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() for table in tables]
        conn.close()
        return result
    before = snapshot()
    with pytest.raises(ValueError, match='newer counters'):
        db.import_player_stats_from_json(guild_id=123, stats_data=stale, reject_stale=True)
    assert snapshot() == before


@pytest.mark.asyncio
async def test_resolution_releases_its_flag_if_full_reconnect_interrupts_apply(world, monkeypatch):
    game, guild, _, _, _ = world
    entered, release = asyncio.Event(), asyncio.Event()
    async def calculate(*args):
        entered.set()
        await release.wait()
        return set(), [], []
    monkeypatch.setattr(resolution, 'evaluate', calculate)
    task = asyncio.create_task(resolution.run(game, S(guild=guild, send=AsyncMock())))
    await asyncio.wait_for(entered.wait(), 2)
    game._persist_player_ids = [p.id for p in game.players]
    game._persist_living_ids = [p.id for p in game.living_players]
    game._rehydrate_pending = True
    release.set()
    with pytest.raises(st.Rejected):
        await task
    assert not game.resolving and game._rehydrate_pending
    assert persistence.load_state(123)['living_ids'] == [1, 2, 3, 4]
    assert not game.state_lock.locked()


@pytest.mark.asyncio
async def test_completed_legacy_engine_does_not_reexecute_spent_corpse(world, monkeypatch):
    game, guild, _, _, _ = world
    game.player_roles[1] = 'Retributionist'
    game.role_states[1] = {'uses_remaining':0, 'used_corpses':[99]}
    game.graveyard = [{'player_id':99,'real_role':'Doctor','used_by_retri':True}]
    game.night_actions[1] = {'type':'heal','actor':1,'target':3,'_from_retri':99}
    game.night_completion_snapshot = {'day':1,'game_key':game.game_key,'night_engine_completed':True,
        'deaths':[3],'engine_deaths':[3],'retri_consumption_done':True,'psychic_visions_delivered':True}
    engine = AsyncMock(side_effect=AssertionError('Completed engine must not run again'))
    monkeypatch.setattr(resolution, 'run_night_pipeline', engine)
    monkeypatch.setattr(resolution, 'finish', AsyncMock())
    await resolution.run(game, S(guild=guild, send=AsyncMock()))
    engine.assert_not_awaited()
    assert game.role_states[1]['uses_remaining'] == 0
    assert game.gameplay['resolution']['death_ids'] == [3]


@pytest.mark.asyncio
async def test_completed_match_notice_still_delivers_before_next_match_starts(module, world, tmp_path, monkeypatch):
    game, _, _, _, _ = world
    game.in_progress, game.game_key = False, None
    db = Database(str(tmp_path / 'audit.db'))
    db.initialize()
    db.enqueue_dm_outbox(guild_id=123, kind='game_over', dedupe_key='mafia_game_over:123:old-match:1',
        target_user_id=1, content='valid completed-match summary', match_key='old-match')
    sent = asyncio.Event()
    async def send(text):
        sent.set()
    user = S(send=AsyncMock(side_effect=send))
    monkeypatch.setattr(module, 'bot', S(db=db, wait_until_ready=AsyncMock(), is_closed=lambda:False, get_user=lambda uid:user))
    pump = asyncio.create_task(module._dm_outbox_pump_loop())
    try:
        await asyncio.wait_for(sent.wait(), 2)
        user.send.assert_awaited_once_with('valid completed-match summary')
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


@pytest.mark.asyncio
async def test_status_is_bounded_and_delete_failure_does_not_claim_dm_failure(module, world):
    game, guild, _, _, _ = world
    players = [S(id=uid, display_name='x'*32, mention=f'<@{uid}>', roles=[]) for uid in range(1, 60)]
    guild.get_member = lambda uid: next((p for p in players if p.id == uid), None)
    game.players, game.living_players = players, players.copy()
    game.player_roles = {p.id:'Doctor' for p in players}
    game.player_slots = {p.id:p.id for p in players}
    ctx = S(guild=guild, author=S(id=999, send=AsyncMock()), send=AsyncMock(),
        message=S(delete=AsyncMock(side_effect=http_error(403))))
    await module.status.callback(ctx)
    embeds = [call.kwargs['embed'] for call in ctx.author.send.call_args_list]
    assert all(len(embed) <= 6000 and len(embed.fields) <= 25 for embed in embeds)
    assert all(len(field.value) <= 1024 for embed in embeds for field in embed.fields)
    assert sum(field.value.count('(Doctor)') for embed in embeds for field in embed.fields) == len(players)
    assert 'sent to your DMs' in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_dm_pump_does_not_block_loop_when_sqlite_is_locked(module, world, tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'audit.db'))
    db.initialize()
    entered, release = threading.Event(), threading.Event()
    def writer():
        with sqlite3.connect(db.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            entered.set()
            assert release.wait(3)
        conn.close()
    worker = threading.Thread(target=writer)
    worker.start()
    assert await asyncio.to_thread(entered.wait, 2)
    client = S(db=db, wait_until_ready=AsyncMock(), is_closed=lambda: False)
    monkeypatch.setattr(module, 'bot', client)
    pump = asyncio.create_task(module._dm_outbox_pump_loop())
    started = time.monotonic()
    try:
        await asyncio.sleep(.05)
        assert time.monotonic()-started < .35
    finally:
        release.set()
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        await asyncio.to_thread(worker.join, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['role_deal', 'game_over'])
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('cold_recovery', [False, True])
async def test_obsolete_outbox_message_cannot_cross_into_next_match(module, world, tmp_path, monkeypatch, kind, legacy, cold_recovery):
    game, _, _, _, _ = world
    game.game_key = 'new-match'
    if cold_recovery:
        persistence.save_state(123, game.to_persisted())
        gm.active_games.clear()
    db = Database(str(tmp_path / 'audit.db'))
    db.initialize()
    db.enqueue_dm_outbox(guild_id=123, kind=kind, dedupe_key=f'mafia_{kind}:123:old-match:1',
        target_user_id=1, content='obsolete private message', match_key=None if legacy else 'old-match')
    completed = threading.Event()
    original = db.mark_dm_outbox_superseded
    def superseded(mid):
        original(mid)
        completed.set()
    db.mark_dm_outbox_superseded = superseded
    user = S(send=AsyncMock())
    client = S(db=db, wait_until_ready=AsyncMock(), is_closed=lambda: False, get_user=lambda uid: user)
    monkeypatch.setattr(module, 'bot', client)
    pump = asyncio.create_task(module._dm_outbox_pump_loop())
    try:
        assert await asyncio.to_thread(completed.wait, 2)
        user.send.assert_not_awaited()
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    with sqlite3.connect(db.path) as conn:
        assert conn.execute('SELECT status FROM dm_outbox').fetchone()[0] == 'superseded'
    conn.close()


@pytest.mark.asyncio
async def test_dead_will_modal_and_clear_leave_state_and_receipt_unchanged(module, world, monkeypatch):
    game, _, _, _, players = world
    game.role_states[1]['will'] = 'before death'
    await st.commit(game, lambda: apply_death(game, 1, 'manual'))
    modal = module.WillModal(game=game, owner_id=1, current_text='before death')
    modal.will._value = 'edited after death'
    interaction = S(user=players[0], response=S(defer=AsyncMock(), is_done=lambda:True), followup=S(send=AsyncMock()))
    await modal.on_submit(interaction)
    monkeypatch.setattr(module.discord, 'DMChannel', Channel)
    ctx = S(author=players[0], channel=Channel(99), send=AsyncMock())
    await module.will.callback(ctx, text='clear')
    assert game.role_states[1]['will'] == game.gameplay['deaths']['1']['will'] == 'before death'
    assert 'Only living players' in interaction.followup.send.call_args.kwargs['content']
    assert 'Only living players' in players[0].send.call_args.args[0]


@pytest.mark.asyncio
async def test_documented_commands_have_stable_seats_and_doused_output(module, world, monkeypatch):
    game, guild, _, _, players = world
    game.player_slots = {1:4, 2:3, 3:2, 4:1}
    ctx = S(guild=guild, send=AsyncMock(), author=players[0])
    await module.show_players_command.callback(ctx)
    assert '#4: <@1>' in ctx.send.call_args.args[0]
    assert module.bot.get_command('doused') is module.doused
    game.player_roles[1] = 'Arsonist'
    game.doused_players = {2}
    monkeypatch.setattr(module.discord, 'DMChannel', Channel)
    ctx.channel = Channel(99)
    ctx.guild = None
    monkeypatch.setattr(module.bot, 'get_guild', lambda gid: guild)
    await module.doused.callback(ctx)
    assert '#3' in ctx.send.call_args.args[0] and 'Player 2' in ctx.send.call_args.args[0]


def test_database_connections_are_closed_on_success_and_failure(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'audit.db'))
    connections = []
    original = db._conn
    def track():
        conn = original()
        connections.append(conn)
        return conn
    monkeypatch.setattr(db, '_conn', track)
    db.initialize()
    assert not db.has_game_key('none')
    with pytest.raises(sqlite3.OperationalError):
        with db._transaction() as conn:
            conn.execute('SELECT * FROM nonexistent_table')
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            conn.execute('SELECT 1')


@pytest.mark.asyncio
@pytest.mark.parametrize('expanded', [False, True])
@pytest.mark.parametrize('invalid', ['spent', 'hidden', 'exhausted'])
async def test_recovered_retributionist_cannot_execute_unavailable_corpse(world, expanded, invalid, monkeypatch):
    game, guild, _, _, _ = world
    game.player_roles = {1:'Retributionist', 2:'Mobster', 3:'Doctor', 4:'Sheriff'}
    game.role_states = {1:{'uses_remaining':0 if invalid == 'exhausted' else 1, 'used_corpses':[]}}
    game.graveyard = [{'player_id':99,'real_role':'Doctor','used_by_retri':invalid == 'spent','is_hidden':invalid == 'hidden'}]
    action = ({'type':'heal','_from_retri':99,'actor':1,'target':3} if expanded else
        {'type':'reanimate','corpse_player_id':99,'corpse_role':'Doctor','actor':1,'target':3})
    game.night_actions = {1:action, 2:{'type':'kill','actor':2,'target':3}}
    restored = Game.from_persisted(game.to_persisted())
    await restored.rehydrate_members(guild)
    gm.active_games[123] = restored
    monkeypatch.setattr(resolution, 'finish', AsyncMock())
    await resolution.run(restored, S(guild=guild, send=AsyncMock()))
    assert 3 not in {p.id for p in restored.living_players}
    assert restored.role_states[1]['uses_remaining'] == (0 if invalid == 'exhausted' else 1)
    assert 1 not in restored.night_actions


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows file-sharing regression')
def test_temporary_windows_read_lock_preserves_valid_snapshot(world):
    from ctypes import wintypes
    game, _, _, _, _ = world
    persistence.save_state(123, game.to_persisted())
    path = persistence.STATE_DIR / '123.json'
    original = path.read_bytes()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x40000000, 0x2|0x4, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        gm.active_games.clear()
        with pytest.raises(persistence.StateReadError):
            gm.get_game_for_guild(123, allowed_guild_id=123)
        assert 123 not in gm.active_games
        assert path.exists() and not path.with_name('123.json.corrupt').exists()
    finally:
        kernel.CloseHandle(handle)
    assert path.read_bytes() == original
    assert gm.get_game_for_guild(123, allowed_guild_id=123).game_key == 'test-match'


@pytest.mark.parametrize('bad_token', [[], {}, None])
def test_invalid_archived_record_cannot_crash_loader(bad_token):
    assert not load_ui({'version':1, 'resolution':{'night_token':bad_token}}).get('resolution')
