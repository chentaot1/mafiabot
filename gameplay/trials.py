from __future__ import annotations

import secrets
from datetime import timedelta

from config import GAME_OVERSEER_ROLE_ID, VOTE_DURATION, VOTE_LIMIT_PER_DAY
from . import state as st
from .death import apply_death

DEFENSE_DURATION = 45
JUDGMENT_DURATION = 30
JUDGMENTS = ("guilty", "innocent", "abstain")


def weight(game, uid):
    return 2 if game.player_roles.get(uid) == "Mayor" and game.role_states.get(uid, {}).get("is_revealed") else 1


def clear_flags(game):
    game.vote_in_progress = game.tribunal_muted = False
    game.tribunal_defendant_id = None
    game.tribunal_defense_deadline_utc = game.tribunal_judgment_deadline_utc = None
    game.tribunal_judgment_message_id = game.tribunal_subphase = None
    # The new receipt survives cleanup; the legacy marker remains for old snapshots.


def cancel_model(game, trial, reason, *, refund=False):
    if trial.get("applied"):
        return
    if refund and trial.get("counted") and not trial.get("refunded"):
        game.votes_today = max(0, game.votes_today - 1)
        trial["refunded"] = True
    trial.update(stage="cancelled", reason=reason, progressed=True)
    clear_flags(game)


async def start(game, *, role_ids, channel_id, guild=None, actor_id=None):
    if guild:
        await game.sync_living_players(guild)
        if actor_id is not None:
            await game.get_member_safe(guild,actor_id)
    def update():
        st.require_current(game, phase="day")
        current_roles=role_ids
        if guild and actor_id is not None:
            member=guild.get_member(actor_id)
            current_roles=[r.id for r in member.roles] if member else []
        if GAME_OVERSEER_ROLE_ID not in current_roles:
            raise st.Rejected("Only the Game Overseer can initiate the Tribunal.")
        previous = game.gameplay.get("trial") or {}
        if game.resolving or game.vote_in_progress or (previous.get("result") and not previous.get("progressed")):
            raise st.Rejected("A trial or phase transition is already underway.")
        if game.votes_today >= VOTE_LIMIT_PER_DAY:
            raise st.Rejected("No more trials are available today.")
        trial = {"id": secrets.token_hex(8), "match": game.game_key, "day": game.day_number, "stage": "nomination",
                 "deadline": (st.now() + timedelta(seconds=VOTE_DURATION)).isoformat(),
                 "channel_id": channel_id, "message_id": None,
                 "nominations": {}, "judgments": {}, "defendant": None,
                 "result": None, "counted": False, "refunded": False,
                 "applied": False, "progressed": False, "permissions_cleaned": False}
        game.gameplay["trial"] = trial
        game.vote_in_progress = True
        game.tribunal_subphase = "nomination"
        game.tribunal_verdict_committed = False
        return trial
    return await st.commit(game, update)


async def cast(game, token, uid, choice, stage, *, guild=None):
    if guild:
        await game.sync_living_players(guild)
    def update():
        trial = st.session(game, token, phase="day")
        if not game.vote_in_progress or trial["stage"] != stage or st.remaining(trial["deadline"]) <= 0:
            raise st.Rejected("Voting has closed for this round.")
        living = {p.id for p in game.living_players}
        if uid not in living:
            raise st.Rejected("Only living players can vote.")
        if stage == "nomination":
            if choice is not None and (choice not in living or choice == uid):
                raise st.Rejected("Choose another living player.")
            if choice is not None and game.role_states.get(choice, {}).get('ga_trial_lock_day') == game.day_number:
                raise st.Rejected('That player is protected from nomination today.')
            trial["nominations"][str(uid)] = choice
        elif stage == "judgment":
            if uid == trial["defendant"]:
                raise st.Rejected("The defendant cannot pass judgment.")
            if choice not in JUDGMENTS:
                raise st.Rejected("Choose Guilty, Innocent, or Abstain.")
            trial["judgments"][str(uid)] = choice
        else:
            raise st.Rejected("Voting is not open.")
    await st.commit(game, update)


