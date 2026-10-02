import json
import os
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

# Only load this project's local settings, never an ancestor project's .env.
load_dotenv(Path(__file__).with_name(".env"))


def _env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer.") from error
    if value < 0:
        raise RuntimeError(f"{name} must not be negative.")
    return value


def _load_private_channel_ids() -> Dict[int, int]:
    raw = os.getenv("PLAYER_PRIVATE_CHANNEL_IDS", "{}").strip() or "{}"
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("PLAYER_PRIVATE_CHANNEL_IDS must be a JSON object.") from error
    if not isinstance(mapping, dict):
        raise RuntimeError("PLAYER_PRIVATE_CHANNEL_IDS must be a JSON object.")
    result: Dict[int, int] = {}
    for player, channel in mapping.items():
        try:
            player_id, channel_id = int(str(player)), int(str(channel))
        except ValueError as error:
            raise RuntimeError("Private channel mappings must contain integer IDs.") from error
        if player_id <= 0 or channel_id <= 0:
            raise RuntimeError("Private channel mappings must contain positive IDs.")
        result[player_id] = channel_id
    return result


# --- CONFIGURATION ---
GAME_MASTER_ROLE = "Game Overseer"
ALIVE_ROLE_NAME = "Mafia - Alive"
STAND_ROLE_NAME = "Mafia - On Stand"
DAY_VOICE_CHANNEL_NAME = "Town Square"
DAY_TEXT_CHANNEL_NAME = "town-square-chat"
MAFIA_CHANNEL_NAME = "mafia-chat"
GRAVEYARD_TEXT_CHANNEL_NAME = "graveyard"
GRAVEYARD_VOICE_CHANNEL_NAME = "Graveyard Voice"
PLAYING_ROLE_ID = _env_int("PLAYING_ROLE_ID")
GAME_CATEGORY_ID = _env_int("GAME_CATEGORY_ID")
GAME_OVERSEER_ROLE_ID = _env_int("GAME_OVERSEER_ROLE_ID")

# --- CONSTANTS ---
DUEL_DURATION = 30
VOTE_DURATION = 300
VOTE_LIMIT_PER_DAY = 2

# Tribunal resume floor (seconds): if less wall-clock remains after restart, abort instead of resuming (B4).
TRIBUNAL_RESUME_MIN_SECONDS = int(os.getenv("TRIBUNAL_RESUME_MIN_SECONDS", "5"))

ALL_MAFIA_ROLES: List[str] = [
    "Mobster", "Framer", "Gravedigger", "Consort",
    "Hypnotist", "Mole", "Tailor", "Gatekeeper",
]

TOWN_ROLES: List[str] = [
    "Retributionist", "Vigilante", "Sheriff", "Investigator", "Doctor",
    "Escort", "Transporter", "Mayor", "Bodyguard",
    "Lookout", "Scary Grandma", "Tracker",
]

ROLEBLOCK_IMMUNE_ROLES: List[str] = [
    "Scary Grandma", "Witch", "Consort", "Escort", "Pirate", "Transporter",
]

CONTROL_IMMUNE_ROLES: List[str] = [
    "Transporter", "Scary Grandma", "Witch", "Pirate", "Chaos",
]

# --- PLAYER PRIVATE CHANNELS ---
# Optional local mapping: Discord user ID -> private channel ID.
# Leave empty to use DMs. Configure your own mapping in the ignored .env file.
PLAYER_PRIVATE_CHANNEL_IDS: Dict[int, int] = _load_private_channel_ids()


def load_allowed_guild_id() -> int:
    return _env_int("ALLOWED_GUILD_ID")


def validate_live_settings() -> None:
    """Require server-specific settings before starting a real gateway session."""
    required = {
        "ALLOWED_GUILD_ID": load_allowed_guild_id(),
        "PLAYING_ROLE_ID": PLAYING_ROLE_ID,
        "GAME_OVERSEER_ROLE_ID": GAME_OVERSEER_ROLE_ID,
    }
    missing = [name for name, value in required.items() if value <= 0]
    if missing:
        raise RuntimeError("Configure positive Discord IDs in .env: " + ", ".join(missing))
