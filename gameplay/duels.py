from __future__ import annotations

import random
from copy import deepcopy

from . import state as st

CHOICES = ("rock", "paper", "scissors")


def get_duel(game, actor_id, token, *, open_only=True):
    st.require_current(game, phase="night" if open_only else None)
    action = game.night_actions.get(actor_id)
    if not open_only and (not isinstance(action,dict) or action.get('duel_token') != token):
        action = game.gameplay.get('duels', {}).get(token)
    if (not isinstance(action, dict) or action.get("type") != "plunder"
            or action.get('actor') != actor_id or action.get("duel_token") != token
            or (open_only and action.get("duel_day", game.day_number) != game.day_number)
            or action.get("duel_match", game.game_key) != game.game_key):
        raise st.Rejected("This duel is no longer active.")
    if open_only and action.get("duel_finished"):
        raise st.Rejected("This duel is already complete.")
    return action


async def choose(game, actor_id, token, uid, choice, *, guild=None):
    if guild:
        await game.sync_living_players(guild)
    def update():
        action = get_duel(game, actor_id, token)
        if st.remaining(action.get("duel_deadline")) <= 0:
            raise st.Rejected("The duel deadline has passed.")
        if uid not in (actor_id, action["target"]) or uid not in {p.id for p in game.living_players}:
            raise st.Rejected("This duel belongs to another player.")
        if choice not in CHOICES:
            raise st.Rejected("Choose Rock, Paper, or Scissors.")
        if str(uid) in action["duel_choices"]:
            raise st.Rejected("Your first choice is locked.")
        action["duel_choices"][str(uid)] = choice
    await st.commit(game, update)


async def complete(game, actor_id, token, *, guild=None):
    if guild:
        await game.sync_living_players(guild)
    def update():
        action = get_duel(game, actor_id, token, open_only=False)
        if action.get("duel_finished"):
            return action
        living = {p.id for p in game.living_players}
        valid = actor_id in living and action["target"] in living
        choices = action.get("duel_choices", {})
        both = all(str(uid) in choices for uid in (actor_id, action["target"]))
        legacy = not action.get("duel_deadline")
        if valid and not legacy and not both and st.remaining(action["duel_deadline"]) > 0:
            raise st.Rejected("The duel is still open.")
        if valid and not legacy:
            for uid in (actor_id, action["target"]):
                choices.setdefault(str(uid), random.choice(CHOICES))
            pirate, target = choices[str(actor_id)], choices[str(action["target"])]
            won = (pirate, target) in {("rock", "scissors"), ("scissors", "paper"), ("paper", "rock")}
            result = f"Pirate chose **{pirate}**; target chose **{target}**. "
            result += "The Pirate wins!" if won else ("Draw." if pirate == target else "The target wins!")
        else:
            won = False
            result = "The duel was cancelled because a participant left, died, or its recovery data was missing."
        # Persist result and release of the resolve guard in the same transaction.
        action.update(duel_choices=choices, duel_won=won, duel_finished=True, duel_result=result)
        game.gameplay.setdefault('duels', {})[token] = deepcopy(action)
        return action
    return await st.commit(game, update)
