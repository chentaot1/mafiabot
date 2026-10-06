import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import game as game_module
from game import Game


class Message:
    def __init__(self, message_id=10, reactions=None):
        self.id = message_id
        self.reactions = reactions or []
        self.add_reaction = AsyncMock()


class Channel:
    def __init__(self, guild):
        self.guild = guild
        self.message = Message()
        self.sent = []
        self.set_permissions = AsyncMock()

    async def send(self, content=None, **kwargs):
        self.sent.append(content or kwargs.get("embed"))
        return self.message

    async def fetch_message(self, message_id):
        return self.message


@pytest.fixture
def bot_module(monkeypatch):
    # Import constructs command objects but never connects to Discord. Keep
    # credentials fake and isolate the module's shared game/bot state.
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    old_bound_bot = game_module._BOT
    import bot as module

    games = {}
    monkeypatch.setattr(game_module, "active_games", games)
    monkeypatch.setattr(module, "active_games", games)
    monkeypatch.setattr(module, "ALLOWED_GUILD_ID", 123)
    monkeypatch.setattr(module, "_dbg", lambda *args, **kwargs: None)
    yield module
    game_module._BOT = old_bound_bot


def member(uid):
    return SimpleNamespace(id=uid, display_name=f"Player {uid}", mention=f"<@{uid}>",
                           roles=[], remove_roles=AsyncMock(), send=AsyncMock(return_value=Message(uid)))


def setup_game(module, monkeypatch, phase):
    players = [member(1), member(2)]
    channels = {}
    guild = SimpleNamespace(id=123, get_role=lambda rid: rid,
                            get_channel=lambda cid: channels.get(cid))
    channel = Channel(guild)
    channels[10] = channel
    game = Game(123)
    game.in_progress = True
    game.phase = phase
    game.day_number = 3
    game.players = players
    game.living_players = players[:]
    game.player_slots = {1: 1, 2: 2}
    game.player_roles = {1: "Pirate", 2: "Doctor"}
    game.game_channel_id = game.day_vc_id = 10
    game.alive_role_id = 20
    game.stand_role_id = 21
    game.sync_living_players = AsyncMock()
    game.get_living_ids = AsyncMock(return_value=[1, 2])
    game.get_member_safe = AsyncMock(side_effect=lambda guild, uid: next((p for p in players if p.id == uid), None))
    game.persist_flush = AsyncMock()
    module.active_games[123] = game
    fake_bot = SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda cid: channels.get(cid))
    monkeypatch.setattr(module, "bot", fake_bot)
    monkeypatch.setattr(module.discord, "TextChannel", Channel)
    return game, guild, channel, players, fake_bot


@pytest.mark.parametrize("preexisting", [False, True])
def test_ready_preserves_live_game_and_resolution_guard(bot_module, monkeypatch, preexisting):
    module = bot_module
    guild = SimpleNamespace(id=123, name="test", get_role=lambda rid: None)
    fake_bot = SimpleNamespace(
        user=SimpleNamespace(id=999), guilds=[guild], commands=[], intents=SimpleNamespace(),
        tree=SimpleNamespace(sync=AsyncMock(return_value=[]), get_commands=lambda **kw: []),
        db=object(), get_guild=lambda gid: guild, _mafia_dm_outbox_started=True,
    )
    monkeypatch.setattr(module, "bot", fake_bot)
    monkeypatch.setattr(module, "_ensure_gateway_watchdog_task", lambda: None)
    loads = []

    def load(guild_id):
        loads.append(guild_id)
        return {"guild_id": guild_id, "in_progress": False}

    monkeypatch.setattr(module, "load_state", load)
    if preexisting:
        module.active_games[123] = Game(123)

    async def run():
        await module.on_ready()
        live = module.active_games[123]
        live.resolving = True
        live.night_actions[1] = {"type": "heal", "target": 2}
        await module.on_ready()
        assert module.active_games[123] is live
        assert live.resolving is True
        assert live.night_actions[1]["type"] == "heal"

    asyncio.run(run())
    assert loads == ([] if preexisting else [123])


