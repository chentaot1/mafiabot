"""Role controls exercise real submission, persistence, recovery, and private routing."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

import game as gm
import persistence
from game import Game
from engine.night import deliver_psychic_visions, run_night_pipeline
from gameplay import actions, reports, resolution, state as st
from gameplay.deputy import fire
from gameplay.views import DayPanel, DeputyDraft, DeputyConfirmation, Draft, NightPanel, ReportHistory, ReportCard
from test_gameplay import world
from test_combined_roster import model


def text_of(view):
    return '\n'.join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def button(view, label):
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def interaction(uid):
    return SimpleNamespace(user=SimpleNamespace(id=uid),
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())


def deputy_day(world):
    game, guild, controller, channels, players = world
    game.phase, game.day_number = 'day', 2
    game.player_roles.update({1: 'Deputy', 2: 'Mobster'})
    game.role_states[1] = {'deputy_shots_remaining': 1}
    return game, guild, controller, channels, players


@pytest.mark.asyncio
async def test_deputy_draft_confirmation_and_duplicate_fire(world):
    game, _, controller, _, _ = deputy_day(world)
    draft = DeputyDraft(controller, game, 1)
    assert button(draft, 'Review shot').disabled
    draft.target = 2
    draft.rebuild()
    await button(draft, 'Review shot').callback(interaction(1))
    assert game.role_states[1]['deputy_shots_remaining'] == 1 and not game.graveyard
    confirmation = DeputyConfirmation(controller, game, 1, 2)
    fire_button = button(confirmation, 'Fire')
    i = interaction(1)
    assert await confirmation.interaction_check(i)
    await fire_button.callback(i)
    assert game.role_states[1]['deputy_shots_remaining'] == 0
    assert persistence.load_state(123)['gameplay']['deaths']['2']
    assert 'Shot committed' in text_of(confirmation)
    assert not any(isinstance(item, discord.ui.Button) for item in confirmation.walk_children())
    await fire_button.callback(interaction(1))
    await asyncio.gather(*list(controller.jobs.values()))
    assert len(game.graveyard) == 1
    assert game.gameplay['deaths']['2']['delivered']
    assert game.check_win_conditions.await_count == 1


@pytest.mark.asyncio
async def test_deputy_cancel_does_not_fire(world):
    game, _, controller, _, _ = deputy_day(world)
    confirmation = DeputyConfirmation(controller, game, 1, 2)
    fire_button = button(confirmation, 'Fire')
    await button(confirmation, 'Cancel').callback(interaction(1))
    await fire_button.callback(interaction(1))  # A queued click on the cancelled form is rejected.
    assert game.role_states[1]['deputy_shots_remaining'] == 1 and not game.graveyard
    assert not any(isinstance(item, discord.ui.Button) for item in confirmation.walk_children())


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['day', 'phase', 'match', 'target_dead', 'role', 'tribunal'])
async def test_deputy_confirmation_revalidates_before_commit(world, changed):
    game, guild, _, _, players = deputy_day(world)
    expected = st.identity(game)
    if changed == 'day': game.day_number += 1
    elif changed == 'phase': game.phase = 'night'
    elif changed == 'match': game.game_key = 'new-match'
    elif changed == 'target_dead': game.living_players.remove(players[1])
    elif changed == 'role': game.player_roles[1] = 'Doctor'
    else: game.vote_in_progress = True
    game.sync_living_players = AsyncMock()
    with pytest.raises(st.Rejected):
        await fire(game, 1, 2, guild=guild, expected=expected)
    assert game.role_states[1]['deputy_shots_remaining'] == 1 and not game.graveyard
    assert not game.state_lock.locked()


@pytest.mark.asyncio
async def test_deputy_failed_write_does_not_confirm_or_disable(world):
    game, _, controller, _, _ = deputy_day(world)
    game.persist_flush = AsyncMock(side_effect=OSError('disk unavailable'))
    confirmation = DeputyConfirmation(controller, game, 1, 2)
    i = interaction(1)
    await button(confirmation, 'Fire').callback(i)
    assert game.role_states[1]['deputy_shots_remaining'] == 1 and not game.graveyard
    assert not button(confirmation, 'Fire').disabled
    assert 'could not be saved' in i.followup.send.call_args.kwargs['content']
    assert not controller.jobs and not game.state_lock.locked()


@pytest.mark.asyncio
async def test_day_panels_restore_without_duplicate_delivery_and_reject_previous_phase(world):
    game, guild, controller, channels, _ = deputy_day(world)
    await controller.send_day_panels(game)
    reference = deepcopy(game.gameplay['panels']['1'])
    assert isinstance(channels[101].messages[reference['message_id']].view, DayPanel)
    old = channels[101].messages[reference['message_id']].view
    recovered = Game.from_persisted(persistence.load_state(123))
    await recovered.rehydrate_members(guild)
    gm.active_games[123] = recovered
    recovered.check_win_conditions = AsyncMock(return_value=False)
    await controller.recover(recovered)
    await controller.recover(recovered)
    assert len(channels[101].messages) == 1
    assert recovered.gameplay['panels']['1'] == reference
    assert not await old.interaction_check(interaction(1))
    current = controller.panel_for(recovered, 1)
    recovered.phase = 'night'
    recovered.gameplay['night_token'] = 'next-night'
    assert not await current.interaction_check(interaction(1))
    await controller.stop_game(recovered)


@pytest.mark.asyncio
async def test_deputy_day_private_channel_safety_and_blocked_dms(world, monkeypatch):
    import gameplay.controller as controllers
    game, _, controller, channels, players = deputy_day(world)
    monkeypatch.setattr(controllers, 'configured_channel', lambda uid: 10)
    channels[10].readers = {0, 1, 2, 3, 4}
    await controller.reopen(game, 1)
    assert not channels[10].sent and channels[101].sent
    players[0].create_dm = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason='blocked'), 'blocked'))
    with pytest.raises(discord.Forbidden): await controller.reopen(game, 1)
    # Ephemeral /actions needs no DM or configured channel.
    assert isinstance(controller.panel_for(game, 1), DayPanel)
    assert not channels[10].sent


@pytest.mark.asyncio
async def test_serial_killer_explicit_mode_is_idempotent_and_preserves_stab(world):
    game, _, controller, _, _ = world
    game.player_roles[1] = 'Serial Killer'
    assert (await actions.submit(game, 1, 'sk_kill', (3,), cooldown=False)).accepted
    before = deepcopy(game.night_actions[1])
    for mode in (True, True, False, False):
        result = await actions.submit(game, 1, 'cautious', cautious_mode=mode)
        assert result.accepted and game.role_states[1]['sk_cautious'] is mode
        assert game.night_actions[1] == before
        assert persistence.load_state(123)['role_states']['1']['sk_cautious'] is mode
    panel = NightPanel(controller, game, 1)
    assert '**Mode:** Aggressive' in text_of(panel) and 'Stab' in text_of(panel)
    i = interaction(1)
    await button(panel, 'Cautious').callback(i)
    assert game.role_states[1]['sk_cautious'] and game.night_actions[1] == before
    assert i.edit_original_response.call_args.kwargs['view'].timeout is None


@pytest.mark.asyncio
async def test_mode_write_failure_restores_mode_and_stab(world):
    game, _, _, _, _ = world
    game.player_roles[1] = 'Serial Killer'
    await actions.submit(game, 1, 'sk_kill', (3,), cooldown=False)
    before = deepcopy(game.night_actions)
    game.persist_flush = AsyncMock(side_effect=OSError('disk unavailable'))
    result = await actions.submit(game, 1, 'cautious', cautious_mode=True)
    assert not result.accepted and not game.role_states[1].get('sk_cautious')
    assert game.night_actions == before


@pytest.mark.asyncio
async def test_guardian_ward_is_preselected_and_requires_submit_even_after_death(world):
    game, _, controller, _, players = world
    game.player_roles[1] = 'Guardian Angel'
    game.role_states[1] = {'ga_target_id': 3, 'ga_ward_charges': 1}
    game.living_players.remove(players[0])
    panel = controller.panel_for(game, 1)
    assert 'Bound player' in text_of(panel) and 'can still ward' in text_of(panel)
    draft = Draft(controller, game, 1, 'ward')
    assert draft.targets == {0: 3} and not game.night_actions
    assert not any(isinstance(item, discord.ui.Select) for item in draft.walk_children())
    await button(draft, 'Submit').callback(interaction(1))
    assert game.night_actions[1] == {'type': 'ward', 'actor': 1, 'target': 3}
    assert game.role_states[1]['ga_ward_charges'] == 1


def test_seer_picker_filters_self_revealed_mayor_and_used_pair(world):
    game, _, controller, _, _ = world
    game.player_roles[1] = 'Seer'
    game.role_states[1] = {'seer_pair_history': [[3, 4]]}
    draft = Draft(controller, game, 1, 'gaze')
    pickers = [item for item in draft.walk_children() if isinstance(item, discord.ui.Select)]
    assert all({option.value for option in picker.options} == {'3', '4'} for picker in pickers)
    assert button(draft, 'Submit').disabled
    draft.targets[0] = 3
    draft.rebuild()
    assert 'No eligible target 2 available' in text_of(draft)
    assert actions.seer_pair_used(game, 1, 4, 3)


@pytest.mark.asyncio
async def test_report_history_is_committed_before_private_cards_and_recovers_once(world):
    game, guild, controller, channels, _ = world
    game.player_roles.update({1: 'Seer', 2: 'Townie', 3: 'Mobster', 4: 'Psychic'})
    await actions.submit(game, 1, 'gaze', (2, 3), cooldown=False)
    with patch.object(resolution, 'finish', AsyncMock(side_effect=OSError('interrupted delivery'))):
        with pytest.raises(OSError):
            await resolution.run(game, SimpleNamespace(guild=guild, send=channels[10].send))
    assert not channels[101].sent and not channels[104].sent
    saved = persistence.load_state(123)
    assert len(reports.history(game, 1)) == len(reports.history(game, 4)) == 1, {
        'living_ids': [p.id for p in game.living_players],
        'role_states': game.role_states,
        'night_snapshot': game.night_completion_snapshot,
    }
    recovered = Game.from_persisted(saved)
    await recovered.rehydrate_members(guild)
    recovered.check_win_conditions = AsyncMock(return_value=False)
    gm.active_games[123] = recovered
    await resolution.finish(recovered, SimpleNamespace(guild=guild, send=channels[10].send))
    await resolution.finish(recovered, SimpleNamespace(guild=guild, send=channels[10].send))
    for uid in (1, 4):
        cards = [msg.view for msg in channels[100+uid].messages.values() if isinstance(msg.view, ReportCard)]
        assert len(cards) == 1 and len(reports.history(recovered, uid)) == 1
        assert 'Night 1' in text_of(cards[0])
        assert isinstance(controller.panel_for(recovered, uid), DayPanel)
    assert all(not isinstance(msg.view, ReportCard) for msg in channels[10].messages.values())


@pytest.mark.asyncio
async def test_report_application_failure_rolls_back_history_without_delivery(world):
    game, guild, _, channels, _ = world
    game.player_roles.update({1: 'Seer', 2: 'Townie', 3: 'Mobster', 4: 'Psychic'})
    await actions.submit(game, 1, 'gaze', (2, 3), cooldown=False)
    calls = 0
    original = game.persist_flush
    async def fail_application():
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError('application interrupted')
        await original()
    game.persist_flush = fail_application
    with pytest.raises(OSError):
        await resolution.run(game, SimpleNamespace(guild=guild, send=channels[10].send))
    assert not reports.history(game, 1) and not reports.history(game, 4)
    assert not channels[101].sent and not channels[104].sent
    assert not game.state_lock.locked()


@pytest.mark.asyncio
async def test_psychic_block_and_witch_feedback_history_match_actual_recipients(model):
    game, guild, members = model
    await deliver_psychic_visions(game, guild, [6])
    assert reports.history(game, 6)[0]['status'] == 'blocked'
    assert 'vision revealed' not in reports.history(game, 6)[0]['text'].lower()
    game.role_states[6].pop('private_reports')
    game.role_states[6]['psychic_vision_recipient_id'] = 3
    await deliver_psychic_visions(game, guild, [])
    for uid in (3, 6):
        entry = reports.history(game, uid)[0]
        assert entry['owner'] == uid and entry['text'] == members[uid-1].send.call_args.args[0]
    assert reports.history(game, 3)[0]['status'] == 'stolen'
    assert not reports.history(game, 2)


@pytest.mark.asyncio
async def test_seer_history_never_exposes_transport_destinations(model):
    game, guild, _ = model
    game.player_roles[5] = 'Transporter'
    await actions.submit(game, 1, 'gaze', (2, 4), cooldown=False)
    await actions.submit(game, 5, 'transport', (2, 3), cooldown=False)
    await run_night_pipeline(game, guild)
    entry = reports.history(game, 1)[0]
    assert entry['selected'] == [2, 4] and 'Enemies' in entry['text']
    assert 3 not in entry['selected']


@pytest.mark.asyncio
async def test_history_is_paged_owned_and_stale_controls_are_rejected(world):
    game, _, controller, _, _ = world
    game.player_roles[1] = 'Psychic'
    for night in range(1, 6):
        game.day_number = night
        reports.remember(game, 1, f'Private vision {night}', kind='psychic')
    view = ReportHistory(controller, game, 1)
    assert 'Private vision 5' in text_of(view) and 'Private vision 1' not in text_of(view)
    assert not button(view, 'Next').disabled
    assert not await view.interaction_check(interaction(2))
    game.gameplay['night_token'] = 'next-night'
    assert not await view.interaction_check(interaction(1))
    game.game_key = 'next-match'
    assert not reports.history(game, 1)


def test_corrupt_saved_reports_are_ignored_and_card_length_is_bounded(world):
    game, _, controller, _, _ = world
    game.player_roles[1] = 'Psychic'
    for night in range(1, 5):
        game.day_number = night
        reports.remember(game, 1, 'x'*1000, kind='psychic', selected=(2, 3))
    valid = deepcopy(game.role_states[1]['private_reports'][0])
    game.role_states[1]['private_reports'].extend([None, {'kind': {}}, {**valid, 'owner': 2},
        {**valid, 'kind': {}}, {**valid, 'text': 'x'*1001}, {**valid, 'selected': [[], 3]}])
    assert len(reports.history(game, 1)) == 4
    assert len(text_of(ReportHistory(controller, game, 1))) < 4000


@pytest.mark.asyncio
async def test_concurrent_panel_delivery_creates_one_message(world, monkeypatch):
    game, _, controller, channels, _ = deputy_day(world)
    original = channels[101].send
    async def delayed_send(*args, **kwargs):
        await asyncio.sleep(0)
        return await original(*args, **kwargs)
    monkeypatch.setattr(channels[101], 'send', delayed_send)
    await asyncio.gather(controller.send_day_panels(game), controller.send_day_panels(game))
    assert len(channels[101].messages) == 1
    assert len(game.gameplay['panels']) == 1


@pytest.mark.asyncio
async def test_dead_investigator_can_reopen_read_only_history_at_night(world):
    game, _, controller, channels, players = world
    game.player_roles[1] = 'Seer'
    reports.remember(game, 1, 'Friends', kind='seer', selected=(3, 4))
    game.living_players.remove(players[0])
    view = controller.panel_for(game, 1)
    assert isinstance(view, ReportHistory) and 'Friends' in text_of(view)
    assert not any(isinstance(item, discord.ui.Select) for item in view.walk_children())
    await controller.reopen(game, 1)
    await controller.send_night_panels(game)
    assert len(channels[101].messages) == 1


@pytest.mark.parametrize('role', ['Deputy', 'Seer', 'Psychic', 'Guardian Angel', 'Serial Killer'])
def test_five_role_panels_serialize_as_v2_components(world, role):
    game, _, controller, _, _ = world
    game.player_roles[1] = role
    game.role_states[1] = {'ga_target_id': 3, 'ga_ward_charges': 1, 'deputy_shots_remaining': 1}
    for phase in ('night', 'day'):
        game.phase = phase
        panel = controller.panel_for(game, 1)
        assert panel.to_components()[0]['type'] == 17  # Container, with component text inside.
        assert len(text_of(panel)) < 4000


@pytest.mark.asyncio
async def test_deputy_text_command_refreshes_the_same_private_day_panel(world, monkeypatch):
    import bot as module
    game, _, controller, channels, players = deputy_day(world)
    monkeypatch.setattr(module, 'bot', controller.bot)
    await controller.send_day_panels(game)
    ctx = SimpleNamespace(author=players[0], guild=None, interaction=None, send=AsyncMock())
    await module.shoot.callback(ctx, 2)
    await asyncio.gather(*list(controller.jobs.values()))
    assert game.role_states[1]['deputy_shots_remaining'] == 0
    assert ctx.send.call_args.kwargs['ephemeral'] is True
    message = next(iter(channels[101].messages.values()))
    assert '**Bullets remaining:** 0' in text_of(message.view)
    assert button(message.view, 'Prepare shot').disabled
    assert len(game.graveyard) == 1 and game.gameplay['deaths']['2']['delivered']
