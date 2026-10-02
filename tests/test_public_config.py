"""Public installs must never inherit a maintainer's server configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETTING_KEYS = (
    "ALLOWED_GUILD_ID", "PLAYING_ROLE_ID", "GAME_OVERSEER_ROLE_ID",
    "GAME_CATEGORY_ID", "PLAYER_PRIVATE_CHANNEL_IDS",
    "TRIBUNAL_RESUME_MIN_SECONDS",
)


def run_config(code: str, **settings: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in SETTING_KEYS:
        env[key] = ""  # Also prevents dotenv from overriding test settings.
    env["TRIBUNAL_RESUME_MIN_SECONDS"] = "5"
    env.update(settings)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=10,
    )


def test_default_install_has_no_server_or_player_ids() -> None:
    result = run_config(
        "import config; import json; print(json.dumps([config.load_allowed_guild_id(), "
        "config.PLAYING_ROLE_ID, config.GAME_CATEGORY_ID, "
        "config.GAME_OVERSEER_ROLE_ID, config.PLAYER_PRIVATE_CHANNEL_IDS]))"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [0, 0, 0, 0, {}]


def test_startup_requires_local_server_and_role_settings() -> None:
    result = run_config("import config; config.validate_live_settings()")
    assert result.returncode != 0
    for key in ("ALLOWED_GUILD_ID", "PLAYING_ROLE_ID", "GAME_OVERSEER_ROLE_ID"):
        assert key in result.stderr


def test_configured_startup_allows_automatic_category_and_private_mapping() -> None:
    result = run_config(
        "import config; config.validate_live_settings(); "
        "assert config.GAME_CATEGORY_ID == 0; "
        "assert config.PLAYER_PRIVATE_CHANNEL_IDS == {101: 202}",
        ALLOWED_GUILD_ID="11", PLAYING_ROLE_ID="12", GAME_OVERSEER_ROLE_ID="13",
        PLAYER_PRIVATE_CHANNEL_IDS='{"101": 202}',
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mapping", [
    "not json", "[]", "null", '{"101": true}', '{"101": 2.5}',
    '{"name": 202}', '{"101": 0}', '{"-1": 202}',
])
def test_invalid_private_channel_mapping_fails_before_connection(mapping: str) -> None:
    result = run_config("import config", PLAYER_PRIVATE_CHANNEL_IDS=mapping)
    assert result.returncode != 0
    assert "RuntimeError" in result.stderr


@pytest.mark.parametrize("value", ["invalid", "-1"])
def test_invalid_server_id_is_rejected(value: str) -> None:
    result = run_config("import config; config.load_allowed_guild_id()", ALLOWED_GUILD_ID=value)
    assert result.returncode != 0
    assert "ALLOWED_GUILD_ID" in result.stderr
