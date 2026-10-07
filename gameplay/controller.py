from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from types import SimpleNamespace

import discord
from async_work import finish_pending

from config import GAME_OVERSEER_ROLE_ID, PLAYER_PRIVATE_CHANNEL_IDS
from . import state as st, trials, duels, reports
from .views import DuelView, NightPanel, DayPanel, ReportHistory, TrialView, NO_MENTIONS


def configured_channel(uid):
    value = PLAYER_PRIVATE_CHANNEL_IDS.get(uid)
    if value:
        return int(value)
    return next((int(k) for k, v in PLAYER_PRIVATE_CHANNEL_IDS.items() if int(v) == uid), None)


def channel_is_private(channel, guild, uid):
    """Fail closed if another ordinary member or role can read the channel."""
    if not channel or getattr(channel, "guild", None) is not guild or not hasattr(channel, "permissions_for"):
        return False
    if channel.permissions_for(guild.default_role).view_channel:
        return False
    owner = guild.get_member(uid)
    if not owner or not channel.permissions_for(owner).view_channel:
        return False
    for target, overwrite in channel.overwrites.items():
        if overwrite.view_channel is not True:
            continue
        if isinstance(target, discord.Role):
            if target.id != GAME_OVERSEER_ROLE_ID and not target.permissions.administrator:
                return False
        elif target.id != uid and not getattr(target, "bot", False):
            if not (target.guild_permissions.administrator or any(r.id == GAME_OVERSEER_ROLE_ID for r in target.roles)):
                return False
    # Chunk before calling this function. Checking effective permissions also catches inheritance.
    for member in guild.members:
        if member.id == uid or getattr(member, "bot", False):
            continue
        staff = member.guild_permissions.administrator or any(r.id == GAME_OVERSEER_ROLE_ID for r in member.roles)
        if not staff and channel.permissions_for(member).view_channel:
            return False
    return True


