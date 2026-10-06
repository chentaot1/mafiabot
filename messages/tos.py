"""Wiki-verbatim ToS message templates (Specs 1–10)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from game import Game

# Substrings exported for smoke tests
TRANSPORTED = "You were transported to another location"
UNSTOPPABLE = "unstoppable"
ROLEBLOCKED = "Someone role blocked you, so you could not act!"
PEACEFUL_NIGHT = "The night was surprisingly peaceful. No one has died."

CAUSE_LINE_BY_TAG: dict[str, str] = {
    "sk_counter_attack": "They were stabbed by a Serial Killer.",
    "serial_killer": "They were stabbed by a Serial Killer.",
    "mafia": "They were killed by a member of the Mafia.",
    "arsonist_ignite": "They were incinerated by an Arsonist.",
    "pirate_plunder": "They were plundered by a Pirate.",
    "bodyguard": "They were killed by a Bodyguard.",
    "bodyguard_guard": "They died guarding someone.",
    "vigilante": "They were shot by a Vigilante.",
    "guilt": "They died from guilt.",
    "scary_grandma": "They were killed by a Scary Grandma.",
    "deputy_shoot": "They were shot by a Deputy.",
    "deputy_friendly_fire": "They were shot by a Deputy.",
    "deputy_friendly_fire_self": "They were shot by a Deputy.",
    "haunt": "They were haunted by a Jester.",
    "lynch": "",
    "night_kill": "",
}


def format_player(game: "Game", member_id: int) -> str:
    """Slot line; names from member_display_names (refreshed via get_member_safe in sync_living_players)."""
    slot = game.player_slots.get(member_id, "?")
    cached = getattr(game, "member_display_names", None) or {}
    if member_id in cached:
        return f"**Slot {slot} {cached[member_id]}**"
    name = "Unknown"
    for p in getattr(game, "living_players", []) or []:
        if p.id == member_id:
            name = p.display_name
            break
    if name == "Unknown":
        for p in getattr(game, "players", []) or []:
            if p.id == member_id:
                name = p.display_name
                break
    return f"**Slot {slot} {name}**"


async def format_player_async(game: "Game", guild: object, member_id: int) -> str:
    """Like format_player, but refreshes name from guild when missing from cache."""
    cached = getattr(game, "member_display_names", None) or {}
    updated = False
    if member_id not in cached and guild is not None:
        member = await game.get_member_safe(guild, member_id)  # type: ignore[arg-type]
        if member:
            cached[member_id] = member.display_name
            updated = True
    if (
        updated
        and getattr(game, "in_progress", False)
        and not getattr(game, "ending", False)
    ):
        try:
            await game.persist_flush()
        except Exception:
            pass
    return format_player(game, member_id)


# --- Phase ---
def day_header(n: int) -> str:
    return f"Day {n}"


def night_header(n: int) -> str:
    return f"Night {n}"


def living_count(n: int) -> str:
    return f"Remaining players: {n}"


def game_begun() -> str:
    return "The game has begun. Roles have been assigned."


def night_submit_hint() -> str:
    return "Submit actions in DMs or your private channel. The GM runs !resolve when ready."


def peaceful_night() -> str:
    return PEACEFUL_NIGHT


def ga_protected(name: str) -> str:
    return f"The Guardian Angel has protected {name}."


# --- Death staged pipeline ---
def death_notice(cause: str, name: str) -> str:
    if cause == "lynch":
        return f"{name} died."
    if cause in ("deputy_shoot", "deputy_friendly_fire", "deputy_friendly_fire_self"):
        return f"{name} died."
    return f"{name} died last night."


def death_cause_line(tag: str) -> Optional[str]:
    # Wiki lines are verbatim values in CAUSE_LINE_BY_TAG (e.g. "They were killed by …").
    return CAUSE_LINE_BY_TAG.get(tag)


def will_not_found() -> str:
    return "We could not find a last will."


def will_found() -> str:
    return "We found a will next to their body."


def will_unreadable_blood() -> str:
    return "Their last will was covered in blood and unreadable."


def role_was(name: str, role: str) -> str:
    return f"{name}'s role was {role}."


def role_unknown(name: str) -> str:
    return f"We could not determine {name}'s role."


# --- Action acks ---
def action_decided(action: str, target: str) -> str:
    return f"You have decided to {action} {target} tonight."


def action_instead(action: str, target: str) -> str:
    return f"You will instead {action} {target} tonight."


def action_changed_mind() -> str:
    return "You have changed your mind."


def action_resolving() -> str:
    return "Night is resolving. Your action cannot be changed right now."


def action_plunder_locked() -> str:
    return "Your plunder duel is already in progress. You can't change actions until it finishes."


# --- Wills ---
def will_saved() -> str:
    return "Your last will has been saved."


def will_cleared() -> str:
    return "Your last will has been cleared."


def will_dead() -> str:
    return "You cannot edit your will while dead."


def will_dm_only() -> str:
    return "Use !will here in DMs or your private channel."


# --- Tribunal ---
def trials_remaining(n: int) -> str:
    return f"There are {n} possible trial(s) remaining today."


def verdict_lynch(defendant: str, guilty: int, innocent: int) -> str:
    return f"The Town has decided to lynch {defendant} by a vote of {guilty} to {innocent}."


def verdict_pardon(defendant: str, innocent: int, guilty: int) -> str:
    return f"The Town has decided to pardon {defendant} by a vote of {innocent} to {guilty}."


def judgment_voted_guilty(name: str) -> str:
    return f"{name} voted guilty."


def judgment_voted_innocent(name: str) -> str:
    return f"{name} voted innocent."


def judgment_abstained(name: str) -> str:
    return f"{name} abstained."


def last_words_open(defendant: str, seconds: int = 20) -> str:
    return f"Last Words! {defendant} has {seconds} seconds to speak."


def execution_concluded() -> str:
    return "The execution is concluded. The sun sets early tonight..."


def mayor_double_vote_note() -> str:
    return "(The Mayor's revealed vote counted double!)"


def jester_revenge_grave() -> str:
    return "The jester will get his revenge from the grave!"


def exe_lynch_success(exe_mention: str) -> str:
    return f"You have successfully gotten your target lynched! {exe_mention}"


def tribunal_stand_open(defendant_mention: str) -> str:
    return (
        f"🚨 **{defendant_mention} has been voted to the stand!** 🚨\n"
        "The town is now muted. You have **45 seconds** to defend yourself."
    )


def nomination_recap(voters: str, defendant: str) -> str:
    return f"Nomination: {voters} voted against {defendant}."


# --- Whispers ---
def whisper_public_meta(sender: str, recipient: str) -> str:
    return f"{sender} is whispering to {recipient}."


def whisper_to_sender(target: str, message: str) -> str:
    return f"To {target}: {message}"


def whisper_from_recipient(sender: str, message: str) -> str:
    return f"From {sender}: {message}"


def whisper_self() -> str:
    return "You cannot whisper yourself, that would be weird."


def whisper_mayor_revealed() -> str:
    return "You can't whisper once you have revealed as the Mayor!"


def whisper_to_revealed_mayor() -> str:
    return "You can't whisper to a revealed Mayor."


def whisper_not_day() -> str:
    return "You cannot whisper right now."


def whisper_ignoring() -> str:
    return "You are ignoring that player."


def whisper_private_channel_failed() -> str:
    return "🛑 Could not deliver your whisper — check that your private channel is configured."


def whisper_public_delivery_failed() -> str:
    return "🛑 Your whisper was delivered privately, but the town could not be notified in the game channel."


# --- Role night DMs (Spec 7) ---
def hypnotist_fake_healed() -> str:
    return doctor_healed()


def hypnotist_fake_attacked() -> str:
    return attacked_survived()


def hypnotist_fake_controlled() -> str:
    return "🧙 You felt a strange force take hold of you... You were **controlled** tonight."


def transported() -> str:
    return "You were transported to another location!"


def roleblocked() -> str:
    return "Someone role blocked you, so you could not act!"


def witch_controlled() -> str:
    return hypnotist_fake_controlled()


def witch_control_resisted() -> str:
    return "Your target resisted your magic tonight."


def witch_control_pawn_blocked() -> str:
    return "Your puppet was stopped before they could carry out your command."


def witch_cannot_redirect_self_target() -> str:
    return "You cannot redirect that player's self-targeted ability."


def sheriff_innocent() -> str:
    return "You cannot find evidence of wrongdoing. Your target is innocent or great at hiding secrets!"


def sheriff_suspicious() -> str:
    return "Your target is suspicious or framed!"


def investigator_result(display_name: str, roles: list[str]) -> str:
    role_list = ", ".join(f"**{r}**" for r in roles)
    return f"Your investigation found clues that **{display_name}** could be: {role_list}."


def lookout_none() -> str:
    return "Nobody visited your target."


def lookout_visitors(names: str) -> str:
    return f"Your target was visited by: {names}"


def lookout_too_many() -> str:
    return "More people visited your target than you could identify."


def lookout_watch_interrupted() -> str:
    return "You were roleblocked — you could not watch anyone tonight."


def tracker_no_visit() -> str:
    return "Your target did not visit anyone!"


def tracker_visit(name: str) -> str:
    return f"Your target visited {name}!"


def tracker_track_interrupted() -> str:
    return "You were roleblocked — you could not track anyone tonight."


def investigate_interrupted() -> str:
    return "You were roleblocked — you could not investigate anyone tonight."


def tracker_visit_multiple(formatted_names: str) -> str:
    return f"Your target visited multiple players tonight: {formatted_names}."


def doctor_target_attacked() -> str:
    return "Your target was attacked last night!"


def doctor_healed() -> str:
    return "You were attacked but someone nursed you back to health!"


def doctor_unstoppable() -> str:
    return "Your target was killed by an unstoppable attack!"


def doctor_heal_unstoppable_no_effect() -> str:
    return "Your target was killed by an unstoppable force — your heal had no effect."


def doctor_heal_redundant() -> str:
    return "Your target was already healed by another Doctor tonight — your heal had no effect."


def defense_too_strong() -> str:
    return "Your target's defense was too strong to kill!"


def attacked_survived() -> str:
    return "You were attacked but survived!"


def vest_survived_attack() -> str:
    return "You were attacked, but your bulletproof vest stopped the blow!"


def alert_survived_attack() -> str:
    return "You were attacked while on alert, but your defense held!"


def scary_grandma_alert_visitor_survived() -> str:
    return (
        "Someone visited you while you were on alert, but your shot did not pierce their defense — "
        "they survived the night."
    )


def mafia_attacked_you() -> str:
    return "You were attacked by a member of the Mafia!"


def sk_attacked_you() -> str:
    return "You were attacked by a Serial Killer!"


def sk_rb_counter() -> str:
    return "Someone role blocked you, so you attacked them!"


def arso_doused() -> str:
    return "You were doused in gas!"


def arso_ignited() -> str:
    return "You were set on fire by an Arsonist!"


def arso_cleaned() -> str:
    return "You have cleaned the gasoline off of yourself."


def psychic_rb() -> str:
    return "You were roleblocked and did not receive a vision!"


def psychic_vision_evil(a: str, b: str, c: str) -> str:
    return f"A vision revealed that {a}, {b} or {c} is evil!"


def psychic_vision_good(a: str, b: str) -> str:
    return f"A vision revealed that {a} or {b} is good!"


def psychic_too_small() -> str:
    return "The town is too small to accurately find an evildoer!"


def psychic_too_evil() -> str:
    return "The town is too evil to find anyone good!"


def retri_corpse_missing() -> str:
    return "The corpse you targeted is missing!"


def gatekeeper_turned_away() -> str:
    return "You were turned away at the gate last night!"


def gatekeeper_blocked_visitor() -> str:
    return "Someone was turned away from the house you guarded last night!"


def chaos_touch() -> str:
    return "You felt an unsettling chaos take hold of you tonight. You cannot tell what was set in motion."


def pirate_attacked() -> str:
    return "You were attacked by a Pirate!"


def exe_convert_jester() -> str:
    return "Your target has died. You have failed your goal and become a Jester."


def jester_lynch_dm() -> str:
    return (
        "You have been successfully lynched! You win!\n"
        "Now, choose one of the players who voted **guilty** or **abstained** to take to the grave.\n"
        "Use `!haunt <number>` in this DM to enact your revenge."
    )


def jester_lynch_dm_voter_list(voter_lines: str) -> str:
    return f"Eligible voters:\n{voter_lines}"


# --- Ammo (Spec 4b) ---
def ammo_vigilante(n: int) -> str:
    return f"You have {n} bullet(s) left."


def bodyguard_rb_no_protect() -> str:
    return "You were roleblocked — your protect had no effect and you kept your charge."


def bodyguard_fought_off() -> str:
    return "You fought off an attacker while guarding your target!"


def ammo_bodyguard(n: int) -> str:
    return f"You have {n} bulletproof vest(s) left."


def ammo_doctor(n: int) -> str:
    return f"You have {n} self heal(s) left."


def ammo_survivor(n: int) -> str:
    return f"You have {n} bulletproof vest(s) left."


def ammo_ga(n: int) -> str:
    return f"You have {n} protections left."


def ammo_gatekeeper(n: int) -> str:
    return f"You have {n} seal(s) left."


def ammo_tailor(n: int) -> str:
    w = "Tailor" if n == 1 else "Tailories"
    return f"You have {n} {w} left."


def ammo_gravedigger(n: int) -> str:
    return f"You have {n} cleaning(s) left."


def ammo_grandma(n: int) -> str:
    return f"You have {n} alert(s) left."


def ammo_retri(n: int) -> str:
    return f"You have {n} corpse reanimation(s) left."


def ammo_chaos(n: int) -> str:
    return f"You have {n} trick(s) left."


# --- Custom (Spec 8) ---
def deputy_ammo() -> str:
    return "You have 1 bullet left for your revolver."


def deputy_day_prompt(day_number: int) -> str:
    return (
        f"☀️ **Day {day_number}** — you may use your Deputy revolver today.\n"
        "`!shoot <slot>` in your private channel or DM (**1 bullet** for the game; "
        "each Deputy may fire once per day until spent)."
    )


def hypnotize_ack(msg_type: str, target_name: str) -> str:
    return f"Sending fake '{msg_type}' message to **{target_name}**."


def win_pirate_named(mention: str) -> str:
    return f"⚔️ **The Pirate, {mention}, has successfully plundered their way to victory!**"


def win_pirate_anon() -> str:
    return "⚔️ **The Pirate has successfully plundered their way to victory!**"


def win_executioner_named(mention: str) -> str:
    return f"⚖️ **The Executioner, {mention}, has achieved their goal and wins!**"


def win_executioner_anon() -> str:
    return "⚖️ **The Executioner has achieved their goal and wins!**"


def win_survivor_named(mention: str) -> str:
    return f"Congratulations to the Survivor, {mention}, for making it to the end!"


def win_survivor_anon() -> str:
    return "Congratulations to the Survivor for making it to the end!"


def win_witch_named(mention: str) -> str:
    return (
        f"Congratulations to the Witch, {mention}, for surviving the fall of Town — "
        f"winning with the **Mafia**, **Arsonist**, or **Serial Killer**!"
    )


def win_guardian_angel_named(mention: str) -> str:
    return (
        f"Congratulations to the Guardian Angel, {mention}, for protecting your bind — "
        f"joint victory with the winning side!"
    )


def win_guardian_angel_anon() -> str:
    return (
        "Congratulations to the Guardian Angel for protecting their bind — "
        "joint victory with the winning side!"
    )


def win_chaos_named(mention: str) -> str:
    return f"Congratulations to Chaos, {mention}, for surviving the madness!"


def win_chaos_anon() -> str:
    return "Congratulations to Chaos for surviving the madness!"


def win_jester_named(mention: str) -> str:
    return f"🎭 **The Jester, {mention}, has gotten their revenge from the grave!**"


def win_jester_anon() -> str:
    return "🎭 **The Jester has gotten their revenge from the grave!**"


def win_draw_all_dead() -> str:
    return "💀 **GAME OVER! Everyone has died. It's a DRAW!**"


def win_draw_bloodless_stalemate(cycles: int) -> str:
    return (
        f"⏳ **GAME OVER!** No one died for **{cycles}** consecutive days and nights. "
        "The town stagnates — it's a **DRAW**."
    )


def win_personal_victories(labels: str) -> str:
    return f"🏆 **Personal victories:** {labels}"


def win_arsonist_named(mention: str) -> str:
    return f"🔥 **GAME OVER! The Arsonist, {mention}, has won!**"


def win_arsonist_anon() -> str:
    return "🔥 **GAME OVER! The Arsonist has won!**"


def win_serial_killer_named(mention: str) -> str:
    return f"🔪 **GAME OVER! The Serial Killer, {mention}, has won!**"


def win_serial_killer_anon() -> str:
    return "🔪 **GAME OVER! The Serial Killer has won!**"


def win_faction(faction: str) -> str:
    return f"🎉 **GAME OVER! The {faction} has won!**"


def deputy_public_kill(target: str) -> str:
    return f"A Deputy fired their revolver at {target} — they fall dead."


def deputy_public_defense(target: str) -> str:
    return f"A Deputy fired their revolver at {target} — the shot was absorbed by basic defense!"


def deputy_public_mistake(target: str) -> str:
    return f"A Deputy fired at {target} — a tragic mistake. Both the innocent and the Deputy are dead."


def deputy_shot_absorbed() -> str:
    return "Your shot failed to pierce your target's defense."


def deputy_shot_mark() -> str:
    return "Your shot found its mark."


def deputy_shot_mistake() -> str:
    return "Your shot killed an innocent. The guilt is unbearable."


def vig_guilt_private_warning() -> str:
    return "Your shot killed a member of the Town. The guilt is unbearable — you will die tomorrow night."


# Haunt / guilt line 1 customs
def haunt_spirit_line(name: str) -> str:
    return f"The Jester's spirit has claimed its revenge! {name} was found dead."


def guilt_suicide_line(name: str) -> str:
    return f"Overcome with guilt, {name} took their own life."


# --- Leaver / rehydrate ---
def player_left_presumed_dead(mention: str, role: str) -> str:
    return f"{mention} left the server and is presumed dead. Their role was **{role}**."


# --- Serial Killer / Escort ---
def sk_roleblock_immune() -> str:
    return (
        "🔪 Someone tried to roleblock you, but you are **immune**. "
        "They won't stop you tonight."
    )


# --- Guardian Angel ward ---
def ga_ward_rb_no_charge() -> str:
    return "You were roleblocked — your ward had no effect and you kept your charge."


def ga_ward_no_charge() -> str:
    return "Your ward failed — you had no charge remaining."


def ga_ward_defeated() -> str:
    return "You can no longer protect your bind — your Guardian Angel purpose has failed."


def ga_ward_applied() -> str:
    return (
        "Your ward took hold: your bind was cleared of any douse and is shielded "
        "from kills and ignites tonight."
    )


def ga_ward_received() -> str:
    return (
        "🪽 A Guardian Angel protected you tonight — you were cleared of douse "
        "and shielded from harm."
    )


def ga_ward_blocked_attacker() -> str:
    return (
        "🪽 Your target was shielded by a **Guardian Angel** — you could not cut through the ward."
    )


def ga_ward_survived_attack() -> str:
    return (
        "🪽 Your **Guardian Angel** ward held — an attack against you tonight failed to pierce the shield."
    )


# --- Arsonist ---
def arso_smell_gasoline() -> str:
    return "⛽ **You smell gasoline...**"


def arso_ignited_through_alert() -> str:
    return "One of your victims was on alert, but your ignition burned through their defense."


def arso_ignited_through_defense() -> str:
    return (
        "One of your victims had extra defense tonight (alert, vest, or heal), "
        "but your unstoppable ignition burned through it."
    )


# --- Seer ---
def seer_gaze_interrupted() -> str:
    return "Your gaze was interrupted — you could not read the stars tonight."


def seer_gaze_mayor_blocked() -> str:
    return "You cannot gaze upon a revealed Mayor."


def seer_gaze_duplicate_pair() -> str:
    return "You have already gazed on that pair of players."


def seer_gaze_friends() -> str:
    return "Your gaze reveals these two players are **Friends** — their fates feel aligned."


def seer_gaze_enemies() -> str:
    return "Your gaze reveals these two players are **Enemies** — their paths diverge."


def witch_stolen_gaze(vision: str) -> str:
    return f"Stolen gaze: your victim's mind revealed that... {vision}"


# --- Bodyguard ---
def bodyguard_someone_protected_you() -> str:
    return "Someone protected you!"


def bodyguard_other_bg_first() -> str:
    return "Someone else protected your target first. You did not engage an attacker tonight."


def pirate_bg_duel_blocked() -> str:
    return (
        "You won your duel, but a Bodyguard killed you before you could finish the plunder."
    )


def killed_by_bodyguard() -> str:
    return "You were killed by a Bodyguard."


# --- Night 1 shields ---
def witch_night1_shield() -> str:
    return "🛡️ Your mystical barrier protected you from an attack!"


def neutral_night1_shield() -> str:
    return "🛡️ Your Night 1 protection absorbed the attack — you survive this strike!"


# --- Pirate ---
def pirate_plunder_blocked() -> str:
    return "You won your duel, but something blocked your visit. The plunder was unsuccessful."


# --- Psychic (deliver_psychic_visions) ---
def psychic_spirits_silent() -> str:
    return "The spirits were silent — no evil presence could be found among the living."


def psychic_too_faint_three() -> str:
    return "The spirits were too faint to name three distinct souls tonight."


def psychic_too_faint_two() -> str:
    return "The spirits were too faint to name two distinct souls tonight."


def psychic_too_small_night() -> str:
    return "The town is too small to accurately read the spirits tonight."


def psychic_vision_evil_slots(slot_a: str, slot_b: str, slot_c: str) -> str:
    return (
        f"Psychic Vision: A vision revealed that Slot {slot_a}, Slot {slot_b}, "
        f"or Slot {slot_c} is evil!"
    )


def psychic_vision_good_slots(slot_a: str, slot_b: str) -> str:
    return f"Psychic Vision: A vision revealed that Slot {slot_a} or Slot {slot_b} is good!"


def psychic_stolen_useless() -> str:
    return "Stolen vision: your victim's mind was too chaotic to reveal anything useful."


def psychic_stolen_prefix(vision: str) -> str:
    return f"Stolen vision: your victim's mind revealed that... {vision}"


# --- Witch control (resolve_control / finalize) ---
def witch_forced_target(controlled_name: str, target_name: str) -> str:
    return f"You successfully forced {controlled_name} to target {target_name}."


def witch_psychic_bent(controlled_name: str) -> str:
    return (
        f"You bent {controlled_name}'s mind — you will receive their Psychic vision tonight."
    )


def witch_prevented_ignite(controlled_name: str, target_name: str) -> str:
    return (
        f"You successfully prevented an ignite and forced {controlled_name} "
        f"to douse {target_name}."
    )


def witch_no_redirectable_action(controlled_name: str) -> str:
    return f"You attempted to control {controlled_name}, but they had no redirectable action."
