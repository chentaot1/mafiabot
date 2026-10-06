from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

import discord

from config import DUEL_DURATION
from .state import Rejected, commit, identity, now, require_current


@dataclass(frozen=True)
class Ability:
    roles: tuple[str, ...]
    targets: int = 1
    allow_self: bool = False
    distinct: bool = False
    resource: str | None = None


ABILITIES = {
    "sk_kill": Ability(("Serial Killer",)),
    "cautious": Ability(("Serial Killer",), 0),
    "ward": Ability(("Guardian Angel",), resource="ga_ward_charges"),
    "gaze": Ability(("Seer",), 2, False, True),
    "kill": Ability(("Mobster",)), "heal": Ability(("Doctor",), allow_self=True),
    "roleblock": Ability(("Escort", "Consort")),
    "investigate": Ability(("Sheriff", "Investigator", "Mole")),
    "shoot": Ability(("Vigilante",), resource="shots_remaining"),
    "frame": Ability(("Framer",)), "hide": Ability(("Gravedigger",), resource="uses_remaining"),
    "transport": Ability(("Transporter",), 2, True, True),
    "protect": Ability(("Bodyguard",), allow_self=True),
    "watch": Ability(("Lookout",)), "track": Ability(("Tracker",)),
    "control": Ability(("Witch",), 2), "douse": Ability(("Arsonist",)),
    "reanimate": Ability(("Retributionist",), 1, True, resource="uses_remaining"),
    "chaos": Ability(("Chaos",), 2, True, True, "uses_remaining"),
    "hypnotize": Ability(("Hypnotist",)), "tailor": Ability(("Tailor",), resource="uses_remaining"),
    "plunder": Ability(("Pirate",)), "guard": Ability(("Gatekeeper",), resource="uses_remaining"),
    "vest": Ability(("Survivor",), 0, resource="vests_remaining"),
    "alert": Ability(("Scary Grandma",), 0, resource="alerts_remaining"),
    "ignite": Ability(("Arsonist",), 0), "clean": Ability(("Arsonist",), 0),
    "haunt": Ability(("Jester",)),
}
HYPNOTIST_MESSAGES = ("healed", "roleblocked", "transported", "controlled", "attacked")
from reanimate_expand import RETRI_CORPSE_EXPANDABLE_ROLES as CORPSE_ROLES


@dataclass(frozen=True)
class Submission:
    accepted: bool
    message: str
    action: dict | None = None


def usable_corpses(game, actor_id: int) -> list[dict]:
    used = {str(x) for x in game.role_states.get(actor_id, {}).get("used_corpses", [])}
    return [e for e in game.graveyard if isinstance(e, dict) and e.get("player_id") is not None
            and not e.get("used_by_retri") and not e.get("is_hidden")
            and str(e["player_id"]) not in used and e.get("real_role") in CORPSE_ROLES]


def abilities_for(game, actor_id: int) -> list[str]:
    role = game.player_roles.get(actor_id)
    state = game.role_states.get(actor_id,{})
    return [key for key, spec in ABILITIES.items() if role in spec.roles
            and (not spec.resource or state.get(spec.resource,0)>0)
            and (key != "haunt" or state.get("can_haunt"))
            and (key != "frame" or game.day_number<=2)
            and (key != "shoot" or not (state.get("will_die_of_guilt") or state.get("guilty_tomorrow")))
            and (key != "ward" or not state.get("ga_defeated"))
            and (key != "investigate" or role != 'Mole' or state.get('uses_remaining',0)>0)
            and (key != 'protect' or state.get('uses_remaining',0)>0 or state.get('self_protects_remaining',0)>0)]


