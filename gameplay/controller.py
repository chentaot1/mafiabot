from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from types import SimpleNamespace

import discord

from config import GAME_OVERSEER_ROLE_ID, PLAYER_PRIVATE_CHANNEL_IDS
from . import state as st, trials, duels
from .views import DuelView, NightPanel, TrialView, NO_MENTIONS


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
        current = asyncio.current_task()
        cancelled = []
        for key, task in list(self.jobs.items()) + list(self.refreshes.items()):
            if key[0] == game.guild_id and task is not current:
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

    async def send_panel(self, game, uid, *, reopen=False):
        expected = st.identity(game)
        destination = await self.private_destination(game, uid)
        st.require_current(game, phase="night", expected=expected)
        refs = game.gameplay["panels"]
        reference = refs.get(str(uid))
        message = await self.fetch_message(destination, reference)
        view = NightPanel(self, game, uid, expected=expected)
        if message:
            await message.edit(view=view, allowed_mentions=NO_MENTIONS)
        else:
            message = await destination.send(view=view, allowed_mentions=NO_MENTIONS)
            def save_ref():
                st.require_current(game, phase="night", expected=expected)
                game.gameplay["panels"][str(uid)] = {"channel_id": destination.id, "message_id": message.id}
            await st.commit(game, save_ref)
        self.bot.add_view(view, message_id=message.id)
        return message

    async def send_night_panels(self, game):
        for member in list(game.players):
            state=game.role_states.get(member.id,{})
            if member.id not in {p.id for p in game.living_players} and not (state.get('can_haunt') or state.get('haunt_target') is not None or game.player_roles.get(member.id) == 'Guardian Angel'):
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
                view.add_item(discord.ui.Container(discord.ui.TextDisplay('Night actions have closed. Use /actions during the next night.')))
                await message.edit(view=view,allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                continue

    def panel_for(self, game, uid):
        st.require_current(game)
        for actor, action in game.night_actions.items():
            if (action.get("type") == "plunder" and uid in {actor, action.get("target")}
                    and (not action.get("duel_finished") or uid not in action.get('duel_delivered', []))):
                return DuelView(self, game, actor, uid, action)
        for action in game.gameplay.get('duels', {}).values():
            if uid in {action['actor'], action['target']} and uid not in action.get('duel_delivered', []):
                return DuelView(self, game, action['actor'], uid, action)
        st.require_current(game, phase="night")
        state=game.role_states.get(uid,{})
        if uid not in {p.id for p in game.living_players} and not (state.get('can_haunt') or state.get('haunt_target') is not None or game.player_roles.get(uid) == 'Guardian Angel'):
            raise st.Rejected("You have no available night actions.")
        return NightPanel(self, game, uid, persistent=False)

    async def reopen(self, game, uid):
        view = self.panel_for(game, uid)
        if isinstance(view, DuelView):
            action=duels.get_duel(game,view.actor_id,view.token,open_only=False)
            await self.duel_prompt(game,view.actor_id,uid,action)
            return
        await self.send_panel(game, uid, reopen=True)

    def after_submission(self, game, actor, action):
        if str(actor) in game.gameplay['panels']:
            self.start_job((game.guild_id,'panel',actor),lambda: self.send_panel(game,actor))
        if action.get("type") == "plunder":
            token = action["duel_token"]
            self.start_job((game.guild_id, "duel", token), lambda: self.run_duel(game, actor, token))

    def wake_duel(self, game, actor, token):
        event = self.events.get((game.guild_id, "duel", token))
        if event:
            event.set()
        self.start_job((game.guild_id, "duel", token), lambda: self.run_duel(game, actor, token))

    async def duel_prompt(self, game, actor, uid, action):
        token = action["duel_token"]
        destination = await self.private_destination(game, uid)
        action = duels.get_duel(game, actor, token, open_only=False)
        message = await self.fetch_message(destination, action.get("duel_prompts", {}).get(str(uid)))
        view = DuelView(self, game, actor, uid, action)
        if message:
            await message.edit(view=view, allowed_mentions=NO_MENTIONS)
        else:
            message = await destination.send(view=view, allowed_mentions=NO_MENTIONS)
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

    async def render_trial(self, game):
        trial = game.gameplay["trial"]
        token = trial["id"]
        guild = self.guild(game)
        channel = guild.get_channel(trial["channel_id"]) if guild else None
        if not channel and guild:
            channel = guild.get_channel(game.game_channel_id) or guild.get_channel(game.day_tc_id)
            if channel:
                def replace_channel():
                    current = st.session(game,token,open_only=False)
                    current.update(channel_id=channel.id,message_id=None)
                await st.commit(game,replace_channel)
        if not channel:
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
        view = TrialView(self, game, trial)
        if message:
            await message.edit(view=view, allowed_mentions=NO_MENTIONS)
        else:
            message = await channel.send(view=view, allowed_mentions=NO_MENTIONS)
            def save_ref():
                st.session(game, token, open_only=False)["message_id"] = message.id
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
                await vc.set_permissions(alive, connect=True, speak=False if defense or game.phase == "night" else True)
            if stand:
                for member in list(guild.members):
                    if defense and member.id == trial.get("defendant"):
                        if stand not in member.roles:
                            await member.add_roles(stand)
                    elif stand in member.roles:
                        await member.remove_roles(stand)
        except discord.HTTPException:
            ok = False
        return ok

    async def run_trial(self, game, token):
        while True:
            trial = st.session(game, token, open_only=False)
            stage = trial["stage"]
            if stage == 'cancelled' or (stage == "done" and (trial.get("progressed") or not trial.get("result"))):
                await self.render_trial(game)
                if not await self.repair_voice(game):
                    raise OSError('Voice cleanup pending')
                return
            if stage in {"closed", "done"}:
                if not trial.get("applied"):
                    await trials.apply_result(game, token)
                    if game.gameplay["trial"]["stage"] == "cancelled":
                        continue
                usable = await self.render_trial(game)
                cleaned = await self.repair_voice(game)
                if not cleaned:
                    raise OSError("Voice cleanup pending")
                def cleanup_checkpoint():
                    st.session(game, token, open_only=False)["permissions_cleaned"] = True
                await st.commit(game, cleanup_checkpoint)
                if not usable:
                    # The logical verdict survives even if all public channels were deleted.
                    # Release voting and voice restrictions; delivery can resume when a channel returns.
                    def unavailable():
                        current = st.session(game,token,open_only=False)
                        current.update(stage='done',progressed=True,reason='Result channel unavailable.')
                        trials.clear_flags(game)
                    await st.commit(game,unavailable)
                    return
                channel = self.guild(game).get_channel(trial["channel_id"])
                if trial["result"]["guilty"]:
                    receipt = game.gameplay["deaths"].get(str(trial["defendant"]))
                    if receipt and not receipt.get("delivered"):
                        await game.deliver_death_receipt(channel, self.guild(game), receipt)
                        if not receipt.get("delivered"):
                            raise OSError("Death delivery pending")
                await trials.finish(game, token)
                if await game.check_win_conditions():
                    return
                if trial["result"]["guilty"] and game.phase == "day":
                    ctx = SimpleNamespace(guild=self.guild(game), send=channel.send)
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
            if not await self.render_trial(game):
                await self.repair_voice(game)
                return
            await self.repair_voice(game, defense=stage == "defense")
            await game.sync_living_players(self.guild(game))
            if stage != "nomination" and trial["defendant"] not in {p.id for p in game.living_players}:
                def cancel_departure():
                    trials.cancel_model(game, st.session(game, token, open_only=False), "The defendant died or left.", refund=True)
                await st.commit(game, cancel_departure)
                continue
            left = st.remaining(trial["deadline"])
            if left > 0:
                await asyncio.sleep(min(5, left))
                continue
            await trials.advance(game, token, guild=self.guild(game))

    async def begin_trial(self, game, role_ids, channel_id, *, actor_id=None):
        trial = await trials.start(game, role_ids=role_ids, channel_id=channel_id, guild=self.guild(game),actor_id=actor_id)
        self.start_job((game.guild_id, "trial", trial["id"]), lambda: self.run_trial(game, trial["id"]))
        return trial

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
        record=game.gameplay.get('resolution',{})
        if record.get('night_token')!=token or not record.get('public_delivery_pending'):
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
            current=game.gameplay.get('resolution',{})
            if current.get('night_token')==token:
                current['public_delivery_pending']=False
        await st.commit(game,delivered)
        await game.check_win_conditions()

    async def recover(self, game):
        if not game.in_progress or game.ending:
            return
        await game.sync_living_players(self.guild(game))
        for token, action in list(game.gameplay.get('duels', {}).items()):
            if action.get('duel_finished') and not {action['actor'],action['target']}.issubset(set(action.get('duel_delivered',[]))):
                actor=action['actor']
                self.start_job((game.guild_id, 'duel', token), lambda actor=actor, token=token: self.run_duel(game, actor, token))
        record = game.gameplay.get("resolution")
        if record and record.get('progressed') and record.get('public_delivery_pending'):
            token=record['night_token']
            self.start_job((game.guild_id,'resolution-delivery',token),lambda:self.deliver_deferred_resolution(game,token))
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
                    self.start_job((game.guild_id,'death',receipt['player_id']),deliver)
