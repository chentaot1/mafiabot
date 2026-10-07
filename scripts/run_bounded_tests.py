"""Run every pytest case with default pool sizing limited to two logical CPUs."""
from pathlib import Path
import os
import sys
import tempfile


def offline_environment():
    # Spawned Windows workers import this module before unpickling game code.
    # Never load deployment credentials in the isolated validation profile.
    if os.environ.get('MAFIABOT_OFFLINE_CHECKS') == '1':
        import dotenv
        dotenv.load_dotenv = lambda *args, **kwargs: False
        os.environ.update(DISCORD_TOKEN='offline-test-token', ALLOWED_GUILD_ID='123',
            GAME_OVERSEER_ROLE_ID='42', PLAYING_ROLE_ID='43', GAME_CATEGORY_ID='0',
            PLAYER_PRIVATE_CHANNEL_IDS='{}', PYTHON_DOTENV_DISABLED='1')


offline_environment()

def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    # Tests that override cpu_count still exercise their own sizing contracts.
    # Production code and explicit worker-count tests are unaffected.
    os.cpu_count = lambda: 2
    args = sys.argv[1:]
    isolated = '--isolated' in args
    args = [arg for arg in args if arg != '--isolated']
    def run():
        import pytest
        return pytest.main(['-q', '--tb=short', '-o', 'addopts=', '-o', 'faulthandler_timeout=45', *args])
    if not isolated:
        return run()
    with tempfile.TemporaryDirectory(prefix='mafiabot-offline-checks-') as folder:
        os.environ.update(MAFIABOT_OFFLINE_CHECKS='1', MAFIABOT_STATE_DIR=str(Path(folder) / 'state'))
        offline_environment()
        import game
        game._dbg = lambda *args, **kwargs: None
        # Construct command objects without opening a Discord connection.
        import bot
        bot._dbg = lambda *args, **kwargs: None
        return run()

if __name__ == '__main__':
    raise SystemExit(main())