def nomination_tally(game, trial):
    living = {p.id for p in game.living_players}
    totals = {uid: 0 for uid in living}
    for uid in living:
        target = trial.get("nominations", {}).get(str(uid))
        if (target in living and target != uid
                and game.role_states.get(target, {}).get('ga_trial_lock_day') != game.day_number):
            totals[target] += weight(game, uid)
    return totals


async def advance(game, token, *, guild=None):
    if guild:
        await game.sync_living_players(guild)
    def update():
        trial = st.session(game, token, phase="day")
        if st.remaining(trial["deadline"]) > 0:
            raise st.Rejected("This round is still open.")
        stage = trial["stage"]
        living = {p.id for p in game.living_players}
        if stage == "nomination":
            totals = nomination_tally(game, trial)
            highest = max(totals.values(), default=0)
            winners = [uid for uid, total in totals.items() if total == highest]
            trial["nomination_totals"] = {str(k): v for k, v in totals.items()}
            if highest == 0 or len(winners) != 1:
                trial.update(stage="done", reason="No nomination." if highest == 0 else "Nominations tied.", progressed=True)
                clear_flags(game)
                return trial
            trial.update(defendant=winners[0], counted=True, stage="defense",
                         deadline=(st.now() + timedelta(seconds=DEFENSE_DURATION)).isoformat())
            game.votes_today += 1
            game.tribunal_defendant_id = winners[0]
            game.tribunal_muted = True
            game.tribunal_defense_deadline_utc = trial["deadline"]
        elif trial["defendant"] not in living:
            cancel_model(game, trial, "The defendant died or left; the trial was cancelled.", refund=True)
            return trial
        elif stage == "defense":
            trial.update(stage="judgment", deadline=(st.now() + timedelta(seconds=JUDGMENT_DURATION)).isoformat())
            game.tribunal_muted = False
            game.tribunal_defense_deadline_utc = None
            game.tribunal_judgment_deadline_utc = trial["deadline"]
        elif stage == "judgment":
            eligible = sorted(living - {trial["defendant"]}, key=lambda uid: game.player_slots.get(uid, uid))
            choices = {str(uid): trial["judgments"].get(str(uid), "abstain") for uid in eligible}
            choices = {uid: c if c in JUDGMENTS else "abstain" for uid, c in choices.items()}
            weights = {str(uid): weight(game, uid) for uid in eligible}
            totals = {c: sum(weights[uid] for uid, choice in choices.items() if choice == c) for c in JUDGMENTS}
            protected = game.role_states.get(trial['defendant'], {}).get('ga_trial_lock_day') == game.day_number
            trial["result"] = {"choices": choices, "weights": weights, "totals": totals,
                               "guilty": totals["guilty"] > totals["innocent"] and not protected,
                               "haunt_ids": [uid for uid in eligible if choices[str(uid)] != "innocent"]}
            trial["stage"] = "closed"
            game.tribunal_judgment_deadline_utc = None
        game.tribunal_subphase = trial["stage"]
        return trial
    return await st.commit(game, update)


async def apply_result(game, token):
    def update():
        trial = st.session(game, token, phase="day", open_only=False)
        if trial["stage"] != "closed" or not isinstance(trial.get("result"), dict):
            raise st.Rejected("There is no closed verdict to apply.")
        if not trial["applied"]:
            if trial["defendant"] not in {p.id for p in game.living_players}:
                cancel_model(game, trial, "The defendant died or left before the verdict was applied.", refund=True)
                return trial
            if trial["result"]["guilty"]:
                apply_death(game, trial["defendant"], "lynch", voters=trial["result"]["haunt_ids"],
                            custom_message=f"By vote, <@{trial['defendant']}> has been sentenced to the gallows.")
            trial["applied"] = True
            game.tribunal_verdict_committed = True
        return trial
    return await st.commit(game, update)


async def finish(game, token):
    def update():
        trial = st.session(game, token, open_only=False)
        trial["stage"] = "done"
        clear_flags(game)
    await st.commit(game, update)
