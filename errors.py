import logging

from discord import app_commands
from discord.ext import commands


async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.CommandOnCooldown):
        try:
            await ctx.send(f"⏳ Slow down! Try again in {error.retry_after:.1f} seconds.")
        except Exception:
            pass
    elif isinstance(error, commands.MissingRequiredArgument):
        try:
            await ctx.send("❌ Missing argument. Check command format.")
        except Exception:
            pass
    elif isinstance(error, commands.BadArgument):
        try:
            await ctx.send("❌ Invalid input. Ensure you are typing numbers.")
        except Exception:
            pass
    elif isinstance(error, commands.MissingRole):
        try:
            await ctx.send("🛑 You do not have permission to use this command.")
        except Exception:
            pass
    else:
        logging.error(f"Command error in {ctx.command}: {error}", exc_info=True)


async def on_app_command_tree_error(interaction, error: app_commands.AppCommandError) -> None:
    """B2: log slash/UI failures without dumping secrets (tokens, webhook URLs, raw payloads)."""
    cmd = getattr(interaction, "command", None)
    cmd_name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", None)
    uid = getattr(getattr(interaction, "user", None), "id", None)
    logging.error(
        "App command error cmd=%s interaction_id=%s guild_id=%s channel_id=%s user_id=%s error_type=%s",
        cmd_name,
        getattr(interaction, "id", None),
        getattr(interaction, "guild_id", None),
        getattr(interaction, "channel_id", None),
        uid,
        type(error).__name__,
        exc_info=error,
    )
