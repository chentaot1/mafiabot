import json
import os
from pathlib import Path
from typing import Dict, Iterable, List

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'), override=False)

# --- CONFIGURATION ---


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


GAME_MASTER_ROLE = "Game Overseer"
ALIVE_ROLE_NAME = "Mafia - Alive"
STAND_ROLE_NAME = "Mafia - On Stand"
DAY_VOICE_CHANNEL_NAME = "Town Square"
DAY_TEXT_CHANNEL_NAME = "town-square-chat"
MAFIA_CHANNEL_NAME = "mafia-chat"
GRAVEYARD_TEXT_CHANNEL_NAME = "graveyard"
GRAVEYARD_VOICE_CHANNEL_NAME = "Graveyard Voice"
PLAYING_ROLE_ID = _env_int("PLAYING_ROLE_ID", 0)
GAME_CATEGORY_ID = _env_int("GAME_CATEGORY_ID", 0)
GAME_OVERSEER_ROLE_ID = _env_int("GAME_OVERSEER_ROLE_ID", 0)

# --- CONSTANTS ---
# Night-role bookkeeping: Chaos starting uses must match bot.startgame + the
# crash-recovery heuristic in engine/night.run_night_pipeline.
CHAOS_STARTING_USES = 2

# Two-charge abilities (Survivor vest, Gatekeeper guard, Scary Grandma alert,
# Retributionist reanimate, Chaos) start with 1 charge at 7p or fewer.
SMALL_LOBBY_MAX_PLAYER_COUNT = 7


def role_starting_charges(*, player_count: int, full_charges: int = 2) -> int:
    """Scale limited-use abilities down in small lobbies."""
    if full_charges <= 1:
        return full_charges
    if player_count <= SMALL_LOBBY_MAX_PLAYER_COUNT:
        return 1
    return full_charges


def chaos_starting_uses(player_count: int) -> int:
    return role_starting_charges(player_count=player_count, full_charges=CHAOS_STARTING_USES)

# Chaos `!chaos` random effect pool (engine/night.py run_night_pipeline). Order is stable for seeded RNG tests.
CHAOS_EFFECT_POOL: tuple[str, ...] = (
    "roleblock",
    "transport",
    "investigate",
    "watch",
    "track",
    "frame",
    "hide",
    "guard",
)

# Witch personal win: alive when Town loses to any primary evil outcome (stats + endgame).
WITCH_TOWN_LOSES_OUTCOMES = frozenset({"Mafia", "Arsonist", "Serial Killer"})

# Legacy alias: primary evil outcomes (Witch personal win). GA bind logic lives in guardian_angel_wins.py;
# stats key is guardian_angel_win (joint bind or stalemate override).
GUARDIAN_ANGEL_JOINT_WIN_OUTCOMES = frozenset({"Town"}) | WITCH_TOWN_LOSES_OUTCOMES


def guardian_angel_bind_pool_ids(all_player_ids: Iterable[int], ga_id: int) -> List[int]:
    """Any other player may be the GA bind (Jester, Executioner, other GAs included)."""
    ga = int(ga_id)
    return [int(pid) for pid in all_player_ids if int(pid) != ga]

# --- Combat tiers (ToS1): none < basic < powerful < unstoppable; invincible blocks all attacks.
# Kill when attack > defense (unstoppable pierces up to powerful; not invincible).
ROLE_PASSIVE_DEFENSE: Dict[str, str] = {
    "Serial Killer": "basic",
    "Arsonist": "basic",
    "Executioner": "basic",
    # Mobster: none (not ToS Godfather basic defense — intentional for this bot).
    # Guardian Angel: none; !ward grants invincible defense on bind that night only.
    # Survivor / Bodyguard / Scary Grandma: none; vest/alert grant basic via role_states.
}

ATTACK_TIER_BY_NIGHT_ACTION: Dict[str, str] = {
    "kill": "basic",
    "shoot": "basic",
    "sk_kill": "basic",
    "sk_counter": "basic",
    "plunder": "powerful",
}

BODYGUARD_COUNTER_ATTACK_TIER: str = "powerful"
DEPUTY_DAY_ATTACK_TIER: str = "unstoppable"
# Used by engine/combat.py for ignite (unstoppable) and Scary Grandma alert (powerful).
IGNITE_ATTACK_TIER: str = "unstoppable"
SCARY_GRANDMA_ALERT_ATTACK_TIER: str = "powerful"

# role_states keys → defense tier for that night (max with passive).
NIGHT_DEFENSE_BY_ROLE_STATE: Dict[str, str] = {
    "is_vested": "basic",
    "is_on_alert": "basic",
    "ga_shield_active_tonight": "invincible",
}

HEAL_DEFENSE_TIER: str = "powerful"

# Night 1: first basic-tier kill fails (not powerful/unstoppable); ignite uses IGNITE_ATTACK_TIER.
N1_BASIC_SHIELD_ROLES: frozenset[str] = frozenset({"Witch", "Chaos", "Jester"})

DUEL_DURATION = 30
VOTE_DURATION = 300
VOTE_LIMIT_PER_DAY = 2

# End in Draw if this many consecutive day+night cycles have zero deaths (lynch or night kill).
STALEMATE_DRAW_CYCLES = 3