def build_action(game, actor_id: int, ability: str, targets=(), *, corpse_id=None,
                 message_type=None, fake_role=None) -> dict:
    """Pure validation/payload factory. Commands and components use this exact path."""
    spec = ABILITIES.get(ability)
    role = game.player_roles.get(actor_id)
    if spec is None or role not in spec.roles:
        raise Rejected("This ability is not available to your current role.")
    state = game.role_states.get(actor_id, {})
    living = {p.id for p in game.living_players}
    if ability == "haunt":
        if actor_id in living or not state.get("can_haunt"):
            raise Rejected("Your spirit cannot haunt now.")
        if not targets or targets[0] not in living or targets[0] not in state.get("guilty_voters", []):
            raise Rejected("Choose an eligible living Guilty or abstaining voter.")
        return {"type": "haunt", "actor": actor_id, "target": targets[0]}
    if actor_id not in living and ability != 'ward':
        raise Rejected("Only living players can submit night actions.")
    if spec.resource and state.get(spec.resource, 0) <= 0:
        raise Rejected("You have no uses remaining for this ability.")
    if ability == "investigate" and role == "Mole" and state.get("uses_remaining", 0) <= 0:
        raise Rejected("You have no investigations left.")
    if ability == "shoot" and (state.get("will_die_of_guilt") or state.get("guilty_tomorrow")):
        raise Rejected("You are overcome with guilt.")
    if ability == "ward" and state.get("ga_defeated"):
        raise Rejected("Your bound player has died; you cannot ward.")
    if ability == "frame" and game.day_number > 2:
        raise Rejected("You can only frame on Nights 1 and 2.")

    corpse = None
    required = spec.targets
    if ability == "reanimate":
        corpse = next((e for e in usable_corpses(game, actor_id)
                       if str(e["player_id"]) == str(corpse_id)), None)
        if corpse is None:
            raise Rejected("That corpse is no longer usable. Reopen the corpse list.")
        required = 2 if corpse["real_role"] == "Transporter" else 1
    targets = tuple(targets[:required])
    if len(targets) != required:
        raise Rejected(f"Choose {required} target(s).")
    if any(t not in living for t in targets):
        raise Rejected("A selected target died or left. Choose a living player.")
    if ability == "ward" and targets[0] != state.get("ga_target_id"):
        raise Rejected("You may only ward your bound player.")
    if ability == 'guard':
        from config import ALL_MAFIA_ROLES
        if game.player_roles.get(targets[0]) in ALL_MAFIA_ROLES:
            raise Rejected('You cannot guard a Mafia player.')
        if (state.get('gatekeeper_last_successful_guard_day_number') == game.day_number - 1
                and state.get('gatekeeper_last_guard_target_id') == targets[0]):
            raise Rejected('You cannot guard that player on consecutive nights.')
    if ability == 'gaze':
        if any(game.player_roles.get(t) == 'Mayor' and game.role_states.get(t, {}).get('is_revealed') for t in targets):
            raise Rejected('You cannot gaze a revealed Mayor.')
        if sorted(targets) in [sorted(pair) for pair in state.get('seer_pair_history', [])]:
            raise Rejected('You have already gazed this pair.')
    if not spec.allow_self and actor_id in targets:
        raise Rejected("You cannot target yourself.")
    if (spec.distinct or (corpse and required == 2)) and len(set(targets)) != len(targets):
        raise Rejected("You must choose two different people.")
    if ability == "heal" or (corpse and corpse["real_role"] == "Doctor"):
        if targets and game.player_roles.get(targets[0]) == 'Mayor' and game.role_states.get(targets[0], {}).get("is_revealed"):
            raise Rejected("Cannot heal a revealed Mayor!")
        if ability == "heal" and targets[0] == actor_id and state.get("self_heals_remaining", 0) <= 0:
            raise Rejected("You have already used your self-heal!")
    action = {"type": ability, "actor": actor_id}
    if required == 2:
        action["targets"] = list(targets)
    elif required:
        action["target"] = targets[0]
    if ability == "protect":
        self_target = targets[0] == actor_id
        if state.get("self_protects_remaining" if self_target else "uses_remaining", 0) <= 0:
            raise Rejected("You have already used this protection.")
        if self_target:
            action["type"] = "bg_vest"
    if ability == "investigate":
        action["role"] = role
    if ability == "vest":
        action["target"] = actor_id
    if ability == "hypnotize":
        message_type = str(message_type or "").lower()
        if message_type not in HYPNOTIST_MESSAGES:
            raise Rejected("Choose a valid Hypnotist message.")
        action["msg_type"] = message_type
    if ability == "tailor":
        if fake_role is None:
            raise Rejected("Enter the fake role text.")
        action["fake_role"] = discord.utils.escape_mentions(discord.utils.escape_markdown(str(fake_role)[:20]))
    if corpse:
        action.update(corpse_player_id=corpse["player_id"], corpse_role=corpse["real_role"], target=targets[0])
    if ability == "plunder":
        action.update(duel_won=False, duel_finished=False, duel_token=secrets.token_hex(8),
                      duel_day=game.day_number, duel_match=game.game_key,
                      duel_deadline=(now() + timedelta(seconds=DUEL_DURATION)).isoformat(),
                      duel_choices={}, duel_prompts={}, duel_delivered=[])
    return action


async def submit(game, actor_id: int, ability: str, targets=(), *, expected=None,
                 guild=None, cooldown=True, **options) -> Submission:
    expected = expected if expected is not None else identity(game)
    if guild is not None:
        await game.sync_living_players(guild)
    def update():
        require_current(game, phase="night", expected=expected)
        from night_action_guards import night_actions_frozen
        if night_actions_frozen(game):
            raise Rejected("Night is resolving. Your action cannot be changed.")
        existing = game.night_actions.get(actor_id)
        if existing and existing.get("type") == "plunder" and not existing.get("duel_finished"):
            raise Rejected("Your plunder duel is already in progress.")
        action = build_action(game, actor_id, ability, targets, **options)
        key = (actor_id, ability)
        if cooldown and time.monotonic() < game.action_cooldowns.get(key, 0):
            raise Rejected("Please wait two seconds between uses of this ability.")
        if ability == "haunt":
            game.role_states[actor_id].update(haunt_target=action["target"], can_haunt=False)
        elif ability == 'cautious':
            state = game.role_states.setdefault(actor_id, {})
            state['sk_cautious'] = not bool(state.get('sk_cautious'))
            return Submission(True, 'You are now ' + ('Cautious.' if state['sk_cautious'] else 'Aggressive.'), action)
        else:
            game.night_actions[actor_id] = action
            if ability == 'sk_kill':
                game.role_states.setdefault(actor_id, {})['sk_target_id'] = action['target']
        if cooldown:
            game.action_cooldowns[key] = time.monotonic() + 2
        return Submission(True, f"Saved **{ability}** for tonight." +
                          (" Your previous action was replaced." if existing else ""), action)
    try:
        result = await commit(game, update)
    except Rejected as error:
        return Submission(False, str(error))
    except OSError:
        return Submission(False, "Your action could not be saved. Your previous action is still in place; please try again.")
    return result
