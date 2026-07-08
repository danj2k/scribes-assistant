"""Slash command error handler — catches all application command errors
and returns a clean, user-friendly embed instead of Discord's default.

Register via ``setup_error_handler(bot)`` from bot/main.py.

py-cord error hierarchy (relevant subset):
  ApplicationCommandError
    ├── CheckFailure          (permission/role/guild checks failed)
    │     ├── MissingPermissions
    │     └── BotMissingPermissions
    ├── BadArgument           (type coercion failed)
    ├── MissingRequiredArgument
    ├── TooManyArguments
    ├── CommandOnCooldown
    ├── NoPrivateMessage      (guild-only command used in DMs)
    ├── CommandNotFound       (shouldn't reach us, but just in case)
    └── HTTPException         (Discord API failure during respond)

The handler logs the full exception for admin debugging, then sends a
short embed to the user.  We never expose tracebacks or internal paths.
"""
import logging
import discord
from discord.ext import commands

logger = logging.getLogger("scribes.error_handler")


# -- Embed builders --------------------------------------------------------

def _error_embed(title: str, description: str, *, colour: int = 0xE74C3C) -> discord.Embed:
    """Build a red error embed with consistent styling."""
    embed = discord.Embed(
        title=title,
        description=description,
        colour=colour,
    )
    embed.set_footer(text="Scribes Assistant")
    return embed


# -- Pre-built responses for known error types -----------------------------

_CHECK_FAILURE_MSG = (
    "You don't have permission to use this command."
)

_MISSING_PERMS_MSG = (
    "I need additional permissions to do that. "
    "Ask a server admin to check my role permissions."
)

_BOT_MISSING_PERMS_MSG = (
    "I'm missing permissions required to run this command. "
    "Ensure my role has the necessary channel and server permissions."
)

_BAD_ARGUMENT_MSG = (
    "Invalid argument. Please check the command usage and try again."
)

_MISSING_ARGUMENT_MSG = (
    "A required parameter was not provided. Please check the command usage."
)

_TOO_MANY_ARGS_MSG = (
    "Too many arguments were provided. Please check the command usage."
)

_NO_PRIVATE_MSG = (
    "This command can only be used in a server, not in DMs."
)

_ON_COOLDOWN_MSG = (
    "This command is on cooldown. Please try again shortly."
)

_HTTP_EXCEPTION_MSG = (
    "Something went wrong communicating with Discord. Please try again."
)

_GENERIC_MSG = (
    "An unexpected error occurred. The admins have been notified."
)


# -- Error handler ---------------------------------------------------------

async def handle_error(
    ctx: discord.ApplicationContext,
    error: discord.DiscordException,
) -> None:
    """Global on_application_command_error handler.

    Catches every exception from slash commands and sends a user-friendly
    embed.  The original exception is always logged at WARNING or ERROR
    level for admin debugging -- we never surface internal details to users.
    """

    # -- Unwrap if py-cord wrapped it ------------------------------------
    # py-cord sometimes wraps the real error inside a generic
    # ApplicationCommandInvokeError.
    original = getattr(error, "original", None)
    if original is not None:
        error = original

    # -- Per-type handling -----------------------------------------------
    if isinstance(error, commands.BotMissingPermissions):
        logger.warning(
            "Bot missing permissions in guild %s: %s",
            ctx.guild_id,
            error.missing_permissions,
        )
        embed = _error_embed("Missing Permissions", _BOT_MISSING_PERMS_MSG)

    elif isinstance(error, commands.MissingPermissions):
        logger.warning(
            "User %s missing permissions in guild %s: %s",
            ctx.author.id,
            ctx.guild_id,
            error.missing_permissions,
        )
        embed = _error_embed("Permission Denied", _CHECK_FAILURE_MSG)

    elif isinstance(error, commands.NoPrivateMessage):
        logger.warning(
            "NoPrivateMessage for user %s on /%s",
            ctx.author.id,
            ctx.command.qualified_name if ctx.command else "unknown",
        )
        embed = _error_embed("Server Only", _NO_PRIVATE_MSG)

    elif isinstance(error, commands.CheckFailure):
        logger.warning(
            "Check failure for user %s on /%s: %s",
            ctx.author.id,
            ctx.command.qualified_name if ctx.command else "unknown",
            error,
        )
        embed = _error_embed("Permission Denied", _CHECK_FAILURE_MSG)

    elif isinstance(error, commands.MissingRequiredArgument):
        logger.info(
            "Missing required argument '%s' on /%s",
            error.param.name,
            ctx.command.qualified_name if ctx.command else "unknown",
        )
        embed = _error_embed("Missing Parameter", _MISSING_ARGUMENT_MSG)

    elif isinstance(error, commands.BadArgument):
        logger.info(
            "Bad argument on /%s: %s",
            ctx.command.qualified_name if ctx.command else "unknown",
            error,
        )
        embed = _error_embed("Invalid Argument", _BAD_ARGUMENT_MSG)

    elif isinstance(error, commands.TooManyArguments):
        logger.info(
            "Too many arguments on /%s",
            ctx.command.qualified_name if ctx.command else "unknown",
        )
        embed = _error_embed("Too Many Arguments", _TOO_MANY_ARGS_MSG)

    elif isinstance(error, commands.CommandOnCooldown):
        logger.info(
            "Command /%s on cooldown for user %s",
            ctx.command.qualified_name if ctx.command else "unknown",
            ctx.author.id,
        )
        embed = _error_embed("On Cooldown", _ON_COOLDOWN_MSG)

    elif isinstance(error, discord.HTTPException):
        logger.error(
            "HTTPException from Discord API on /%s: %s [%s]",
            ctx.command.qualified_name if ctx.command else "unknown",
            error,
            error.status,
        )
        embed = _error_embed("Discord Error", _HTTP_EXCEPTION_MSG)

    else:
        # Unknown error -- log at ERROR with full traceback, show generic message
        logger.error(
            "Unhandled error on /%s from user %s: %s",
            ctx.command.qualified_name if ctx.command else "unknown",
            ctx.author.id,
            error,
            exc_info=True,
        )
        embed = _error_embed("Something Went Wrong", _GENERIC_MSG)

    # -- Respond --------------------------------------------------------
    try:
        if ctx.response.is_done():
            # Already responded (e.g. deferred) -- use followup
            await ctx.followup.send(embed=embed, ephemeral=True)
        else:
            await ctx.respond(embed=embed, ephemeral=True)
    except discord.HTTPException as send_err:
        logger.error(
            "Failed to send error embed to user %s: %s",
            ctx.author.id,
            send_err,
        )


# -- Registration helper ---------------------------------------------------

def setup_error_handler(bot: commands.Bot) -> None:
    """Register the error handler on the bot.

    Call once from bot/main.py after the bot is created.
    """
    bot.event(handle_error)
    logger.info("Error handler registered")

