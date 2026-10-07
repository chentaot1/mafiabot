"""Complete private-channel ACLs and idempotent member-role reconciliation."""
import discord
from async_work import finish_pending

from config import ALL_MAFIA_ROLES, PLAYING_ROLE_ID, GAME_OVERSEER_ROLE_ID
from game import try_get_bot


def private_overwrites(guild, alive_role):
    me = getattr(guild, 'me', None)
    if me is None:
        client = try_get_bot()
        me = guild.get_member(getattr(getattr(client, 'user', None), 'id', 0))
    if me is None:
        raise RuntimeError('Cannot verify the bot member for private-channel permissions.')
    acl = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
            read_message_history=True, embed_links=True, connect=True, speak=True),
    }
    for role in (alive_role, guild.get_role(PLAYING_ROLE_ID)):
        if role:
            acl[role] = discord.PermissionOverwrite(view_channel=False)
    overseer = guild.get_role(GAME_OVERSEER_ROLE_ID)
    if overseer:
        acl[overseer] = discord.PermissionOverwrite(view_channel=True,
            send_messages=True, read_message_history=True, connect=True, speak=True)
    return acl


async def reconcile(game, guild, guard):
    # Called under the startup barrier. Gameplay controls remain unavailable
    # during recovery, and reset waits for every in-flight request to finish.
    members = {p.id: await game.lookup_member(guild, p.id) for p in list(game.players)}
    guard()
    alive = guild.get_role(game.alive_role_id) if game.alive_role_id else None
    playing = guild.get_role(PLAYING_ROLE_ID)
    lockdown = guild.get_role(game.lockdown_role_id) if game.lockdown_role_id else None
    living = {p.id for p in game.living_players}
    for uid, member in members.items():
        if member is None:
            continue
        staff = member.guild_permissions.administrator or any(r.id == GAME_OVERSEER_ROLE_ID for r in member.roles)
        for role, required in ((alive, uid in living), (playing, True), (lockdown, not staff)):
            if role and required and role not in member.roles:
                await finish_pending(member.add_roles(role))
                guard()
            elif role and not required and role in member.roles:
                await finish_pending(member.remove_roles(role))
                guard()
    if not any((game.mafia_tc_id, game.grave_tc_id, game.grave_vc_id)):
        return
    baseline = private_overwrites(guild, alive)
    for cid, mafia in ((game.mafia_tc_id, True), (game.grave_tc_id, False), (game.grave_vc_id, False)):
        channel = guild.get_channel(cid) if cid else None
        if channel is None:
            continue
        acl = dict(baseline)
        for uid, member in members.items():
            if member and ((mafia and uid in living and game.player_roles.get(uid) in ALL_MAFIA_ROLES)
                    or (not mafia and uid not in living)):
                acl[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                    read_message_history=True, connect=True, speak=True)
        guard()
        await finish_pending(channel.edit(overwrites=acl, reason='Recover game-channel privacy'))
        guard()