# Tribunal resume floor (seconds): if less wall-clock remains after restart, abort instead of resuming (B4).
TRIBUNAL_RESUME_MIN_SECONDS = _env_int("TRIBUNAL_RESUME_MIN_SECONDS", 30)

ALL_MAFIA_ROLES: List[str] = [
    "Mobster", "Framer", "Gravedigger", "Consort",
    "Hypnotist", "Mole", "Tailor", "Gatekeeper",
]

TOWN_ROLES: List[str] = [
    "Retributionist",
    "Vigilante",
    "Sheriff",
    "Investigator",
    "Doctor",
    "Escort",
    "Transporter",
    "Mayor",
    "Bodyguard",
    "Lookout",
    "Scary Grandma",
    "Tracker",
    "Psychic",
    "Deputy",
    "Seer",
]

# Neutral benign roles (not in TOWN_ROLES); used for Psychic even-night pool and role lists.
NEUTRAL_BENIGN_ROLES: List[str] = ["Survivor", "Guardian Angel"]

# Seer Friends bucket allies beyond TOWN_ROLES (GA protects a town bind; Jester is wiki-parity friendly).
SEER_FRIENDLY_EXTRA_ROLES: List[str] = ["Guardian Angel", "Jester"]

# Psychic odd-night "evil pool" (plus ALL_MAFIA_ROLES, framed, doused at delivery time).
PSYCHIC_ODD_EVIL_NEUTRALS: List[str] = [
    "Witch",
    "Executioner",
    "Jester",
    "Pirate",
    "Arsonist",
    "Chaos",
    "Serial Killer",
]

# Seer Friends / Enemies bucket helpers (B3/B4 fixed sets; B1/B2 derived at runtime).
SEER_NEUTRAL_KILLING_ROLES: List[str] = ["Arsonist", "Serial Killer"]
SEER_HOSTILE_NEUTRAL_ROLES: List[str] = ["Survivor", "Chaos", "Executioner", "Witch", "Pirate"]

# Deputy daytime gun: neutral roles read as "evil" for friendly-fire vs kill-only paths.
DEPUTY_GUN_EVIL_NEUTRALS: List[str] = [
    "Witch",
    "Executioner",
    "Jester",
    "Pirate",
    "Arsonist",
    "Chaos",
    "Serial Killer",
]

ROLEBLOCK_IMMUNE_ROLES: List[str] = [
    "Retributionist",
    "Scary Grandma",
    "Witch",
    "Consort",
    "Escort",
    "Pirate",
    "Transporter",
    "Serial Killer",
]

# Pirate plunder roleblocks these roles despite ROLEBLOCK_IMMUNE (ToS1 Transporter wiki + MafiaSalem parity).
PIRATE_PLUNDER_ROLEBLOCK_OVERRIDES: List[str] = ["Transporter", "Escort", "Consort"]

# Witch cannot redirect these roles' night actions. Gatekeeper is intentionally omitted —
# Witch may control a Gatekeeper (GK still blocks visitors to a guarded target via guard).
CONTROL_IMMUNE_ROLES: List[str] = [
    "Retributionist",
    "Transporter",
    "Scary Grandma",
    "Witch",
    "Pirate",
    "Chaos",
    "Guardian Angel",
]

# --- PLAYER PRIVATE CHANNELS ---
# Map Discord user_id -> private channel_id (inside your server).
# Night-action commands (`!heal`, `/heal`, etc.) can be restricted to only work in
# the player's mapped channel. On `!startgame`, the bot also posts there (ping +
# role + command hints) in addition to DMs/outbox.
# Optional override: MAFIABOT_PRIVATE_CHANNELS_JSON='{"123":456,"789":101}'
def _load_private_channel_ids() -> Dict[int, int]:
    raw = os.getenv("PLAYER_PRIVATE_CHANNEL_IDS", os.getenv("MAFIABOT_PRIVATE_CHANNELS_JSON", "{}")).strip() or "{}"
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

_load_player_private_channel_ids = _load_private_channel_ids


PLAYER_PRIVATE_CHANNEL_IDS: Dict[int, int] = _load_player_private_channel_ids()


def load_allowed_guild_id() -> int:
    return _env_int("ALLOWED_GUILD_ID")


def validate_deployment_config(*, strict: bool | None = None) -> list[str]:
    """
    Return configuration warnings for single-server deploys.

    Set MAFIABOT_STRICT_CONFIG=1 (or pass strict=True) to raise on missing env vars.
    """
    if strict is None:
        strict = os.getenv("MAFIABOT_STRICT_CONFIG", "").strip().lower() in ("1", "true", "yes", "on")
    issues: list[str] = []
    if not os.getenv("DISCORD_TOKEN", "").strip():
        issues.append("DISCORD_TOKEN is not set")
    if not os.getenv("ALLOWED_GUILD_ID", "").strip():
        issues.append("ALLOWED_GUILD_ID is not set (using built-in default guild id)")
    for name in ("PLAYING_ROLE_ID", "GAME_CATEGORY_ID", "GAME_OVERSEER_ROLE_ID"):
        if not os.getenv(name, "").strip():
            issues.append(f"{name} is not set (using built-in default)")
    if strict and issues:
        raise RuntimeError("Deployment config invalid:\n- " + "\n- ".join(issues))
    return issues



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
