"""Corrupt corpse actions must neither execute nor spend Retributionist resources."""
from copy import deepcopy

import pytest

from conftest import FakeGuild, FakeMember, mk_game
from engine.night import run_night_pipeline
from reanimate_expand import RETRI_CORPSE_EXPANDABLE_ROLES, expand_reanimate_actions
from retributionist_consumption import consume_retributionist_uses


ACTION_TYPES = {
    'Doctor': 'heal', 'Sheriff': 'investigate', 'Investigator': 'investigate',
    'Lookout': 'watch', 'Tracker': 'track', 'Escort': 'roleblock',
    'Bodyguard': 'ret_protect', 'Vigilante': 'shoot', 'Transporter': 'transport',
}


def corpse_game(role, form='submitted', *, strings=False):
    members = [FakeMember(uid) for uid in range(1, 5)]
    game = mk_game(members, {1: 'Retributionist', 2: 'Townie', 3: 'Mobster', 4: 'Doctor'},
                   {1: {'uses_remaining': 2, 'used_corpses': []}})
    game.graveyard = [{'player_id': 99, 'real_role': role}]
    actor, corpse, target = ('1', '99', '2') if strings else (1, 99, 2)
    action = {'type': 'reanimate', 'actor': actor, 'corpse_role': role,
              'corpse_player_id': corpse, 'target': target}
    if role == 'Transporter':
        action['targets'] = [target, '3' if strings else 3]
    if form == 'expanded':
        action = {'type': ACTION_TYPES[role], 'actor': actor, '_from_retri': corpse,
                  **({'targets': action['targets']} if role == 'Transporter' else {'target': target})}
        if role in {'Sheriff', 'Investigator'}:
            action['role'] = role
    game.night_actions[1] = action
    return game, FakeGuild(members)


@pytest.mark.asyncio
@pytest.mark.parametrize('role', sorted(RETRI_CORPSE_EXPANDABLE_ROLES))
@pytest.mark.parametrize('form', ['submitted', 'expanded'])
@pytest.mark.parametrize('bad_target', [None, [], 999])
async def test_malformed_corpse_payload_is_inert_and_not_consumed(role, form, bad_target):
    game, guild = corpse_game(role, form)
    if role == 'Transporter':
        game.night_actions[1]['targets'] = [2, bad_target]
    else:
        game.night_actions[1]['target'] = bad_target
    before = deepcopy(game.role_states[1])
    assert expand_reanimate_actions(game) == [1]
    assert 1 not in game.night_actions
    visits, blocked, healed, protected, deaths = await run_night_pipeline(game, guild)
    consume_retributionist_uses(game, blocked, healed)
    assert not visits and not healed and not protected and not deaths
    assert game.role_states[1] == before
    assert not game.graveyard[0].get('used_by_retri')


@pytest.mark.parametrize('bad_target', [True, False, 2.9, {}, 'not-a-player', 0, -1])
def test_consumption_rejects_invalid_ids_without_lossy_coercion(bad_target):
    game, _ = corpse_game('Doctor', 'expanded')
    game.night_actions[1]['target'] = bad_target
    consume_retributionist_uses(game, [], {})
    assert game.role_states[1] == {'uses_remaining': 2, 'used_corpses': []}
    assert not game.graveyard[0].get('used_by_retri')


@pytest.mark.parametrize('targets', [None, [], [2], [2, 3, 4], [2, 2], ['2', 2],
                                     [2, {}], [True, 3], [2.9, 3]])
def test_invalid_transport_payload_does_not_spend(targets):
    game, _ = corpse_game('Transporter')
    game.night_actions[1]['targets'] = targets
    consume_retributionist_uses(game, [], {})
    assert game.role_states[1] == {'uses_remaining': 2, 'used_corpses': []}
    assert not game.graveyard[0].get('used_by_retri')


@pytest.mark.parametrize('role', sorted(RETRI_CORPSE_EXPANDABLE_ROLES))
@pytest.mark.parametrize('form', ['submitted', 'expanded'])
def test_string_ids_are_normalized_and_valid_blocked_attempt_spends_once(role, form):
    game, _ = corpse_game(role, form, strings=True)
    assert expand_reanimate_actions(game) == []
    action = game.night_actions[1]
    assert action['actor'] == 1 and action['_from_retri'] == 99
    assert action.get('targets', [action.get('target')]) == ([2, 3] if role == 'Transporter' else [2])
    consume_retributionist_uses(game, [1], {})
    consume_retributionist_uses(game, [1], {})
    assert game.role_states[1] == {'uses_remaining': 1, 'used_corpses': [99]}
    assert game.graveyard[0]['used_by_retri'] is True


@pytest.mark.parametrize('corruption', ['action_type', 'investigation_role', 'actor', 'corpse'])
def test_expanded_action_must_match_its_actor_and_corpse(corruption):
    game, _ = corpse_game('Sheriff', 'expanded')
    action = game.night_actions[1]
    if corruption == 'action_type':
        action['type'] = 'shoot'
    elif corruption == 'investigation_role':
        action['role'] = 'Mole'
    elif corruption == 'actor':
        action['actor'] = 2
    else:
        action['_from_retri'] = 88
    consume_retributionist_uses(game, [], {})
    assert game.role_states[1] == {'uses_remaining': 2, 'used_corpses': []}
    assert not game.graveyard[0].get('used_by_retri')


@pytest.mark.parametrize('form', ['submitted', 'expanded'])
def test_graveyard_row_for_a_living_player_cannot_be_reanimated(form):
    game, _ = corpse_game('Doctor', form)
    game.living_players.append(FakeMember(99))
    consume_retributionist_uses(game, [], {})
    assert game.role_states[1] == {'uses_remaining': 2, 'used_corpses': []}
    assert not game.graveyard[0].get('used_by_retri')


@pytest.mark.asyncio
async def test_redundant_valid_doctor_attempt_still_spends():
    game, guild = corpse_game('Doctor', strings=True)
    # The engine processes actors by stable slot: the living Doctor goes first.
    action = game.night_actions[1]
    action['actor'] = '4'
    game.player_roles.update({1: 'Doctor', 4: 'Retributionist'})
    game.role_states[4] = game.role_states.pop(1)
    game.night_actions = {1: {'type': 'heal', 'actor': 1, 'target': 2}, 4: action}
    _, blocked, healed, _, _ = await run_night_pipeline(game, guild)
    assert healed == {2: 1}
    consume_retributionist_uses(game, blocked, healed)
    assert game.role_states[4]['uses_remaining'] == 1
    assert game.graveyard[0]['used_by_retri']
