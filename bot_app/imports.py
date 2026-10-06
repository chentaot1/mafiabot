import discord
from discord.ext import commands
import random
import asyncio
import logging
from typing import Any, Callable, Collection, List, Dict, Optional, Tuple, Set
import os
from datetime import datetime, timedelta, timezone
import secrets
from pathlib import Path
import json
import time
import sys
import ctypes
from roles import get_role_description, role_start_dm_supplements, role_start_private_channel_lines
from player_channels import private_text_channel_id_for_user, send_to_player_private_channel
from persistence import load_state
from persistence import load_stats
from checks import (
    only_during_night_gameplay as only_during_night_gameplay_factory,
    enforce_allowed_guild,
    night_autocomplete_living_slots_ok,
)
from guild_resolve import resolve_game_guild
from errors import on_app_command_tree_error, on_command_error as on_command_error_handler
from game import Game, active_games, bind_bot, get_game_by_player_id, get_game_for_guild
import game_roles
from engine.night import (
    run_night_pipeline,
    deliver_psychic_visions,
    snapshot_attacked_tonight_reasons,
    snapshot_healed_by_map,
    restore_healed_by_map,
    clear_attacked_tonight_reasons,
)
from messages.delivery import game_text_channel, post_game_channel, post_game_channel_embed
import messages.tos as tos_msg
from messages.role_catalog import consig_blurb
from database import Database
from config import (
    CHAOS_STARTING_USES,
    chaos_starting_uses,
    role_starting_charges,
    TRIBUNAL_RESUME_MIN_SECONDS,
    GAME_MASTER_ROLE,
    GAME_OVERSEER_ROLE_ID,
    ALIVE_ROLE_NAME,
    STAND_ROLE_NAME,
    DAY_VOICE_CHANNEL_NAME,
    MAFIA_CHANNEL_NAME,
    GRAVEYARD_TEXT_CHANNEL_NAME,
    GRAVEYARD_VOICE_CHANNEL_NAME,
    PLAYING_ROLE_ID,
    DUEL_DURATION,
    VOTE_DURATION,
    VOTE_LIMIT_PER_DAY,
    ALL_MAFIA_ROLES,
    TOWN_ROLES,
    DEPUTY_GUN_EVIL_NEUTRALS,
    PLAYER_PRIVATE_CHANNEL_IDS,
    ROLEBLOCK_IMMUNE_ROLES,
    CONTROL_IMMUNE_ROLES,
    load_allowed_guild_id,
    guardian_angel_bind_pool_ids,
)
