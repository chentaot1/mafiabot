"""Compatibility exports from the running application; never import its entrypoint."""
from config import load_allowed_guild_id
from game import _require_bot, get_game_by_player_id
from checks import only_during_night_gameplay as night_decorator

ALLOWED_GUILD_ID = load_allowed_guild_id()


def only_during_night_gameplay():
    return night_decorator(bot=_require_bot(), get_game_by_player_id=get_game_by_player_id)


def __getattr__(name):
    if name == 'bot':
        return _require_bot()
    raise AttributeError(name)
