"""Voice state management for Scribe's Assistant.

Handles auto-disconnect when the bot is left alone in a voice channel,
implementing the idle timeout feature.
"""

import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands

from shared.database import STATUS_RECORDING


# Track idle check tasks per guild
_idle_tasks: dict[int, asyncio.Task] = {}


async def on_voice_state_update(
    bot: commands.Bot,
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """Handle voice state changes.
    
    When the bot is left alone in a voice channel, start an idle timer.
    When a user joins, cancel the timer. When the timer expires, end the
    session and disconnect.
    """
    # Ignore bot's own voice state changes
    if member.bot:
        return
    
    guild = member.guild
    if not guild.voice_client:
        return
    
    # Check if the bot is in a voice channel in this guild
    bot_vc = guild.voice_client
    if not bot_vc or not bot_vc.channel:
        return
    
    # Count non-bot members in the bot's channel
    members_in_channel = [
        m for m in bot_vc.channel.members if not m.bot
    ]
    
    if len(members_in_channel) == 0:
        # Bot is alone — start idle timer
        bot.logger.info(
            f"Bot alone in {bot_vc.channel.name}, starting idle timer "
            f"({bot.config.idle_timeout}s)"
        )
        _idle_tasks[guild.id] = asyncio.create_task(
            _idle_timeout(bot, guild, bot_vc)
        )
    else:
        # User joined — cancel idle timer if running
        if guild.id in _idle_tasks:
            _idle_tasks[guild.id].cancel()
            del _idle_tasks[guild.id]
            bot.logger.info("User joined, cancelling idle timer")


async def _idle_timeout(bot, guild, voice_client):
    try:
        await asyncio.sleep(bot.config.idle_timeout)

        # Session is still active — end it
        db = bot.db
        guild_id = str(guild.id)
        active = db.get_active_session(guild_id)

        if active:
            session_id = active["id"]

            # Stop recording — triggers the recording callback
            if voice_client.is_recording():
                voice_client.stop_recording()

            # Await the recording future so WAV files finish writing
            # before we disconnect and end the session.  Mirrors the
            # same pattern used in /stop (commands.py) to avoid the
            # race between the recording callback writing files and
            # end_session() queuing the session for transcription.
            recording_future = getattr(bot, "_recording_futures", {}).pop(guild.id, None)
            audio_count = None
            if recording_future is not None:
                try:
                    audio_count = await asyncio.wait_for(
                        asyncio.shield(recording_future), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    bot.logger.warning(
                        f"Session {session_id}: recording callback timed out "
                        f"after 30s during idle timeout, proceeding with "
                        "whatever files were registered"
                    )
                except Exception as e:
                    bot.logger.error(
                        f"Session {session_id}: recording callback failed "
                        f"during idle timeout: {e}"
                    )

            # Disconnect
            if voice_client.is_connected():
                await voice_client.disconnect()

            # If the callback detected zero audio files (nobody spoke),
            # mark as failed so the session doesn't block future recordings.
            if not audio_count:
                if audio_count is None:
                    # Callback never fired — ensure session is marked failed
                    # so it doesn't linger as ACTIVE.
                    db.fail_session(session_id)
                else:
                    # Zero audio files; callback already called fail_session()
                    pass

                bot.logger.info(
                    f"Session {session_id}: idle timeout with no audio "
                    f"(count={audio_count}), marked as failed"
                )
            else:
                # Mark as queued for transcription
                db.end_session(session_id)
                bot.logger.info(
                    f"Session {session_id} auto-ended after idle timeout"
                )

            # Notify the channel
            channel = guild.get_channel(int(active["discord_channel_id"]))
            if channel:
                msg = (
                    f"Session `{session_id}` ended automatically "
                    f"(bot was idle for {bot.config.idle_timeout}s)."
                )
                if not audio_count:
                    msg += " Nobody spoke — no transcript will be generated."
                else:
                    msg += " Queued for transcription."
                await channel.send(msg)
        else:
            # No active session, just disconnect
            if voice_client.is_connected():
                await voice_client.disconnect()

    except asyncio.CancelledError:
        pass
    finally:
        _idle_tasks.pop(guild.id, None)