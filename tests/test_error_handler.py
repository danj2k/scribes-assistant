"""Tests for bot/error_handler — slash command error handler.

All tests use mocks; no Discord connection required.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands

from bot.error_handler import (
    handle_error,
    setup_error_handler,
    _error_embed,
    _CHECK_FAILURE_MSG,
    _BOT_MISSING_PERMS_MSG,
    _BAD_ARGUMENT_MSG,
    _MISSING_ARGUMENT_MSG,
    _TOO_MANY_ARGS_MSG,
    _NO_PRIVATE_MSG,
    _ON_COOLDOWN_MSG,
    _HTTP_EXCEPTION_MSG,
    _GENERIC_MSG,
)


# -- Helpers ----------------------------------------------------------------

def _make_ctx(command_name="start"):
    """Build a mock ApplicationContext with all needed async methods."""
    ctx = AsyncMock(spec=discord.ApplicationContext)
    ctx.command = MagicMock()
    ctx.command.qualified_name = command_name
    ctx.guild_id = 123456
    ctx.author = MagicMock()
    ctx.author.id = 789012
    # response.is_done() must return False by default so handler uses respond
    ctx.response.is_done.return_value = False
    # followup must be awaitable
    ctx.followup = MagicMock()
    ctx.followup.send = AsyncMock()
    return ctx


def _make_check_failure():
    return commands.CheckFailure()


def _make_missing_permissions(perm_list):
    err = commands.MissingPermissions(perm_list)
    err.missing_permissions = perm_list
    return err


def _make_bot_missing_permissions(perm_list):
    err = commands.BotMissingPermissions(perm_list)
    err.missing_permissions = perm_list
    return err


def _make_bad_argument():
    return commands.BadArgument("Invalid integer: abc")


def _make_missing_required_argument():
    param = MagicMock()
    param.name = "session_id"
    return commands.MissingRequiredArgument(param)


def _make_too_many_arguments():
    return commands.TooManyArguments("Too many arguments")


def _make_command_on_cooldown():
    bucket = commands.BucketType.user
    cooldown = commands.Cooldown(1, 10.0)
    return commands.CommandOnCooldown(cooldown=cooldown, retry_after=5.0, type=bucket)


def _make_no_private_message():
    return commands.NoPrivateMessage()


def _make_http_exception(status=500):
    err = discord.HTTPException(response=MagicMock(), message="Internal Server Error")
    err.status = status
    return err


# -- Tests: embed builder ---------------------------------------------------

class TestErrorEmbed:
    """Tests for _error_embed helper."""

    def test_creates_embed_with_defaults(self):
        embed = _error_embed("Error", "Something went wrong")
        assert embed.title == "Error"
        assert embed.description == "Something went wrong"
        assert embed.colour.value == 0xE74C3C
        assert embed.footer.text == "Scribes Assistant"

    def test_custom_colour(self):
        embed = _error_embed("Info", "A note", colour=0x3498DB)
        assert embed.colour.value == 0x3498DB

    def test_footer_always_present(self):
        embed = _error_embed("T", "D")
        assert "Scribes Assistant" in embed.footer.text


# -- Tests: error messages are non-empty ------------------------------------

class TestErrorMessages:
    """Sanity-check that all message constants are non-empty strings."""

    def test_all_messages_exist(self):
        msgs = [
            _CHECK_FAILURE_MSG,
            _BOT_MISSING_PERMS_MSG,
            _BAD_ARGUMENT_MSG,
            _MISSING_ARGUMENT_MSG,
            _TOO_MANY_ARGS_MSG,
            _NO_PRIVATE_MSG,
            _ON_COOLDOWN_MSG,
            _HTTP_EXCEPTION_MSG,
            _GENERIC_MSG,
        ]
        for msg in msgs:
            assert isinstance(msg, str)
            assert len(msg) > 0


# -- Tests: handle_error per error type -------------------------------------

@pytest.mark.asyncio
class TestHandleError:
    """Async tests for the main handle_error function."""

    async def test_check_failure_sends_permission_denied(self):
        ctx = _make_ctx()
        err = _make_check_failure()
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Permission Denied"
        assert embed.description == _CHECK_FAILURE_MSG
        assert ctx.respond.call_args.kwargs["ephemeral"] is True

    async def test_missing_permissions_sends_permission_denied(self):
        ctx = _make_ctx()
        err = _make_missing_permissions(["manage_channels"])
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Permission Denied"

    async def test_bot_missing_permissions_sends_missing_permissions(self):
        ctx = _make_ctx()
        err = _make_bot_missing_permissions(["connect"])
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Missing Permissions"
        assert embed.description == _BOT_MISSING_PERMS_MSG

    async def test_bad_argument_sends_invalid_argument(self):
        ctx = _make_ctx()
        err = _make_bad_argument()
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Invalid Argument"

    async def test_missing_required_argument_sends_missing_parameter(self):
        ctx = _make_ctx()
        err = _make_missing_required_argument()
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Missing Parameter"

    async def test_too_many_arguments_sends_too_many(self):
        ctx = _make_ctx()
        err = _make_too_many_arguments()
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Too Many Arguments"

    async def test_command_on_cooldown_sends_cooldown(self):
        ctx = _make_ctx()
        err = _make_command_on_cooldown()
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "On Cooldown"

    async def test_no_private_message_sends_server_only(self):
        ctx = _make_ctx()
        err = _make_no_private_message()
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Server Only"

    async def test_http_exception_sends_discord_error(self):
        ctx = _make_ctx()
        err = _make_http_exception(500)
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Discord Error"

    async def test_unknown_error_sends_generic(self):
        ctx = _make_ctx()
        err = ValueError("some internal bug")
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Something Went Wrong"

    async def test_always_ephemeral(self):
        """Error responses are always ephemeral so only the user sees them."""
        ctx = _make_ctx()
        err = _make_check_failure()
        await handle_error(ctx, err)
        assert ctx.respond.call_args.kwargs["ephemeral"] is True


# -- Tests: deferred interaction (is_done) -----------------------------------

@pytest.mark.asyncio
class TestDeferredInteraction:
    """When interaction is already responded to, use followup."""

    async def test_uses_followup_when_already_responded(self):
        ctx = _make_ctx()
        ctx.response.is_done.return_value = True
        err = _make_check_failure()
        await handle_error(ctx, err)
        ctx.followup.send.assert_awaited_once()
        ctx.respond.assert_not_awaited()
        embed = ctx.followup.send.call_args.kwargs["embed"]
        assert embed.title == "Permission Denied"

    async def test_followup_also_ephemeral(self):
        ctx = _make_ctx()
        ctx.response.is_done.return_value = True
        err = _make_bad_argument()
        await handle_error(ctx, err)
        assert ctx.followup.send.call_args.kwargs["ephemeral"] is True


# -- Tests: error unwrapping ------------------------------------------------

@pytest.mark.asyncio
class TestErrorUnwrapping:
    """Test that CommandInvokeError.original is unwrapped."""

    async def test_unwraps_original_error(self):
        ctx = _make_ctx()
        real_err = _make_bad_argument()
        wrapper = commands.CommandInvokeError(real_err)
        await handle_error(ctx, wrapper)
        # Should have handled it as BadArgument, not generic
        ctx.respond.assert_awaited_once()
        embed = ctx.respond.call_args.kwargs["embed"]
        assert embed.title == "Invalid Argument"


# -- Tests: send failure logging --------------------------------------------

@pytest.mark.asyncio
class TestSendFailure:
    """When sending the error embed itself fails, log but don't crash."""

    async def test_log_on_send_failure(self):
        ctx = _make_ctx()
        ctx.respond.side_effect = discord.HTTPException(
            response=MagicMock(), message="rate limited"
        )
        err = _make_check_failure()
        # Should NOT raise
        await handle_error(ctx, err)


# -- Tests: setup function --------------------------------------------------

class TestSetupErrorHandler:
    """Test that setup_error_handler registers the handler on the bot."""

    def test_registers_handler(self):
        bot = MagicMock()
        setup_error_handler(bot)
        bot.event.assert_called_once_with(handle_error)


# -- Tests: command name in context -----------------------------------------

@pytest.mark.asyncio
class TestCommandContext:
    """Verify the handler uses ctx.command.qualified_name safely."""

    async def test_handles_none_command(self):
        ctx = _make_ctx()
        ctx.command = None
        # Should fall back to "unknown" and not crash
        err = _make_check_failure()
        await handle_error(ctx, err)
        ctx.respond.assert_awaited_once()