@pytest.mark.parametrize("outcome", ["innocent", "guilty", "missing_channel", "missing_defendant", "error", "cancel"])
def test_resumed_trial_always_releases_vote_and_permissions(bot_module, monkeypatch, outcome):
    module = bot_module
    game, guild, channel, players, fake_bot = setup_game(module, monkeypatch, "day")
    game.vote_in_progress = game.tribunal_muted = True
    game.tribunal_defendant_id = 1
    game.tribunal_subphase = "defense"
    game.tribunal_defense_deadline_utc = "2030-01-01T00:00:00+00:00"
    game.votes_today = 1
    game.process_death = AsyncMock()
    game.check_win_conditions = AsyncMock(return_value=False)

    async def start_night(ctx):
        game.phase = "night"

    game.start_night = AsyncMock(side_effect=start_night)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    if outcome == "guilty":
        async def users():
            yield players[1]
        channel.message.reactions = [SimpleNamespace(emoji="✅", users=users)]
    elif outcome == "missing_channel":
        fake_bot.get_channel = lambda cid: None
    elif outcome == "missing_defendant":
        players.pop(0)
    elif outcome == "error":
        channel.send = AsyncMock(side_effect=RuntimeError("send failed"))
    elif outcome == "cancel":
        monkeypatch.setattr(module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    async def run():
        if outcome == "error":
            with pytest.raises(RuntimeError, match="send failed"):
                await module._resume_tribunal_defense_after_restart(guild, 0)
        elif outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await module._resume_tribunal_defense_after_restart(guild, 1)
        else:
            await module._resume_tribunal_defense_after_restart(guild, 0)

    asyncio.run(run())
    assert game.vote_in_progress is False
    assert game.tribunal_defendant_id is None
    assert game.tribunal_muted is False
    assert game.tribunal_subphase is None
    assert game.tribunal_verdict_committed is False
    assert game.votes_today == 1
    assert game.tribunal_defense_deadline_utc is None
    assert game.tribunal_judgment_deadline_utc is None
    assert game.tribunal_judgment_message_id is None
    if outcome == "guilty":
        game.process_death.assert_awaited_once()
        # Finishing a guilty trial must not unmute the following night.
        assert game.phase == "night"
    else:
        channel.set_permissions.assert_awaited_with(20, speak=True)


@pytest.mark.parametrize("target_emoji,won", [("✂️", True), ("📄", False), ("🪨", False)])
def test_pirate_result_is_recorded_before_resolution_is_unlocked(bot_module, monkeypatch, target_emoji, won):
    from gameplay import actions, duels, resolution
    module = bot_module
    game, guild, channel, players, fake_bot = setup_game(module, monkeypatch, "night")
    snapshots = []
    async def persist():
        snapshots.append(deepcopy(game.night_actions))
    game.persist_flush = persist
    async def run():
        result = await actions.submit(game, 1, 'plunder', (2,))
        token = result.action['duel_token']
        await duels.choose(game, 1, token, 1, 'rock')
        await duels.choose(game, 1, token, 2, {'✂️':'scissors','📄':'paper','🪨':'rock'}[target_emoji])
        entered, release = asyncio.Event(), asyncio.Event()
        async def saving():
            if game.night_actions[1]['duel_finished']:
                entered.set()
                await release.wait()
            await persist()
        game.persist_flush = saving
        task = asyncio.create_task(duels.complete(game, 1, token))
        await entered.wait()
        assert game.state_lock.locked()
        resolving = asyncio.create_task(resolution.begin(game, guild))
        await asyncio.sleep(0)
        assert not resolving.done()
        release.set()
        await task
        await resolving
        assert game.night_actions[1]['duel_finished'] and game.night_actions[1]['duel_won'] is won
        assert all(not snap[1]['duel_finished'] or snap[1]['duel_won'] is won for snap in snapshots)
    asyncio.run(run())


@pytest.mark.parametrize("cancel_at", ["sync", "commit"])
def test_cancelled_pirate_duel_finishes_without_overwriting_a_victory(bot_module, monkeypatch, cancel_at):
    from gameplay import actions, duels
    module = bot_module
    game, guild, channel, players, fake_bot = setup_game(module, monkeypatch, "night")
    async def run():
        result = await actions.submit(game, 1, 'plunder', (2,))
        token = result.action['duel_token']
        await duels.choose(game, 1, token, 1, 'rock')
        await duels.choose(game, 1, token, 2, 'scissors')
        entered, release = asyncio.Event(), asyncio.Event()
        async def pause(*args):
            entered.set()
            await release.wait()
        if cancel_at == 'sync':
            game.sync_living_players = pause
        else:
            game.persist_flush = pause
        task = asyncio.create_task(duels.complete(game, 1, token, guild=guild))
        await entered.wait()
        task.cancel()
        if cancel_at == 'commit':
            await asyncio.sleep(0)
            assert game.state_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        if cancel_at == 'sync':
            assert not game.night_actions[1]['duel_finished']  # Durable session remains recoverable.
            game.sync_living_players = AsyncMock()
            await duels.complete(game, 1, token, guild=guild)
        assert game.night_actions[1]['duel_finished'] and game.night_actions[1]['duel_won']
        assert not game.state_lock.locked()
    asyncio.run(run())


def test_obsolete_resume_task_does_not_clean_up_a_new_game(bot_module, monkeypatch):
    module = bot_module
    old, guild, channel, players, fake_bot = setup_game(module, monkeypatch, "day")
    old.vote_in_progress = True
    old.tribunal_subphase = "defense"
    old.tribunal_defendant_id = 1
    new = Game(123)
    new.vote_in_progress = True
    new.tribunal_subphase = "defense"
    new.tribunal_defendant_id = 2

    async def sleep(remaining):
        module.active_games[123] = new

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    asyncio.run(module._resume_tribunal_defense_after_restart(guild, 1))
    assert new.vote_in_progress is True
    assert new.tribunal_defendant_id == 2
    old.persist_flush.assert_not_awaited()
    assert channel.sent == []
