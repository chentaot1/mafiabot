from __future__ import annotations

import logging
from datetime import datetime

import discord

from . import actions, duels, trials, reports, state as st

NO_MENTIONS = discord.AllowedMentions.none()


def player_label(game, uid):
    member = next((p for p in game.players if p.id == uid), None)
    name = getattr(member, "display_name", str(uid))
    return f"{game.player_slots.get(uid, '?')}: {discord.utils.escape_markdown(discord.utils.escape_mentions(name))}"


async def private_reply(interaction, text, *, view=None):
    # Components v2 messages cannot contain ordinary content or embeds.
    payload = {'view':view} if isinstance(view,discord.ui.LayoutView) else {'content':text}
    if not interaction.response.is_done():
        await interaction.response.send_message(**payload, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.followup.send(**payload, ephemeral=True, allowed_mentions=NO_MENTIONS)


class Button(discord.ui.Button):
    def __init__(self, label, custom_id, callback, *, disabled=False, style=discord.ButtonStyle.secondary):
        super().__init__(label=label[:80], custom_id=custom_id, disabled=disabled, style=style)
        self.handler = callback

    async def callback(self, interaction):
        # Modal entry points acknowledge by opening the modal themselves.
        try:
            await self.handler(interaction)
        except st.Rejected as error:
            await private_reply(interaction, str(error))
        except Exception as error:
            logging.error("Gameplay control failed: %s", type(error).__name__)
            await private_reply(interaction, "The operation could not be saved. Reopen /actions and try again.")


class Picker(discord.ui.Select):
    def __init__(self, placeholder, custom_id, options, callback):
        super().__init__(placeholder=placeholder[:150], custom_id=custom_id, options=options)
        self.handler = callback

    async def callback(self, interaction):
        try:
            await self.handler(interaction, self.values[0])
        except st.Rejected as error:
            await private_reply(interaction, str(error))
        except Exception as error:
            logging.error("Gameplay picker failed: %s", type(error).__name__)
            await private_reply(interaction, "The operation could not be saved. Please try again.")


class PrivateView(discord.ui.LayoutView):
    def __init__(self, controller, game, uid, *, persistent=False, expected=None):
        super().__init__(timeout=None if persistent else 900)
        self.controller, self.game, self.uid = controller, game, uid
        self.expected = expected or st.identity(game)
        self.phase = game.phase

    async def interaction_check(self, interaction):
        try:
            if interaction.user.id != self.uid:
                raise st.Rejected("These controls belong to another player.")
            st.require_current(self.game, phase=self.phase, expected=self.expected)
            if self.game.resolving:
                raise st.Rejected("Resolution is in progress; submissions have closed.")
        except st.Rejected as error:
            await private_reply(interaction, str(error))
            return False
        return True


def trial_text(game, trial, page=0):
    stage = trial["stage"]
    if stage == 'cancelled':
        return '## Trial cancelled\n' + trial.get('reason', 'This trial is no longer usable.')
    if stage == "nomination":
        totals = trials.nomination_tally(game, trial)
        lines = ["## Nominations", f"Change your nomination until <t:{int(datetime.fromisoformat(trial['deadline']).timestamp())}:R>."]
        for p in game.ordered_living_players()[page*20:(page+1)*20]:
            target = trial["nominations"].get(str(p.id))
            lines.append(f"{player_label(game, p.id)} → {player_label(game, target) if target in totals else 'Abstain'}")
        lines.append("**Weighted totals:** " + "; ".join(f"{game.player_slots.get(uid, '?')}: {n}" for uid, n in totals.items()))
        return "\n".join(lines)
    if stage in {"defense", "judgment"}:
        deadline = int(datetime.fromisoformat(trial["deadline"]).timestamp())
        return (f"## {'Defense' if stage == 'defense' else 'Judgment'}\nDefendant: **{player_label(game, trial['defendant'])}**.\n"
                f"Ends <t:{deadline}:R>.\n" + ("The defendant has 45 seconds to speak." if stage == "defense" else
                "Living players other than the defendant may change their private choice until closing. Missing votes count as Abstain."))
    result = trial.get("result")
    if result:
        lines = ["## Judgment closed", f"Defendant: **{player_label(game, trial['defendant'])}**"]
        for uid, choice in list(result["choices"].items())[page*20:(page+1)*20]:
            lines.append(f"{player_label(game, int(uid))}: **{choice.title()}** (weight {result['weights'][uid]})")
        lines.extend([" · ".join(f"{c.title()}: {result['totals'][c]}" for c in trials.JUDGMENTS),
                      "**Guilty**" if result["guilty"] else "**Innocent**"])
        return "\n".join(lines)
    return "## Trial closed\n" + trial.get("reason", "Voting has closed.")


class TrialView(discord.ui.LayoutView):
    def __init__(self, controller, game, trial, *, archived=False):
        super().__init__(timeout=None)
        self.controller, self.game, self.token = controller, game, trial["id"]
        count = len(trial['result']['choices']) if trial.get('result') else len(game.living_players)
        page = min(trial.get('display_page', 0), max(0, (count-1)//20))
        box = discord.ui.Container(discord.ui.TextDisplay(trial_text(game, trial, page)))
        self.add_item(box)
        if count > 20 and (trial['stage'] == 'nomination' or trial.get('result')):
            async def move(i, delta):
                await i.response.defer(ephemeral=True)
                def update():
                    controller.trial_record(game, self.token, archived=archived)['display_page'] = page+delta
                await st.commit(game, update)
                await controller.render_trial(game, token=self.token, archived=archived)
            box.add_item(discord.ui.ActionRow(
                Button('Previous page', f'mf:{self.token}:prev', lambda i: move(i, -1), disabled=page==0),
                Button('Next page', f'mf:{self.token}:next', lambda i: move(i, 1), disabled=(page+1)*20>=count)))
        if trial["stage"] == "nomination":
            async def nominate(i):
                await i.response.defer(ephemeral=True)
                current=st.session(game,self.token,phase='day')
                if current['stage']!='nomination' or st.remaining(current['deadline'])<=0:
                    raise st.Rejected('Nominations have closed.')
                if i.user.id not in {p.id for p in game.living_players}:
                    raise st.Rejected('Only living players can nominate.')
                await private_reply(i, "Choose a living player.", view=NominationPicker(controller, game, self.token, i.user.id))
            async def abstain(i):
                await i.response.defer(ephemeral=True)
                await trials.cast(game, self.token, i.user.id, None, "nomination", guild=controller.guild(game))
                await private_reply(i, "Saved Abstain. You may change it until closing.")
                controller.refresh_trial(game)
            box.add_item(discord.ui.ActionRow(Button("Nominate", f"mf:{self.token}:nom", nominate),
                                              Button("Abstain", f"mf:{self.token}:na", abstain)))
        if trial["stage"] == "judgment":
            buttons = []
            for choice in trials.JUDGMENTS:
                async def vote(i, choice=choice):
                    await i.response.defer(ephemeral=True)
                    await trials.cast(game, self.token, i.user.id, choice, "judgment", guild=controller.guild(game))
                    await private_reply(i, f"Saved {choice.title()}. You may change it until closing.")
                buttons.append(Button(choice.title(), f"mf:{self.token}:{choice}", vote))
            box.add_item(discord.ui.ActionRow(*buttons))


class NominationPicker(discord.ui.LayoutView):
    def __init__(self, controller, game, token, uid, page=0):
        super().__init__(timeout=600)
        self.uid, self.game, self.token = uid, game, token
        members = [p for p in game.ordered_living_players() if p.id != uid
                   and game.role_states.get(p.id, {}).get('ga_trial_lock_day') != game.day_number]
        box = discord.ui.Container(discord.ui.TextDisplay("Select a nominee. Selecting submits your nomination."))
        self.add_item(box)
        if members:
            options = [discord.SelectOption(label=player_label(game, p.id)[:100], value=str(p.id)) for p in members[page*25:(page+1)*25]]
            async def picked(i, value):
                await i.response.defer(ephemeral=True)
                await trials.cast(game, token, uid, int(value), "nomination", guild=controller.guild(game))
                await private_reply(i, "Nomination saved. You may change it until closing.")
                controller.refresh_trial(game)
            box.add_item(discord.ui.ActionRow(Picker("Nominee", f"mf:{token}:pick:{uid}:{page}", options, picked)))
            if len(members) > 25:
                async def move(i, delta):
                    await i.response.edit_message(view=NominationPicker(controller, game, token, uid, page+delta), allowed_mentions=NO_MENTIONS)
                box.add_item(discord.ui.ActionRow(
                    Button("Previous", f"mf:{token}:np:{uid}:{page}", lambda i: move(i, -1), disabled=page==0),
                    Button("Next", f"mf:{token}:nn:{uid}:{page}", lambda i: move(i, 1), disabled=(page+1)*25>=len(members))))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.uid:
            await private_reply(interaction, "This picker belongs to another player.")
            return False
        try:
            current=st.session(self.game,self.token,phase='day')
            if current['stage']!='nomination' or st.remaining(current['deadline'])<=0:
                raise st.Rejected('Nominations have closed.')
            if self.uid not in {p.id for p in self.game.living_players}:
                raise st.Rejected('Only living players can nominate.')
        except st.Rejected as error:
            await private_reply(interaction,str(error))
            return False
        return True


def report_text(game, entry):
    lines = [f"## Night {entry['night']} · {entry['kind'].title()}"]
    selected = entry.get('selected', [])
    if isinstance(selected, list) and selected:
        lines.append('**Chosen pair:** ' + ', '.join(player_label(game, uid)[:100] for uid in selected))
    lines.append(entry['text'])
    if selected:
        lines.append('Transport/control may change this comparison.')
    return '\n'.join(lines)


class ReportCard(discord.ui.LayoutView):
    def __init__(self, game, entry):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(report_text(game, entry))))


def add_history(view, box):
    entries = reports.history(view.game, view.uid)
    if not entries:
        return
    box.add_item(discord.ui.TextDisplay(report_text(view.game, entries[-1])))
    async def open_history(i):
        await i.response.defer(ephemeral=True)
        await private_reply(i, 'Your private reports',
            view=ReportHistory(view.controller, view.game, view.uid, expected=view.expected))
    box.add_item(discord.ui.ActionRow(Button('Report history', f'mf:{view.expected[2]}:{view.uid}:reports', open_history)))


class ReportHistory(PrivateView):
    def __init__(self, controller, game, uid, *, page=0, persistent=False, expected=None):
        super().__init__(controller, game, uid, persistent=persistent, expected=expected)
        entries = list(reversed(reports.history(game, uid)))
        page = min(max(0, page), max(0, (len(entries)-1)//3))
        box = discord.ui.Container(discord.ui.TextDisplay('## Your private report history'))
        self.add_item(box)
        for entry in entries[page*3:(page+1)*3]:
            box.add_item(discord.ui.TextDisplay(report_text(game, entry)))
        if not entries:
            box.add_item(discord.ui.TextDisplay('No reports have been recorded in this match.'))
        if len(entries) > 3:
            async def move(i, delta):
                await i.response.edit_message(view=ReportHistory(controller, game, uid, page=page+delta,
                    persistent=persistent, expected=self.expected), allowed_mentions=NO_MENTIONS)
            box.add_item(discord.ui.ActionRow(
                Button('Previous', 'reports:prev', lambda i: move(i, -1), disabled=page==0),
                Button('Next', 'reports:next', lambda i: move(i, 1), disabled=(page+1)*3>=len(entries))))


def guardian_target(game, uid):
    target = game.role_states.get(uid, {}).get('ga_target_id')
    return target if isinstance(target, int) and not isinstance(target, bool) else None


def guardian_details(game, uid):
    state = game.role_states.get(uid, {})
    bind = guardian_target(game, uid)
    if bind is None:
        return f"**Bound player:** Unassigned\n**Wards remaining:** {state.get('ga_ward_charges', 0)}\nA valid bound player is required to ward."
    living = {p.id for p in game.living_players}
    lines = [f"**Bound player:** {player_label(game, bind)} · {'Alive' if bind in living else 'Dead or departed'}",
             f"**Wards remaining:** {state.get('ga_ward_charges', 0)}"]
    if state.get('ga_defeated'):
        lines.append('Your bound player has fallen; wards are unavailable.')
    elif uid not in living:
        lines.append('You can still ward your living bound player at night. A dead Angel cannot win.')
    if bind in living:
        target_state = game.role_states.get(bind, {})
        if target_state.get('ga_shield_active_tonight') and game.phase == 'night':
            lines.append('Night protection is active.')
        if game.phase == 'day' and target_state.get('ga_trial_lock_day') == game.day_number:
            lines.append('Your bound player is protected from nomination today.')
    return '\n'.join(lines)


class DayPanel(PrivateView):
    def __init__(self, controller, game, uid, *, persistent=True, expected=None):
        super().__init__(controller, game, uid, persistent=persistent, expected=expected)
        role = game.player_roles.get(uid)
        state = game.role_states.get(uid, {})
        box = discord.ui.Container(discord.ui.TextDisplay(f'## Day {game.day_number} · {role}'))
        self.add_item(box)
        if role == 'Deputy':
            alive = uid in {p.id for p in game.living_players}
            available = alive and game.day_number >= 2 and state.get('deputy_shots_remaining', 0) > 0 and not game.deputy_fired_today(uid)
            box.add_item(discord.ui.TextDisplay(f"**Bullets remaining:** {state.get('deputy_shots_remaining', 0)}\n"
                'You can fire from Day 2, outside the Tribunal. Shooting an innocent player kills you from guilt.'))
            async def prepare(i):
                await i.response.defer(ephemeral=True)
                await private_reply(i, 'Choose a target', view=DeputyDraft(controller, game, uid, expected=self.expected))
            box.add_item(discord.ui.ActionRow(Button('Prepare shot', f'mf:{self.expected[2]}:{uid}:shot', prepare,
                disabled=not available or game.vote_in_progress)))
            if not alive:
                box.add_item(discord.ui.TextDisplay('You are dead and cannot fire.'))
            elif game.vote_in_progress:
                box.add_item(discord.ui.TextDisplay('Shooting is paused during the Tribunal.'))
        elif role == 'Guardian Angel':
            box.add_item(discord.ui.TextDisplay(guardian_details(game, uid)))
        elif role == 'Psychic':
            box.add_item(discord.ui.TextDisplay('Your visions arrive automatically after night resolution.'))
        elif role == 'Seer':
            box.add_item(discord.ui.TextDisplay('Choose a new pair during the next night. Reports follow the existing transport and control rules.'))
        elif role == 'Serial Killer':
            box.add_item(discord.ui.TextDisplay('**Mode:** ' + ('Cautious' if state.get('sk_cautious') else 'Aggressive') + '\nReopen at night to set your mode and stab target.'))
        add_history(self, box)


class NightPanel(PrivateView):
    def __init__(self, controller, game, uid, *, persistent=True, expected=None):
        super().__init__(controller, game, uid, persistent=persistent, expected=expected)
        state = game.role_states.get(uid, {})
        labels = {'ga_ward_charges': 'Wards remaining', 'deputy_shots_remaining': 'Bullets remaining'}
        resources = ", ".join(f"{labels.get(k, k.replace('_', ' '))}: {v}" for k, v in state.items()
                              if k.endswith("remaining") or k in {"wins", "can_haunt", "ga_ward_charges"}) or "No limited resources."
        current = game.night_actions.get(uid)
        if state.get('haunt_target') is not None:
            current={'type':'haunt','target':state['haunt_target']}
        submitted = "None"
        if current:
            submitted = actions.ability_label(current["type"]) + " " + ", ".join(player_label(game, x) for x in current.get("targets", [current['target']] if current.get('target') else []))
            if current.get('corpse_player_id') is not None:
                submitted += f" · corpse {player_label(game,current['corpse_player_id'])} ({current.get('corpse_role','Unknown')})"
            if current.get('msg_type'):
                submitted += f" · message: {current['msg_type']}"
            if current.get('fake_role') is not None:
                submitted += f" · disguise: {discord.utils.escape_mentions(current['fake_role'])}"
            if current['type']=='plunder':
                submitted += ' · duel complete' if current.get('duel_finished') else ' · duel in progress'
        role = game.player_roles.get(uid)
        instructions = {'Guardian Angel': 'Prepare your ward, review the bound player, then press Submit.',
                        'Psychic': 'Your ability is passive. Reports arrive privately after resolution.',
                        'Deputy': 'Your revolver is a daytime ability.'}.get(role,
                        'Choose an ability, draft its choices, then press Submit.')
        box = discord.ui.Container(discord.ui.TextDisplay(
            f"## Night {game.day_number} · {role or 'Unknown'}\n{resources}\n**Submitted:** {submitted}\n{instructions}"))
        self.add_item(box)
        available = actions.abilities_for(game, uid)
        if role == 'Serial Killer':
            cautious = bool(state.get('sk_cautious'))
            box.add_item(discord.ui.TextDisplay('**Mode:** ' + ('Cautious' if cautious else 'Aggressive') +
                '\nAggressive counters Escort/Consort roleblocks. Changing mode keeps your stab target.'))
            async def set_mode(i, mode):
                await i.response.defer(ephemeral=True)
                result = await actions.submit(game, uid, 'cautious', cautious_mode=mode,
                    expected=self.expected, guild=controller.guild(game))
                await private_reply(i, result.message)
                if result.accepted:
                    controller.after_submission(game, uid, result.action)
                    await i.edit_original_response(view=NightPanel(controller, game, uid, persistent=self.timeout is None,
                        expected=self.expected), allowed_mentions=NO_MENTIONS)
            box.add_item(discord.ui.ActionRow(
                Button('Cautious', f'mf:{self.expected[2]}:{uid}:cautious', lambda i: set_mode(i, True), disabled=cautious),
                Button('Aggressive', f'mf:{self.expected[2]}:{uid}:aggressive', lambda i: set_mode(i, False), disabled=not cautious)))
            available = [a for a in available if a != 'cautious']
        if role == 'Guardian Angel':
            box.add_item(discord.ui.TextDisplay(guardian_details(game, uid)))
            bind = guardian_target(game, uid)
            async def ward(i):
                await i.response.defer(ephemeral=True)
                await private_reply(i, 'Review your ward', view=Draft(controller, game, uid, 'ward', expected=self.expected))
            box.add_item(discord.ui.ActionRow(Button(f'Ward {player_label(game, bind)}' if bind is not None else 'Ward unavailable', f'mf:{self.expected[2]}:{uid}:ward', ward,
                disabled='ward' not in available or bind not in {p.id for p in game.living_players})))
            available = [a for a in available if a != 'ward']
        if available:
            async def choose(i, ability):
                await i.response.defer(ephemeral=True)
                await private_reply(i, "Draft your action below.", view=Draft(controller, game, uid, ability, expected=self.expected))
            box.add_item(discord.ui.ActionRow(Picker("Ability", f"mf:{self.expected[2]}:{uid}:ability",
                         [discord.SelectOption(label=actions.ability_label(a), value=a) for a in available], choose)))
        elif role == 'Psychic':
            box.add_item(discord.ui.TextDisplay('Your ability is passive. A private vision arrives after resolution: '
                + ('one evil among three seats tonight.' if game.day_number % 2 else 'a good vision tonight.') + '\nRoleblocks and Witch control follow the existing rules.'))
        elif role == 'Deputy':
            box.add_item(discord.ui.TextDisplay('Your revolver is a daytime ability. Reopen /actions from Day 2 to prepare a shot.'))
        elif role != 'Guardian Angel':
            box.add_item(discord.ui.TextDisplay("You have no available night ability. Use your existing role commands where appropriate."))
        add_history(self, box)


class TailorModal(discord.ui.Modal):
    def __init__(self, draft):
        super().__init__(title="Tailor disguise")
        self.draft = draft
        self.text = discord.ui.TextInput(label="Fake role", max_length=20, default=draft.options.get("fake_role", ""))
        self.add_item(self.text)

    async def on_submit(self, interaction):
        if not await self.draft.interaction_check(interaction):
            return
        self.draft.options["fake_role"] = str(self.text)
        self.draft.rebuild()
        await interaction.response.edit_message(view=self.draft, allowed_mentions=NO_MENTIONS)


class PagedPrivateView(PrivateView):
    def paged(self, box, key, values, label, callback):
        page = min(self.pages.get(key, 0), max(0, (len(values)-1)//25))
        if not values:
            box.add_item(discord.ui.TextDisplay(f"No eligible {label.lower()} available."))
            return
        async def picked(i, value):
            callback(value)
            self.rebuild()
            await i.response.edit_message(view=self, allowed_mentions=NO_MENTIONS)
        options = [discord.SelectOption(label=name[:100], value=str(value)) for value, name in values[page*25:(page+1)*25]]
        box.add_item(discord.ui.ActionRow(Picker(label, f"draft:{key}:{page}", options, picked)))
        if len(values) > 25:
            async def move(i, delta):
                self.pages[key] = page + delta
                self.rebuild()
                await i.response.edit_message(view=self, allowed_mentions=NO_MENTIONS)
            box.add_item(discord.ui.ActionRow(Button("Previous", f"draft:{key}:prev", lambda i: move(i, -1), disabled=page==0),
                         Button("Next", f"draft:{key}:next", lambda i: move(i, 1), disabled=(page+1)*25>=len(values))))


class Draft(PagedPrivateView):
    def __init__(self, controller, game, uid, ability, *, expected=None):
        super().__init__(controller, game, uid, expected=expected)
        self.ability, self.targets, self.options, self.pages = ability, {}, {}, {}
        if ability == 'ward':
            self.targets[0] = game.role_states.get(uid, {}).get('ga_target_id')
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        details = ", ".join(f"Target {n+1}: {player_label(self.game, uid)}" for n, uid in self.targets.items())
        if self.options:
            details += "\n" + ", ".join(f"{k}: {discord.utils.escape_mentions(discord.utils.escape_markdown(str(v)))}" for k, v in self.options.items())
        box = discord.ui.Container(discord.ui.TextDisplay(f"## Draft {actions.ability_label(self.ability)}\n{details or 'No choices yet.'}\nNothing is saved until you press Submit."))
        self.add_item(box)
        required = actions.ABILITIES[self.ability].targets
        if self.ability == "reanimate":
            corpses = actions.usable_corpses(self.game, self.uid)
            def corpse(value):
                self.options['corpse_id'] = int(value)
                self.targets.clear()
            self.paged(box, "corpse", [(e["player_id"], f"{player_label(self.game, e['player_id'])} · {e['real_role']}") for e in corpses], "Corpse", corpse)
            selected = next((e for e in corpses if e["player_id"] == self.options.get("corpse_id")), None)
            required = 2 if selected and selected["real_role"] == "Transporter" else 1
        members = self.game.ordered_living_players()
        if self.ability == "haunt":
            ids = self.game.role_states.get(self.uid, {}).get("guilty_voters", [])
            members = [p for p in members if p.id in ids]
        elif self.ability == "ward":
            bind = self.game.role_states.get(self.uid, {}).get('ga_target_id')
            members = [p for p in members if p.id == bind]
        elif not actions.ABILITIES[self.ability].allow_self:
            members = [p for p in members if p.id != self.uid]
        for n in range(required):
            if self.ability == 'ward':
                box.add_item(discord.ui.TextDisplay('**Ward target:** ' + player_label(self.game, self.targets.get(0))))
                continue
            choices = actions.seer_targets(self.game, self.uid, self.targets.get(1-n)) if self.ability == 'gaze' else members
            self.paged(box, f"target{n}", [(p.id, player_label(self.game, p.id)) for p in choices], f"Target {n+1}", lambda value, n=n: self.targets.__setitem__(n, int(value)))
        if self.ability == "hypnotize":
            self.paged(box, "message", [(x, x.title()) for x in actions.HYPNOTIST_MESSAGES], "Hypnotist message", lambda v: self.options.__setitem__("message_type", v))
        if self.ability == "tailor":
            async def text(i):
                await i.response.send_modal(TailorModal(self))
            box.add_item(discord.ui.ActionRow(Button("Enter fake role", "draft:tailor", text)))
        async def submit(i):
            await i.response.defer(ephemeral=True)
            result = await actions.submit(self.game, self.uid, self.ability, tuple(self.targets.get(n) for n in range(required)),
                                          expected=self.expected, guild=self.controller.guild(self.game), **self.options)
            await private_reply(i, result.message)
            if result.accepted:
                self.controller.after_submission(self.game, self.uid, result.action)
                for item in self.walk_children():
                    if isinstance(item,discord.ui.TextDisplay):
                        item.content = item.content.replace('## Draft ', '## Submitted ').replace('Nothing is saved until you press Submit.', result.message)
                        break
                for item in self.walk_children():
                    if hasattr(item, "disabled"):
                        item.disabled = True
                await i.edit_original_response(view=self, allowed_mentions=NO_MENTIONS)
        box.add_item(discord.ui.ActionRow(Button("Submit", "draft:submit", submit, style=discord.ButtonStyle.success,
            disabled=self.ability == 'gaze' and any(self.targets.get(n) is None for n in range(required)))))


class DeputyDraft(PagedPrivateView):
    def __init__(self, controller, game, uid, *, expected=None):
        super().__init__(controller, game, uid, expected=expected)
        self.target, self.pages = None, {}
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        box = discord.ui.Container(discord.ui.TextDisplay('## Prepare your shot\n'
            'Selecting a target does not fire. Review the target, then confirm Fire.'))
        self.add_item(box)
        members = [p for p in self.game.ordered_living_players() if p.id != self.uid]
        if self.target is not None:
            box.add_item(discord.ui.TextDisplay('**Selected:** ' + player_label(self.game, self.target)))
        self.paged(box, 'shot', [(p.id, player_label(self.game, p.id)) for p in members], 'Target',
            lambda value: setattr(self, 'target', int(value)))
        async def review(i):
            await i.response.defer(ephemeral=True)
            if self.target is None:
                raise st.Rejected('Choose a target first.')
            await private_reply(i, 'Review your shot',
                view=DeputyConfirmation(self.controller, self.game, self.uid, self.target, expected=self.expected))
        box.add_item(discord.ui.ActionRow(Button('Review shot', 'shot:review', review, disabled=self.target is None)))


class DeputyConfirmation(PrivateView):
    def __init__(self, controller, game, uid, target, *, expected=None):
        super().__init__(controller, game, uid, expected=expected)
        self.target = target
        self.closed = False
        box = discord.ui.Container(discord.ui.TextDisplay('## Confirm your shot\n'
            f'**Target:** {player_label(game, target)}\n'
            'Fire spends your only bullet immediately. Shooting an innocent player kills you from guilt.'))
        self.add_item(box)
        async def fire(i):
            from .deputy import fire as submit_shot
            if self.closed:
                raise st.Rejected('This confirmation has closed. Reopen /actions for current controls.')
            self.closed = True
            try:
                await i.response.defer(ephemeral=True)
                message, receipts = await submit_shot(game, uid, target, expected=self.expected, guild=controller.guild(game))
            except BaseException:
                self.closed = game.role_states.get(uid, {}).get('deputy_shots_remaining', 0) <= 0
                raise
            controller.after_deputy_shot(game, uid, receipts)
            self.clear_items()
            self.add_item(discord.ui.Container(discord.ui.TextDisplay('## Shot committed\n' + message)))
            await private_reply(i, message)
            await i.edit_original_response(view=self, allowed_mentions=NO_MENTIONS)
        async def cancel(i):
            if self.closed:
                raise st.Rejected('This confirmation has closed. Reopen /actions for current controls.')
            self.closed = True
            self.clear_items()
            self.add_item(discord.ui.Container(discord.ui.TextDisplay('Shot cancelled. Your bullet has not been spent.')))
            await i.response.edit_message(view=self, allowed_mentions=NO_MENTIONS)
        box.add_item(discord.ui.ActionRow(Button('Fire', 'shot:fire', fire, style=discord.ButtonStyle.danger),
                                         Button('Cancel', 'shot:cancel', cancel)))


class DuelView(PrivateView):
    def __init__(self, controller, game, actor_id, uid, action):
        super().__init__(controller, game, uid, persistent=True)
        self.actor_id = actor_id
        self.token = action["duel_token"]
        finished = action.get("duel_finished")
        if finished:
            choices = action.get("duel_choices", {})
            text = f"## Duel complete\n{action.get('duel_result', '')}\n" + "\n".join(f"{player_label(game, int(k))}: {v.title()}" for k, v in choices.items())
        else:
            deadline = int(datetime.fromisoformat(action['duel_deadline']).timestamp())
            text = f"## Pirate duel\nChoose once before <t:{deadline}:R>. Choices stay private until completion. Unanswered choices are random."
        box = discord.ui.Container(discord.ui.TextDisplay(text))
        self.add_item(box)
        buttons = []
        for choice in duels.CHOICES:
            async def choose(i, choice=choice):
                await i.response.defer(ephemeral=True)
                await duels.choose(game, actor_id, self.token, uid, choice, guild=controller.guild(game))
                await private_reply(i, "Choice locked. The result will appear when the duel finishes.")
                controller.wake_duel(game, actor_id, self.token)
            buttons.append(Button(choice.title(), f"mf:{self.token}:{uid}:{choice}", choose, disabled=bool(finished)))
        box.add_item(discord.ui.ActionRow(*buttons))
