"""Slash command handlers for Scribe's Assistant.

Implements /start, /stop, /status, /session, /invite, /help, /lexicon.
"""
import asyncio
from datetime import datetime, timezone
import os

import discord
from discord.commands import SlashCommandGroup, Option
from discord.ext import commands

from shared.database import (
    STATUS_RECORDING,
    STATUS_QUEUED,
    STATUS_TRANSCRIBING,
    STATUS_COMPLETE,
    STATUS_FAILED,
)


def _check_permission(interaction: discord.Interaction, config) -> tuple[bool, str | None]:
    """Check if the user has permission to use a restricted command.

    Returns (allowed, error_message). If allowed is True, the command
    may proceed. If False, error_message should be sent as an ephemeral reply.
    """
    if not config.restrict_commands:
        return True, None

    allowed_roles = config.allowed_roles
    if not allowed_roles:
        return True, None  # Empty list = everyone allowed

    user_role_names = {role.name.lower() for role in interaction.user.roles}
    user_role_ids = {role.id for role in interaction.user.roles}

    for role in allowed_roles:
        if isinstance(role, int) and role in user_role_ids:
            return True, None
        if isinstance(role, str) and role.lower() in user_role_names:
            return True, None

    return False, "You don't have permission to use this command."


def setup_commands(bot: commands.Bot):
    """Register all slash commands with the bot."""

    # --- /start ---
    @bot.slash_command(name="start", description="Start recording your session")
    async def start_command(interaction: discord.Interaction):

        # Permission check
        allowed, error = _check_permission(interaction, bot.config)
        if not allowed:
            await interaction.response.send_message(error, ephemeral=True)
            return

        db = bot.db
        guild_id = str(interaction.guild_id)

        # Check for existing active session
        active = db.get_active_session(guild_id)
        if active:
            await interaction.response.send_message(
                "A recording session is already in progress. Use `/stop` to end it first.",
                ephemeral=True,
            )
            return

        # User must be in a voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel to start recording.",
                ephemeral=True,
            )
            return

        voice_channel = interaction.user.voice.channel

        # Generate session ID from timestamp
        session_id = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")

        # Create database record
        db.create_session(
            session_id=session_id,
            guild_id=guild_id,
            channel_id=str(interaction.channel_id),
        )

        # Join voice channel and start recording
        try:
            vc = await voice_channel.connect()
            # Start recording — py-cord's WaveSink captures per-user WAV audio.
            # We capture the sink reference so the after-callback can access it
            # (py-cord 2.8.0 only passes the exception, not the sink, to the
            # after callback).
            sink = discord.sinks.WaveSink()
            after_cb, recording_done = make_recording_after_callback(
                bot, session_id, guild_id, str(interaction.channel_id), sink,
            )
            vc.start_recording(sink, after_cb)
        except Exception as e:
            db.update_session_status(session_id, STATUS_FAILED)
            await interaction.response.send_message(
                f"Failed to join voice channel: {e}",
                ephemeral=True,
            )
            return

        # Store the voice client reference and recording future for /stop
        if not hasattr(bot, "_voice_clients"):
            bot._voice_clients = {}
        bot._voice_clients[interaction.guild_id] = vc  # type: ignore
        if not hasattr(bot, "_recording_futures"):
            bot._recording_futures = {}
        bot._recording_futures[interaction.guild_id] = recording_done

        await interaction.response.send_message(
            f"Recording started! Session: `{session_id}`\n"
            f"Joined **{voice_channel.name}**. Use `/stop` when you're done.",
        )
        bot.logger.info(f"Session {session_id} started in channel #{interaction.channel}")

    # --- /stop ---
    @bot.slash_command(name="stop", description="Stop recording and queue for transcription")
    async def stop_command(interaction: discord.Interaction):

        # Permission check
        allowed, error = _check_permission(interaction, bot.config)
        if not allowed:
            await interaction.response.send_message(error, ephemeral=True)
            return

        db = bot.db
        guild_id = str(interaction.guild_id)

        active = db.get_active_session(guild_id)
        if not active:
            await interaction.response.send_message(
                "No active recording session.",
                ephemeral=True,
            )
            return

        session_id = active["id"]

        # Stop recording and disconnect
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            vc.stop_recording()
            await vc.disconnect()

        # Wait for the recording callback to finish processing audio files
        # before marking the session as queued. Without this, end_session()
        # races the callback that writes WAV files and registers them in the
        # DB — the transcriber would find zero files and produce an empty
        # transcript.
        recording_future = getattr(bot, "_recording_futures", {}).pop(interaction.guild_id, None)
        if recording_future is not None:
            try:
                await asyncio.wait_for(asyncio.shield(recording_future), timeout=30.0)
            except asyncio.TimeoutError:
                bot.logger.warning(
                    f"Session {session_id}: recording callback timed out after 30s, "
                    "proceeding with whatever files were registered"
                )
            except Exception as e:
                bot.logger.error(f"Session {session_id}: recording callback failed: {e}")

        # Mark session as queued for transcription
        db.end_session(session_id)

        await interaction.response.send_message(
            f"Recording stopped! Session `{session_id}` has been queued for transcription.\n"
            "You'll receive the transcript here when it's ready.",
        )
        bot.logger.info(f"Session {session_id} stopped and queued for transcription")

    # --- /status ---
    @bot.slash_command(name="status", description="Check current session status")
    async def status_command(interaction: discord.Interaction):

        # Permission check
        allowed, error = _check_permission(interaction, bot.config)
        if not allowed:
            await interaction.response.send_message(error, ephemeral=True)
            return

        db = bot.db
        guild_id = str(interaction.guild_id)

        active = db.get_active_session(guild_id)
        if not active:
            # Check for recent queued/transcribing sessions
            sessions = db.get_sessions_for_guild(guild_id, limit=1)
            if sessions:
                s = sessions[0]
                status_emoji = {
                    STATUS_QUEUED: "\u23f3",
                    STATUS_TRANSCRIBING: "\U0001f504",
                    STATUS_COMPLETE: "\u2705",
                    STATUS_FAILED: "\u274c",
                }.get(s["status"], "\u2753")

                await interaction.response.send_message(
                    f"No active recording.\n"
                    f"Latest session: `{s['id']}` — {status_emoji} {s['status'].title()}",
                )
            else:
                await interaction.response.send_message(
                    "No active recording session. Use `/start` to begin one.",
                )
            return

        # Calculate duration
        started = datetime.fromisoformat(active["started_at"])
        duration = datetime.now(timezone.utc) - started
        minutes = int(duration.total_seconds() // 60)
        seconds = int(duration.total_seconds() % 60)

        await interaction.response.send_message(
            f"**Recording in progress**\n"
            f"Session: `{active['id']}`\n"
            f"Duration: {minutes}m {seconds}s",
        )

    # --- /session ---
    @bot.slash_command(name="session", description="List previous sessions")
    async def session_command(interaction: discord.Interaction):

        # Permission check
        allowed, error = _check_permission(interaction, bot.config)
        if not allowed:
            await interaction.response.send_message(error, ephemeral=True)
            return

        db = bot.db
        guild_id = str(interaction.guild_id)

        sessions = db.get_sessions_for_guild(guild_id, limit=10)
        if not sessions:
            await interaction.response.send_message(
                "No sessions found for this server.",
                ephemeral=True,
            )
            return

        lines = ["**Recent Sessions:**\n"]
        for s in sessions:
            status_emoji = {
                STATUS_RECORDING: "\U0001f534",
                STATUS_QUEUED: "\u23f3",
                STATUS_TRANSCRIBING: "\U0001f504",
                STATUS_COMPLETE: "\u2705",
                STATUS_FAILED: "\u274c",
            }.get(s["status"], "\u2753")

            lines.append(f"{status_emoji} `{s['id']}` — {s['status'].title()}")

        await interaction.response.send_message("\n".join(lines))

    # --- /invite ---
    @bot.slash_command(name="invite", description="Get a link to invite the bot to another server")
    async def invite_command(interaction: discord.Interaction):
        if not bot.user:
            return

        invite_url = (
            f"https://discord.com/api/oauth2/authorize"
            f"?client_id={bot.user.id}"
            f"&scope=bot%20applications.commands"
            f"&permissions=2147503104"
        )

        await interaction.response.send_message(
            f"[Click here to invite Scribe's Assistant to your server]({invite_url})",
            ephemeral=True,
        )

    # --- /help ---
    @bot.slash_command(name="help", description="Show available commands")
    async def help_command(interaction: discord.Interaction):
        embed = discord.Embed(
            title="Scribe's Assistant — Commands",
            description="Automated transcription for tabletop RPG sessions.",
            color=discord.Color.blue(),
        )

        commands_list = [
            ("`/start`", "Start recording. Bot joins your voice channel."),
            ("`/stop`", "Stop recording. Queues session for transcription."),
            ("`/status`", "Check current session status and recording duration."),
            ("`/session`", "List previous sessions and their status."),
            ("`/lexicon add term:X description:Y`", "Add a word to the transcription lexicon."),
            ("`/lexicon list`", "Show all lexicon words."),
            ("`/lexicon remove term:X`", "Remove a word from the lexicon."),
            ("`/invite`", "Get a link to invite the bot to another server."),
        ]

        for name, desc in commands_list:
            embed.add_field(name=name, value=desc, inline=False)

        embed.set_footer(text="Transcripts are delivered as threads in this channel.")
        await interaction.response.send_message(embed=embed)

    # --- /lexicon ---
    lexicon_group = SlashCommandGroup(name="lexicon", description="Manage the transcription lexicon")

    @lexicon_group.command(name="add", description="Add a word to the transcription lexicon")
    async def lexicon_add(interaction: discord.Interaction, term: Option(str, description="The word to add"), description: Option(str, description="What this word means")):

        # Permission check
        allowed, error = _check_permission(interaction, bot.config)
        if not allowed:
            await interaction.response.send_message(error, ephemeral=True)
            return

        if not bot.config.lexicon_enabled:
            await interaction.response.send_message(
                "Lexicon features are disabled.",
                ephemeral=True,
            )
            return

        # Add to YAML lexicon file (authoritative store)
        bot.lexicon.add_term(term, description)
        bot.lexicon.save()

        await interaction.response.send_message(
            f"Added **{term}** to the lexicon: {description}",
        )
        bot.logger.info(f"Lexicon: added '{term}' by {interaction.user}")

    @lexicon_group.command(name="list", description="Show all lexicon words")
    async def lexicon_list(interaction: discord.Interaction):

        # Permission check
        allowed, error = _check_permission(interaction, bot.config)
        if not allowed:
            await interaction.response.send_message(error, ephemeral=True)
            return

        if not bot.config.lexicon_enabled:
            await interaction.response.send_message(
                "Lexicon features are disabled.",
                ephemeral=True,
            )
            return

        terms = bot.lexicon.get_terms_list()

        if not terms:
            await interaction.response.send_message(
                "The lexicon is empty. Use `/lexicon add` to add words.",
                ephemeral=True,
            )
            return

        lines = ["**Lexicon:**\n"]
        for entry in bot.lexicon.terms.values():
            lines.append(f"\u2022 **{entry['term']}** — {entry['description']}")

        await interaction.response.send_message("\n".join(lines))

    @lexicon_group.command(name="remove", description="Remove a word from the lexicon")
    async def lexicon_remove(interaction: discord.Interaction, term: Option(str, description="The word to remove")):

        # Permission check
        allowed, error = _check_permission(interaction, bot.config)
        if not allowed:
            await interaction.response.send_message(error, ephemeral=True)
            return

        if not bot.config.lexicon_enabled:
            await interaction.response.send_message(
                "Lexicon features are disabled.",
                ephemeral=True,
            )
            return

        # Check if term exists before removing
        key = term.lower()
        if key not in bot.lexicon.terms:
            await interaction.response.send_message(
                f"**{term}** was not found in the lexicon.",
                ephemeral=True,
            )
            return

        bot.lexicon.remove_term(term)
        bot.lexicon.save()

        await interaction.response.send_message(
            f"Removed **{term}** from the lexicon.",
        )
        bot.logger.info(f"Lexicon: removed '{term}' by {interaction.user}")

    bot.add_application_command(lexicon_group)


def make_recording_after_callback(bot, session_id, guild_id, channel_id, sink):
    """Create a py-cord after-callback for recording completion.

    py-cord 2.8.0's ``after`` callback is a synchronous callable that receives
    a single ``exception`` argument — it does **not** pass the sink.  We close
    over the sink here so that the async processing logic can access it.

    The callback may be invoked from a non-event-loop thread (py-cord's socket
    listener or the AudioReader._stop() path), so we use
    :func:`asyncio.run_coroutine_threadsafe` to schedule the async audio
    processing on the bot's event loop.

    Returns ``(after_cb, future)`` where ``after_cb`` is the sync callback to
    pass to ``vc.start_recording()`` and ``future`` is an
    :class:`asyncio.Future` that resolves once audio processing is complete.
    The caller (typically ``/stop``) should await ``future`` before marking the
    session as queued, so that audio files are registered in the DB before the
    transcriber picks up the session.
    """
    loop = asyncio.get_running_loop()
    # Future shared between the sync callback and the async processing task.
    # Set when audio processing finishes (or errors out).
    done_future: asyncio.Future = loop.create_future()

    async def _process_recording(exc: Exception | None):
        """Process recorded audio and save to disk."""
        try:
            if exc is not None:
                bot.logger.error(
                    f"Session {session_id}: recording stopped with error: {exc}"
                )
                # Still try to save whatever audio was captured
            db = bot.db

            # Resolve guild for member lookup — needed to map user IDs
            # to display names. The transcriber has no Discord API access,
            # so the bot must store speaker identity in the DB now.
            guild = bot.get_guild(int(guild_id)) if hasattr(bot, "get_guild") else None

            # Ensure recording directory exists
            rec_dir = f"/data/recordings/{session_id}"
            os.makedirs(rec_dir, exist_ok=True)

            audio_count = 0
            for user_id, audio_data in sink.audio_data.items():
                filepath = f"{rec_dir}/{user_id}.wav"

                # audio_data is a BytesIO object containing WAV data
                with open(filepath, "wb") as f:
                    f.write(audio_data.getbuffer())

                # Resolve speaker display name from guild member cache.
                # user_id is an int (Discord user ID); fall back to str(id)
                # if the member has left the guild or the cache is cold.
                speaker_name = str(user_id)
                if guild is not None:
                    member = guild.get_member(user_id)
                    if member is not None:
                        speaker_name = member.display_name

                size = audio_data.getbuffer().nbytes
                db.add_audio_file(
                    session_id=session_id,
                    filepath=filepath,
                    size_bytes=size,
                    discord_user_id=str(user_id),
                    speaker_name=speaker_name,
                )
                audio_count += 1

            bot.logger.info(
                f"Session {session_id}: saved {audio_count} audio file(s) to {rec_dir}"
            )
        except Exception as e:
            bot.logger.error(f"Session {session_id}: recording callback failed: {e}")
            raise
        finally:
            # Signal the future regardless of success/failure so /stop
            # doesn't hang forever waiting.
            if not done_future.done():
                done_future.set_result(None)

    def after_cb(exc: Exception | None) -> None:
        """Synchronous after-callback for py-cord.

        Schedules async audio processing on the event loop. This may be
        called from a non-loop thread, hence run_coroutine_threadsafe.
        """
        asyncio.run_coroutine_threadsafe(_process_recording(exc), loop)

    return after_cb, done_future