class Controller:
    def __init__(self, bot):
        self.bot = bot
        self.jobs, self.events, self.refreshes = {}, {}, {}

    def guild(self, game):
        return self.bot.get_guild(game.guild_id)

    def start_job(self, key, factory):
        task = self.jobs.get(key)
        if task and not task.done():
            return task
        async def supervised():
            delay = 1
            while True:
                try:
                    await factory()
                    return
                except st.Rejected:
                    return
                except (OSError, discord.HTTPException) as error:
                    logging.warning("Gameplay delivery/save retry: %s", type(error).__name__)
                    await asyncio.sleep(delay)
                    delay = min(30, delay * 2)
        task = asyncio.create_task(supervised())
        self.jobs[key] = task
        def done(t):
            if self.jobs.get(key) is t:
                self.jobs.pop(key, None)
            if not t.cancelled() and t.exception():
                logging.error("Gameplay controller stopped: %s", type(t.exception()).__name__)
        task.add_done_callback(done)
        return task

    async def stop_game(self, game):
        await self._stop_jobs(lambda key: key[0] == game.guild_id)

    async def stop_all(self):
        await self._stop_jobs(lambda key: True)

    async def _stop_jobs(self, selected):
        current = asyncio.current_task()
        cancelled = []
        for key, task in list(self.jobs.items()) + list(self.refreshes.items()):
            if selected(key) and task is not current:
                task.cancel()
                cancelled.append(task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    async def private_destination(self, game, uid):
        guild = self.guild(game)
        if not guild:
            raise st.Rejected("The game server is unavailable.")
        member = await game.get_member_safe(guild, uid)
        if not member:
            raise st.Rejected("The player has left the server.")
        cid = configured_channel(uid)
        channel = guild.get_channel(cid) if cid else None
        if channel:
            try:
                if not guild.chunked:
                    await guild.chunk(cache=True)
                if guild.chunked and channel_is_private(channel, guild, uid):
                    return channel
            except discord.HTTPException:
                pass
        return await member.create_dm()

    async def fetch_message(self, destination, reference):
        if not reference or reference.get("channel_id") != destination.id:
            return None
        try:
            return await destination.fetch_message(reference["message_id"])
        except discord.NotFound:
            return None

    async def send_panel(self, game, uid, *, reopen=False, expected=None, phase=None):
        expected = st.identity(game) if expected is None else expected
        phase = game.phase if phase is None else phase
        lock = game.delivery_locks.setdefault(f'panel:{uid}', asyncio.Lock())
        async with lock:
            from .lifecycle import message_lock
            async with message_lock(game.guild_id):
                return await self._send_panel(game, uid, expected=expected, phase=phase)

    async def _send_panel(self, game, uid, *, expected, phase):
        st.require_current(game, phase=phase, expected=expected)
        destination = await self.private_destination(game, uid)
        st.require_current(game, phase=phase, expected=expected)
        refs = game.gameplay["panels"]
        reference = refs.get(str(uid))
        message = await self.fetch_message(destination, reference)
        st.require_current(game, phase=phase, expected=expected)
        view = self.panel_for(game, uid, persistent=True, expected=expected, regular=True)
        if message:
            await finish_pending(message.edit(view=view, allowed_mentions=NO_MENTIONS))
        else:
            message = await finish_pending(destination.send(view=view, allowed_mentions=NO_MENTIONS))
            def save_ref():
                st.require_current(game, phase=phase, expected=expected)
                game.gameplay["panels"][str(uid)] = {"channel_id": destination.id, "message_id": message.id}
            await st.commit(game, save_ref)
        self.bot.add_view(view, message_id=message.id)
        return message

    async def send_day_panels(self, game):
        st.require_current(game, phase='day')
        # Deputies receive their day controls automatically; reopened report/status
        # panels are restored on reconnect without bulk delivery to other roles.
        owners = {p.id for p in game.living_players if game.player_roles.get(p.id) == 'Deputy' and game.day_number >= 2}
        owners.update(int(uid) for uid in game.gameplay['panels'] if str(uid).isdigit())
        for uid in owners:
            try:
                await self.send_panel(game, uid)
            except (discord.HTTPException, st.Rejected):
                logging.warning('Private day panel unavailable for player %s; /actions can reopen it.', uid)

    async def send_night_panels(self, game):
        for member in list(game.players):
            state=game.role_states.get(member.id,{})
            if member.id not in {p.id for p in game.living_players} and not (state.get('can_haunt') or state.get('haunt_target') is not None or game.player_roles.get(member.id) == 'Guardian Angel'
                    or (str(member.id) in game.gameplay['panels'] and reports.history(game, member.id))):
                continue
            try:
                await self.send_panel(game, member.id)
            except (discord.HTTPException, st.Rejected):
                # Never put role details or submitted actions in a public fallback.
                logging.warning("Private night panel unavailable for player %s; /actions can reopen it.", member.id)

    async def close_night_panels(self, references):
        for reference in references.values():
            try:
                destination=self.bot.get_channel(reference['channel_id'])
                if destination is None:
                    destination=await self.bot.fetch_channel(reference['channel_id'])
                message=await destination.fetch_message(reference['message_id'])
                view=discord.ui.LayoutView(timeout=None)
                view.add_item(discord.ui.Container(discord.ui.TextDisplay('These phase controls have closed. Use /actions to reopen your current controls.')))
                await message.edit(view=view,allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                continue

    def panel_for(self, game, uid, *, persistent=False, expected=None, regular=False):
        st.require_current(game, expected=expected)
        if uid not in {p.id for p in game.players}:
            raise st.Rejected('These controls are only available to players in this match.')
        for actor, action in ([] if regular else game.night_actions.items()):
            if (action.get("type") == "plunder" and uid in {actor, action.get("target")}
                    and (not action.get("duel_finished") or uid not in action.get('duel_delivered', []))):
                return DuelView(self, game, actor, uid, action)
        for action in ([] if regular else game.gameplay.get('duels', {}).values()):
            if uid in {action['actor'], action['target']} and uid not in action.get('duel_delivered', []):
                return DuelView(self, game, action['actor'], uid, action)
        if game.phase == 'day':
            if game.player_roles.get(uid) in {'Deputy', 'Guardian Angel', 'Seer', 'Psychic', 'Serial Killer'} or reports.history(game, uid):
                return DayPanel(self, game, uid, persistent=persistent, expected=expected)
            raise st.Rejected('Your role has no daytime controls. Reopen /actions at night.')
        st.require_current(game, phase='night')
        state=game.role_states.get(uid,{})
        if uid not in {p.id for p in game.living_players} and not (state.get('can_haunt') or state.get('haunt_target') is not None or game.player_roles.get(uid) == 'Guardian Angel'):
            if reports.history(game, uid):
                return ReportHistory(self, game, uid, persistent=persistent, expected=expected)
            raise st.Rejected("You have no available night actions.")
        return NightPanel(self, game, uid, persistent=persistent, expected=expected)

    async def reopen(self, game, uid):
        view = self.panel_for(game, uid)
        if isinstance(view, DuelView):
            action=duels.get_duel(game,view.actor_id,view.token,open_only=False)
            await self.duel_prompt(game,view.actor_id,uid,action)
            return
        await self.send_panel(game, uid, reopen=True)

    def after_submission(self, game, actor, action):
        if str(actor) in game.gameplay['panels']:
            expected, phase = st.identity(game), game.phase
            self.start_job((game.guild_id,'panel',actor,expected,phase),
                lambda: self.send_panel(game,actor,expected=expected,phase=phase))
        if action.get("type") == "plunder":
            token = action["duel_token"]
            self.start_job((game.guild_id, "duel", token), lambda: self.run_duel(game, actor, token))

    def after_deputy_shot(self, game, actor, receipts):
        self.after_submission(game, actor, {'type': 'deputy_fire'})
        match = game.game_key
        async def deliver():
            st.require_current(game)
            if game.game_key != match:
                raise st.Rejected('This shot belongs to an earlier match.')
            channel = self.public_destination(game)
            for receipt in receipts:
                current = game.gameplay['deaths'].get(str(receipt['player_id']))
                if current and not current.get('delivered'):
                    await game.deliver_death_receipt(channel, self.guild(game), current)
                    self.retry_death(game, receipt['player_id'])
            await game.check_win_conditions()
        return self.start_job((game.guild_id, 'deputy', actor, match, game.day_number), deliver)

    def wake_duel(self, game, actor, token):
        event = self.events.get((game.guild_id, "duel", token))
        if event:
            event.set()
        self.start_job((game.guild_id, "duel", token), lambda: self.run_duel(game, actor, token))

    async def duel_prompt(self, game, actor, uid, action):
        from .lifecycle import message_lock
        async with message_lock(game.guild_id):
            return await self._duel_prompt(game, actor, uid, action)

    async def _duel_prompt(self, game, actor, uid, action):
        token = action["duel_token"]
        destination = await self.private_destination(game, uid)
        action = duels.get_duel(game, actor, token, open_only=False)
        message = await self.fetch_message(destination, action.get("duel_prompts", {}).get(str(uid)))
        view = DuelView(self, game, actor, uid, action)
        if message:
            await finish_pending(message.edit(view=view, allowed_mentions=NO_MENTIONS))
        else:
            message = await finish_pending(destination.send(view=view, allowed_mentions=NO_MENTIONS))
        def checkpoint():
            current = duels.get_duel(game, actor, token, open_only=False)
            current.setdefault("duel_prompts", {})[str(uid)] = {"channel_id": destination.id, "message_id": message.id}
            if current.get("duel_finished"):
                delivered = current.setdefault("duel_delivered", [])
                if uid not in delivered:
                    delivered.append(uid)
                game.gameplay.setdefault('duels', {})[token] = deepcopy(current)
        await st.commit(game, checkpoint)
        self.bot.add_view(view, message_id=message.id)

    async def run_duel(self, game, actor, token):
        key = (game.guild_id, "duel", token)
        event = self.events.setdefault(key, asyncio.Event())
        try:
            while True:
                action = duels.get_duel(game, actor, token, open_only=False)
                if not action.get("duel_finished"):
                    for uid in {actor, action["target"]}:
                        if str(uid) not in action.get("duel_prompts", {}):
                            try:
                                await self.duel_prompt(game, actor, uid, action)
                            except (discord.HTTPException, st.Rejected):
                                pass  # Blocked DMs still use the original timeout/random fallback.
                    try:
                        action = await duels.complete(game, actor, token, guild=self.guild(game))
                    except st.Rejected as error:
                        if str(error) != "The duel is still open.":
                            raise
                        event.clear()
                        try:
                            await asyncio.wait_for(event.wait(), timeout=max(.01, min(5, st.remaining(action["duel_deadline"]))))
                        except asyncio.TimeoutError:
                            pass
                        continue
                for uid in {actor, action["target"]}:
                    if uid not in action.get("duel_delivered", []):
                        try:
                            await self.duel_prompt(game, actor, uid, action)
                        except (discord.HTTPException, st.Rejected):
                            logging.warning("Duel completion delivery unavailable for player %s.", uid)
                return
        finally:
            self.events.pop(key, None)

    def trial_record(self, game, token, *, archived=False):
        if not archived:
            return st.session(game, token, open_only=False)
        st.require_current(game)
        trial = game.gameplay.get('trials', {}).get(token)
        if not trial or trial.get('match', game.game_key) != game.game_key:
            raise st.Rejected('This result belongs to an earlier match.')
        return trial

    async def render_trial(self, game, *, token=None, archived=False):
        token = token or game.gameplay['trial']['id']
        trial = self.trial_record(game, token, archived=archived)
        guild = self.guild(game)
        channel = guild.get_channel(trial["channel_id"]) if guild else None
        if not channel and guild:
            channel = guild.get_channel(game.game_channel_id) or guild.get_channel(game.day_tc_id)
            if channel:
                def replace_channel():
                    current = self.trial_record(game, token, archived=archived)
                    current.update(channel_id=channel.id,message_id=None)
                await st.commit(game,replace_channel)
        if not channel:
            if archived or trial['stage'] in {'closed', 'done', 'cancelled'}:
                return False
            def cancel():
                current = st.session(game, token, open_only=False)
                trials.cancel_model(game, current, "Trial channel unavailable.", refund=True)
            await st.commit(game, cancel)
            return False
        message = None
        if trial.get("message_id"):
            try:
                message = await channel.fetch_message(trial["message_id"])
            except discord.NotFound:
                pass
        view = TrialView(self, game, trial, archived=archived)
        if message:
            await message.edit(view=view, allowed_mentions=NO_MENTIONS)
        else:
            message = await channel.send(view=view, allowed_mentions=NO_MENTIONS)
            def save_ref():
                self.trial_record(game, token, archived=archived)["message_id"] = message.id
            await st.commit(game, save_ref)
        self.bot.add_view(view, message_id=message.id)
        return True

    def refresh_trial(self, game):
        token = game.gameplay["trial"]["id"]
        key = (game.guild_id, "refresh", token)
        if key in self.refreshes and not self.refreshes[key].done():
            return
        async def refresh():
            try:
                await asyncio.sleep(.75)
                st.session(game, token, open_only=False)
                await self.render_trial(game)
            except (st.Rejected, discord.HTTPException, OSError):
                pass
            finally:
                self.refreshes.pop(key, None)
        self.refreshes[key] = asyncio.create_task(refresh())

    async def repair_voice(self, game, *, defense=False):
        guild = self.guild(game)
        if not guild:
            return False
        vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
        alive = guild.get_role(game.alive_role_id) if game.alive_role_id else None
        stand = guild.get_role(game.stand_role_id) if game.stand_role_id else None
        trial = game.gameplay.get("trial") or {}
        ok = True
        try:
            if vc and alive:
                    await finish_pending(vc.set_permissions(alive, connect=True, speak=False if defense or game.phase == "night" else True))
            if stand:
                for member in list(guild.members):
                    if defense and member.id == trial.get("defendant"):
                        if stand not in member.roles:
                            await finish_pending(member.add_roles(stand))
                    elif stand in member.roles:
                        await finish_pending(member.remove_roles(stand))
        except discord.HTTPException:
            ok = False
        return ok

    async def run_trial(self, game, token):
        while True:
            trial = st.session(game, token, open_only=False)
            stage = trial["stage"]
            if stage == 'cancelled' or (stage == "done" and (trial.get("progressed") or not trial.get("result"))):
                cleaned = await self.repair_voice(game)
                def completed():
                    current = st.session(game, token, open_only=False)
                    current.update(delivery_pending=True, permissions_cleaned=cleaned)
                    game.gameplay.setdefault('trials', {})[token] = current
                await st.commit(game, completed)
                self.retry_trial_delivery(game, token)
                if not cleaned:
                    self.retry_voice(game)
                return
            if stage in {"closed", "done"}:
                if not trial.get("applied"):
                    await trials.apply_result(game, token)
                    if game.gameplay["trial"]["stage"] == "cancelled":
                        continue
                cleaned = await self.repair_voice(game)
                def cleanup_checkpoint():
                    st.session(game, token, open_only=False)['permissions_cleaned'] = cleaned
                await st.commit(game, cleanup_checkpoint)
                channel = self.public_destination(game)
                if trial['result']['guilty']:
                    receipt = game.gameplay['deaths'].get(str(trial['defendant']))
                    if receipt and not receipt.get('delivered'):
                        await game.deliver_death_receipt(None, self.guild(game), receipt)
                        self.retry_death(game, trial['defendant'])
                def pending_display():
                    st.session(game, token, open_only=False)['delivery_pending'] = True
                await st.commit(game, pending_display)
                await trials.finish(game, token)
                self.retry_trial_delivery(game, token)
                if not cleaned:
                    self.retry_voice(game)
                if await game.check_win_conditions():
                    return
                if trial["result"]["guilty"] and game.phase == "day":
                    async def quiet_notice(*args, **kwargs):
                        return None
                    ctx = SimpleNamespace(guild=self.guild(game), send=channel.send if channel else quiet_notice)
                    await game.start_night(ctx, trial_token=token)
                else:
                    def progressed():
                        st.session(game, token, open_only=False)["progressed"] = True
                    await st.commit(game, progressed)
                return
            if game.phase != "day":
                def cancel():
                    trials.cancel_model(game, st.session(game, token, open_only=False), "The day ended.", refund=True)
                await st.commit(game, cancel)
                continue
            # Deadlines are model transitions, independent of message fetch/edit permissions.
            await game.sync_living_players(self.guild(game))
            if stage != 'nomination' and trial['defendant'] not in {p.id for p in game.living_players}:
                def cancel_departure():
                    trials.cancel_model(game, st.session(game, token, open_only=False), 'The defendant died or left.', refund=True)
                await st.commit(game, cancel_departure)
                continue
            left = st.remaining(trial['deadline'])
            if left <= 0:
                await trials.advance(game, token, guild=self.guild(game))
                continue
            self.refresh_trial(game)
            await self.repair_voice(game, defense=stage == 'defense')
            await asyncio.sleep(min(5, left))

    async def begin_trial(self, game, role_ids, channel_id, *, actor_id=None):
        trial = await trials.start(game, role_ids=role_ids, channel_id=channel_id, guild=self.guild(game),actor_id=actor_id)
        self.start_job((game.guild_id, "trial", trial["id"]), lambda: self.run_trial(game, trial["id"]))
        return trial

    def retry_trial_delivery(self, game, token):
        record = game.gameplay.get('trials', {}).get(token)
        if record and record.get('delivery_pending'):
            self.start_job((game.guild_id, 'trial-delivery', token),
                lambda: self.deliver_deferred_trial(game, token))

    async def deliver_deferred_trial(self, game, token):
        trial = self.trial_record(game, token, archived=True)
        if not trial.get('delivery_pending'):
            return
        if not await self.render_trial(game, token=token, archived=True):
            raise OSError('Trial result channel unavailable')
        await st.commit(game, lambda: self.trial_record(game, token, archived=True).update(delivery_pending=False))

    def retry_voice(self, game):
        async def repair():
            st.require_current(game)
            defense = (game.phase == 'day' and (game.gameplay.get('trial') or {}).get('stage') == 'defense')
            if not await self.repair_voice(game, defense=defense):
                raise OSError('Voice cleanup pending')
            def checkpoint():
                st.require_current(game)
                for trial in game.gameplay.get('trials', {}).values():
                    trial['permissions_cleaned'] = True
            await st.commit(game, checkpoint)
        return self.start_job((game.guild_id, 'voice-repair', game.game_key), repair)

    def public_destination(self, game):
        guild=self.guild(game)
        if not guild:
            return None
        candidates=[guild.get_channel(game.game_channel_id),guild.get_channel(game.day_tc_id),getattr(guild,'system_channel',None)]
        candidates.extend(getattr(guild,'text_channels',[]))
        for channel in candidates:
            if channel is None or not hasattr(channel,'send'):
                continue
            me=getattr(guild,'me',None)
            if me:
                permissions=channel.permissions_for(me)
                if not (permissions.view_channel and permissions.send_messages):
                    continue
            # Never use an arbitrary private or unrelated channel for public results.
            if channel.id in {game.game_channel_id,game.day_tc_id} or channel.permissions_for(guild.default_role).view_channel:
                return channel
        return None

    async def recover_resolution(self, game):
        from . import resolution
        st.require_current(game,phase='night')
        channel=self.public_destination(game)
        async def defer_notice(*args,**kwargs):
            return None
        ctx=SimpleNamespace(guild=self.guild(game),send=channel.send if channel else defer_notice,
                            public_available=channel is not None)
        await resolution.finish(game,ctx)
        # Applied results survive even when no public channel is usable. Move on,
        # repair voice access and retry public notices independently of the model.
        if game.in_progress and game.gameplay.get('resolution',{}).get('public_delivery_pending'):
            token=game.gameplay['resolution']['night_token']
            self.start_job((game.guild_id,'resolution-delivery',token),lambda:self.deliver_deferred_resolution(game,token))

    async def deliver_deferred_resolution(self, game, token):
        st.require_current(game)
        record=game.gameplay.get('resolutions',{}).get(token)
        if record is None and game.gameplay.get('resolution',{}).get('night_token') == token:
            record = game.gameplay['resolution']
        if not record or not record.get('public_delivery_pending'):
            return
        channel=self.public_destination(game)
        if channel is None:
            raise OSError('Public results channel unavailable')
        for uid in record['death_ids']:
            receipt=game.gameplay['deaths'].get(str(uid))
            if receipt and not receipt.get('delivered'):
                await game.deliver_death_receipt(channel,self.guild(game),receipt)
                if not game.gameplay['deaths'][str(uid)].get('delivered'):
                    raise OSError('Death delivery pending')
        await channel.send(f"Night {record.get('day',max(0,game.day_number-1))} results are complete. "
                           f"Current phase: {game.phase.title()} {game.day_number}. Remaining players: {len(game.living_players)}.",
                           allowed_mentions=NO_MENTIONS)
        def delivered():
            st.require_current(game)
            record['public_delivery_pending']=False
        await st.commit(game,delivered)
        await game.check_win_conditions()

    def retry_death(self, game, uid):
        receipt = game.gameplay['deaths'].get(str(uid))
        if not receipt or receipt.get('delivered'):
            return
        async def deliver():
            st.require_current(game)
            channel = self.public_destination(game)
            await game.deliver_death_receipt(channel, self.guild(game), receipt)
            if receipt.get('cause') in {'manual', 'left', 'deputy_shoot', 'deputy_friendly_fire', 'deputy_guilt'} and not game.resolving:
                if await game.check_win_conditions():
                    return
            if not receipt.get('delivered'):
                raise OSError('Death delivery pending')
        self.start_job((game.guild_id, 'death-delivery', game.game_key, uid), deliver)

    async def recover(self, game):
        if not game.in_progress or game.ending:
            return
        startup_record = game.gameplay.get('startup', {})
        if startup_record and (startup_record.get('complete') is False or not startup_record.get('announced')):
            from . import startup
            self.start_job((game.guild_id, 'startup', game.game_key),
                lambda: startup.resume(game, self.guild(game), client=self.bot))
            return
        await game.sync_living_players(self.guild(game))
        for token, trial in list(game.gameplay.get('trials', {}).items()):
            self.retry_trial_delivery(game, token)
            if not trial.get('permissions_cleaned'):
                self.retry_voice(game)
        for uid in game.gameplay['deaths']:
            self.retry_death(game, int(uid))
        for token, action in list(game.gameplay.get('duels', {}).items()):
            if action.get('duel_finished') and not {action['actor'],action['target']}.issubset(set(action.get('duel_delivered',[]))):
                actor=action['actor']
                self.start_job((game.guild_id, 'duel', token), lambda actor=actor, token=token: self.run_duel(game, actor, token))
        record = game.gameplay.get("resolution")
        records = dict(game.gameplay.get('resolutions', {}))
        if record:
            records.setdefault(record['night_token'], record)
        for token, saved in records.items():
            if saved.get('progressed') and saved.get('public_delivery_pending'):
                self.start_job((game.guild_id,'resolution-delivery',token),lambda token=token:self.deliver_deferred_resolution(game,token))
        if game.phase == "night":
            if record and record.get("applied") and not record.get("progressed"):
                self.start_job((game.guild_id, "resolution", record.get("night_token")), lambda:self.recover_resolution(game))
                return
            # A snapshot from before modern panels gets one stable night identity.
            if not game.gameplay.get("night_token"):
                import secrets
                await st.commit(game, lambda: game.gameplay.__setitem__("night_token", secrets.token_hex(8)))
            await self.send_night_panels(game)
            for actor, action in list(game.night_actions.items()):
                if action.get("type") != "plunder":
                    continue
                if not action.get("duel_deadline"):
                    # Legacy incomplete duels have no recoverable choices or deadline.
                    def legacy_cancel(action=action):
                        if not action.get("duel_finished"):
                            action.update(duel_won=False, duel_finished=True)
                    await st.commit(game, legacy_cancel)
                elif action.get("duel_token"):
                    self.after_submission(game, actor, action)
        elif game.phase == 'day':
            await self.send_day_panels(game)
        trial = game.gameplay.get("trial")
        if trial and trial.get('day') != game.day_number:
            # Completion receipts survive later days, but their controls stay stale.
            trial = None
        if trial and (trial.get("stage") not in {"done", "cancelled"} or not trial.get("progressed")):
            self.start_job((game.guild_id, "trial", trial["id"]), lambda: self.run_trial(game, trial["id"]))
        elif trial:
            await self.render_trial(game)
            await self.repair_voice(game)
        channel = self.public_destination(game)
        if channel:
            for receipt in list(game.gameplay["deaths"].values()):
                if not receipt.get("delivered"):
                    async def deliver(receipt=receipt):
                        st.require_current(game)
                        await game.deliver_death_receipt(channel,self.guild(game),receipt)
                        current=game.gameplay['deaths'].get(str(receipt['player_id']))
                        if current and not current.get('delivered'):
                            raise OSError('Death delivery pending')
                        if receipt.get('cause') in {'deputy_shoot', 'deputy_friendly_fire', 'deputy_guilt'}:
                            await game.check_win_conditions()
                    self.start_job((game.guild_id,'death',receipt['player_id']),deliver)
