"""Behavioral regressions for the reconstructed recovery integration points."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from game import Game
from night_action_guards import actor_has_guilt_pending, night_actions_frozen
from persist_validation import normalize_night_completion_snapshot_for_game, normalize_night_transport_swaps


@pytest.mark.parametrize('snapshot', [
    {'pre_pipeline': True}, {'night_engine_running': True},
    {'night_engine_completed': True}, {'post_pipeline_pending': True},
])
def test_matching_interrupted_night_blocks_new_actions(snapshot):
    game = Game(99)
    game.phase = 'night'
    game.day_number = 2
    game.game_key = 'match'
    game.night_completion_snapshot = {'day': 2, 'game_key': 'match', **snapshot}
    assert night_actions_frozen(game)
    game.night_completion_snapshot['game_key'] = 'previous-match'
    assert not night_actions_frozen(game)
    game.night_completion_snapshot['game_key'] = 'match'
    game.night_completion_snapshot['day'] = 1
    assert not night_actions_frozen(game)


def test_lock_protects_actions_even_before_resolving_flag_is_set():
    game = Game(99)
    game._night_resolve_guard = SimpleNamespace(locked=lambda: True)
    assert night_actions_frozen(game)
    game._night_resolve_guard = SimpleNamespace(locked=lambda: False)
    assert not night_actions_frozen(game)


@pytest.mark.parametrize('state, expected', [
    ({'guilty_tomorrow': True}, True), ({'will_die_of_guilt': 'true'}, True),
    ({'guilty_tomorrow': 'false', 'will_die_of_guilt': False}, False),
    ({}, False), ('corrupt', False),
])
def test_deferred_guilt_prevents_an_extra_ability(state, expected):
    game = Game(99)
    game.role_states = {1: state}
    assert actor_has_guilt_pending(game, 1) is expected


def test_transport_recovery_rejects_bad_ids_but_keeps_order_and_repeated_swaps():
    raw = [['1', '2', '3'], [1, 2, 3], [2, 1, 4], [1, 1, 3], [True, 2, 3], [1.5, 2, 3], [0, 2, 3], [1, 2], 'bad']
    assert normalize_night_transport_swaps(raw) == [(1, 2, 3), (1, 2, 3), (2, 1, 4)]


def test_corrupt_snapshot_fields_do_not_become_completed_phases_or_player_ids():
    raw = {'day': '2', 'night_engine_completed': 'false', 'misc_phase_complete': 'invalid',
           'deaths': ['1', True, 2.5, 'oops', -3, '1'],
           'healed_by': [['4', '2'], ['bad', 2]],
           'protected_by': {'4': [{'id': '1', 'dies_on_guard': 'false'}], 'bad': []}}
    snap = normalize_night_completion_snapshot_for_game(raw)
    assert snap['day'] == 2
    assert snap['night_engine_completed'] is False
    assert snap['misc_phase_complete'] is False
    assert snap['deaths'] == [1]
    assert snap['healed_by'] == [[4, 2]]
    assert snap['protected_by'] == {4: [{'id': 1, 'dies_on_guard': False}]}
    assert raw['deaths'][0] == '1'


def test_json_roundtrip_restores_guard_protection_and_does_not_spend_it_twice():
    from engine.night import apply_misc_actions, resolve_killing
    from night_engine_checkpoint import restore_night_engine_phase_checkpoint
    from scripts.monte_carlo.bridge import _FakeGuild, _FakeMember

    game = Game(99)
    game.in_progress = True
    game.phase = 'night'
    game.day_number = 2
    game.game_key = 'match'
    members = [_FakeMember(pid) for pid in (1, 2, 3, 4)]
    game.players = members
    game.living_players = members.copy()
    game.player_roles = {1: 'Bodyguard', 2: 'Doctor', 3: 'Mobster', 4: 'Sheriff'}
    game.role_states = {1: {'protects_remaining': 0}, 2: {}, 3: {}, 4: {}}
    game.night_actions = {3: {'type': 'kill', 'actor': 3, 'target': 4}}
    game.night_completion_snapshot = {'day': 2, 'game_key': 'match', 'misc_phase_complete': True,
        'healed_by': [], 'protected_by': {4: [{'id': 1, 'dies_on_guard': True}]}}
    loaded = Game.from_persisted(json.loads(json.dumps(game.to_persisted())))
    loaded.players = members
    loaded.living_players = members.copy()
    guild = _FakeGuild(members)
    restore_night_engine_phase_checkpoint(loaded)

    async def resolve():
        healed, protected = await apply_misc_actions(loaded, [], guild)
        deaths = await resolve_killing(loaded, {4: [3]}, [], healed, protected, guild)
        return protected, deaths

    protected, deaths = asyncio.run(resolve())
    assert protected[4][0]['id'] == 1
    assert deaths == {1, 3}
    assert loaded.role_states[1]['protects_remaining'] == 0


@pytest.fixture
def stats_db(tmp_path, monkeypatch):
    import persistence
    from database import Database
    monkeypatch.setattr(persistence, 'STATE_DIR', tmp_path / 'state')
    db = Database(str(tmp_path / 'stats.db'))
    db.initialize()
    return db


def test_mirror_repair_is_idempotent_and_preserves_pending_endgame_metadata(stats_db):
    from persistence import load_stats, save_stats
    from stats_mirror_repair import repair_guild_json_mirror_from_sqlite
    stats_db.upsert_player_stats_delta(guild_id=99, player_id=1, games_played=1,
        wins_total=1, losses_total=0, draws_total=0, wins_town=1, wins_mafia=0, wins_arsonist=0)
    save_stats(99, {'players': {'1': {'wins': 999}}, '_meta': {'pending_endgame': {'game_key': 'pending'}}})
    for _ in range(2):
        assert repair_guild_json_mirror_from_sqlite(stats_db, guild_id=99, game_key='finished')
    mirror = load_stats(99)
    assert mirror['players']['1']['wins'] == 1
    assert mirror['_meta']['pending_endgame']['game_key'] == 'pending'
    assert mirror['_meta']['last_json_game_key'] == 'finished'
    assert stats_db.get_player_stats_summary(guild_id=99, player_id=1)['games_played'] == 1


def test_failed_sqlite_read_preserves_previous_mirror(stats_db, monkeypatch):
    from persistence import load_stats, save_stats
    from stats_mirror_repair import repair_guild_json_mirror_from_sqlite
    original = {'players': {'1': {'wins': 2}}, '_meta': {'pending_endgame': {'game_key': 'pending'}}}
    save_stats(99, original)
    def fail(**kwargs):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(stats_db, 'build_json_players_mirror', fail)
    assert not repair_guild_json_mirror_from_sqlite(stats_db, guild_id=99)
    assert load_stats(99) == original


def test_mirror_repair_does_not_include_another_guild(stats_db):
    from persistence import load_stats
    from stats_mirror_repair import repair_guild_json_mirror_from_sqlite
    for gid in (99, 100):
        stats_db.upsert_player_stats_delta(guild_id=gid, player_id=gid, games_played=1,
            wins_total=1, losses_total=0, draws_total=0, wins_town=1, wins_mafia=0, wins_arsonist=0)
    assert repair_guild_json_mirror_from_sqlite(stats_db, guild_id=99)
    assert set(load_stats(99)['players']) == {'99'}


def test_entry_point_checks_credentials_and_releases_lock_on_failure(monkeypatch):
    import bot
    import config
    import bot as bootstrap
    calls = []
    monkeypatch.delenv('DISCORD_TOKEN', raising=False)
    monkeypatch.delenv('DISCORD_BOT_TOKEN', raising=False)
    monkeypatch.setattr(bootstrap, '_acquire_single_instance_lock', lambda: calls.append('acquire'))
    monkeypatch.setattr(bootstrap, '_release_single_instance_lock', lambda: calls.append('release'))
    with pytest.raises(RuntimeError, match='token|TOKEN'):
        bot.main()
    assert calls == []
    monkeypatch.setenv('DISCORD_TOKEN', 'offline-test-only')
    monkeypatch.setattr(config, 'load_allowed_guild_id', lambda: 99)
    monkeypatch.setattr(config, 'PLAYING_ROLE_ID', 1)
    monkeypatch.setattr(config, 'GAME_OVERSEER_ROLE_ID', 2)
    async def fail_session():
        raise RuntimeError('session failed')
    monkeypatch.setattr(bot, '_run_session', fail_session)
    with pytest.raises(RuntimeError, match='session failed'):
        bot.main()
    assert calls == ['acquire', 'release']


def test_entry_point_rejects_invalid_server_config_before_locking(monkeypatch):
    import bot
    import config
    import bot as bootstrap
    monkeypatch.setenv('DISCORD_BOT_TOKEN', 'offline-test-only')
    monkeypatch.setattr(config, 'load_allowed_guild_id', lambda: 0)
    monkeypatch.setattr(bootstrap, '_acquire_single_instance_lock', lambda: pytest.fail('must validate first'))
    with pytest.raises(RuntimeError, match='ALLOWED_GUILD_ID'):
        bot.main()


def test_graceful_release_allows_restart_in_the_same_process(tmp_path, monkeypatch):
    import bot as bootstrap
    monkeypatch.setenv('MAFIABOT_INSTANCE_LOCK_PATH', str(tmp_path / 'bot.instance.lock'))
    monkeypatch.delenv('MAFIABOT_ALLOW_MULTI', raising=False)
    try:
        bootstrap._acquire_single_instance_lock()
        from instance_lock import acquire_instance_lock
        with pytest.raises(RuntimeError):
            acquire_instance_lock(tmp_path / 'bot.instance.lock')
    finally:
        bootstrap._release_single_instance_lock()
    assert bootstrap._single_instance_lock_handle is None
    bootstrap._acquire_single_instance_lock()
    bootstrap._release_single_instance_lock()
    assert bootstrap._single_instance_lock_handle is None
