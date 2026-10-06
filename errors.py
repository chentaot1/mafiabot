import logging

import discord
from discord import app_commands
from discord.ext import commands


async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    from bot_app.shared import safe_reply

    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        # Checks usually reply themselves; this covers silent custom checks.
        await safe_reply(ctx, "🛑 That command isn't available in this situation.")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await safe_reply(ctx, f"⏳ Slow down! Try again in {error.retry_after:.1f} seconds.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await safe_reply(ctx, "❌ Missing argument. Check command format.")
    elif isinstance(error, commands.BadArgument):
        await safe_reply(ctx, "❌ Invalid input. Ensure you are typing numbers.")
    elif isinstance(error, commands.MissingRole):
        await safe_reply(ctx, "🛑 You do not have permission to use this command.")
    else:
        # Audit M5 — log AND surface a generic best-effort user message so
        # users don't see an apparent no-op when an internal error fires.
        # We never leak stack traces (logged server-side only).
        logging.error(f"Command error in {ctx.command}: {error}", exc_info=True)
        await safe_reply(
            ctx,
            "⚠️ Something went wrong running that command. The GM has been notified in the logs.",
        )


async def on_app_command_tree_error(interaction, error: app_commands.AppCommandError) -> None:
    """B2: log slash/UI failures without dumping secrets (tokens, webhook URLs, raw payloads).
    Audit M6 — also respond to the interaction so users see a clear message
    instead of Discord's generic 'this interaction failed'."""
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

    # User-facing response. The interaction may already have been responded
    # to (e.g., via defer), so we fall back to followup.send. Both wrapped
    # in best-effort try/except to avoid raising inside the error handler.
    from bot_app.shared import safe_interaction_ephemeral

    if not await safe_interaction_ephemeral(
        interaction,
        "⚠️ Something went wrong running that command. The GM has been notified in the logs.",
    ):
        logging.debug("on_app_command_tree_error could not notify user interaction_id=%s", getattr(interaction, "id", None))
