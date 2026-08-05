"""Slash command handlers for Scribe's Assistant.

Implements /start, /stop, /status, /session, /invite, /help, /lexicon.
"""
import asyncio
import logging
from datetime import datetime, timezone
import os

import discord
from discord.commands import SlashCommandGroup, Option
from discord.ext import commands

from bot.timestamped_sink import TimestampedWaveSink
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

        # Enforce single-session-only: this bot serves one D&D group, so
        # only one recording session should be active at any time — across
        # all guilds. This also eliminates the session ID collision risk
        # (two starts in the same UTC second would produce the same ID).
        active = db.get_any_active_session()
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

        # Defer the interaction before the voice channel join. Discord
        # requires an interaction response within 3 seconds; voice join
        # involves a gateway state change, WebSocket handshake, and
        # encryption key exchange that can exceed this deadline under
        # high latency. After deferring, all responses must use
        # followup.send() instead of response.send_message().
        await interaction.response.defer(ephemeral=True)

        # Join voice channel and start recording
        try:
            vc = await voice_channel.connect()

            # DAVE (Discord Audio/Video End-to-End Encryption) is enforced
            # on all non-stage voice channels since March 2026.  After the
            # WebSocket voice handshake completes, py-cord starts an
            # asynchronous MLS key exchange: it sends a key package, Discord
            # responds with binary MLS proposals/commits, and only once
            # process_commit()/process_welcome() succeeds does the davey
            # session transition to "active" (ready=True).
            #
            # If we call start_recording before that handshake completes,
            # the audio reader receives DAVE-encrypted packets, cannot
            # decrypt them, and feeds garbage to the Opus decoder — which
            # raises OpusError("corrupted stream") and crashes the router.
            #
            # We poll dave_session.ready with a timeout.  If the channel
            # is not DAVE-enabled (dave_session is None), we skip the wait.
            DAVE_WAIT_TIMEOUT = 15  # seconds
            DAVE_POLL_INTERVAL = 0.25  # seconds

            dave_session = vc._connection.dave_session
            if dave_session is not None:
                bot.logger.info(
                    f"DAVE session detected, waiting up to {DAVE_WAIT_TIMEOUT}s "
                    f"for MLS handshake to complete..."
                )
                elapsed = 0.0
                while not dave_session.ready:
                    if elapsed >= DAVE_WAIT_TIMEOUT:
                        bot.logger.warning(
                            f"DAVE handshake did not complete within "
                            f"{DAVE_WAIT_TIMEOUT}s (status={dave_session.status}), "
                            f"proceeding anyway — audio may fail"
                        )
                        break
                    await asyncio.sleep(DAVE_POLL_INTERVAL)
                    elapsed += DAVE_POLL_INTERVAL

                if dave_session.ready:
                    bot.logger.info(
                        f"DAVE handshake completed after {elapsed:.1f}s, "
                        f"starting recording"
                    )
                else:
                    # DAVE never became ready.  Disconnect and abort rather
                    # than feeding encrypted packets to the Opus decoder.
                    await vc.disconnect(force=True)
                    db.fail_session(session_id)
                    await interaction.followup.send(
                        "Could not establish a secure (DAVE) connection to "
                        "Discord's voice server within "
                        f"{DAVE_WAIT_TIMEOUT} seconds. Please try again.",
                        ephemeral=True,
                    )
                    return

            # Start recording — TimestampedWaveSink pads silence for DTX
            # gaps and initial offsets so all speakers share the same
            # timeline.  The base WaveSink simply appends PCM as it
            # arrives, which means each user's file starts at their first
            # speech and all pauses are removed — making timestamps
            # incomparable across speakers and scrambling dialogue order.
            # See bot/timestamped_sink.py for full details.
            #
            # We pass a dummy positional arg (None) because PR #3159's
            # AudioReader._stop() only fires the callback if self.args is
            # truthy: ``if self.after and self.args:``.  Without any *args,
            # self.args is an empty tuple (falsy) and the callback never runs.
            # The dummy arg is harmless — it's forwarded as *args to
            # after_cb, which ignores them.
            sink = TimestampedWaveSink()
            after_cb, recording_done = make_recording_after_callback(
                bot, session_id, guild_id, str(interaction.channel_id), sink,
            )
            vc.start_recording(sink, after_cb, None)
        except Exception as e:
            # fail_session() sets BOTH status=FAILED and ended_at. Using
            # update_session_status() here would leave ended_at=NULL, causing
            # get_active_session() to keep returning this dead session and
            # blocking all future recordings in the guild.
            db.fail_session(session_id)
            await interaction.followup.send(
                f"Failed to join voice channel: {e}",
                ephemeral=True,
            )
            return

        # Store the recording future for /stop
        if not hasattr(bot, "_recording_futures"):
            bot._recording_futures = {}
        bot._recording_futures[interaction.guild_id] = recording_done

        await interaction.followup.send(
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

        # Acknowledge the interaction immediately — Discord interactions
        # expire after 3 seconds, but the recording callback (which writes
        # WAV files and registers them in the DB) can take much longer.
        # We'll send the final result as a followup.
        await interaction.response.send_message(
            f"Stopping recording for session `{session_id}`... "
            "Processing audio files, please wait.",
        )

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
        audio_count = None
        if recording_future is not None:
            try:
                audio_count = await asyncio.wait_for(asyncio.shield(recording_future), timeout=30.0)
            except asyncio.TimeoutError:
                bot.logger.warning(
                    f"Session {session_id}: recording callback timed out after 30s, "
                    "proceeding with whatever files were registered"
                )
            except Exception as e:
                bot.logger.error(f"Session {session_id}: recording callback failed: {e}")

        # If the callback detected zero audio files (nobody spoke), it has
        # already called fail_session(). Inform the user and don't queue
        # for transcription.  audio_count is None when the callback timed
        # out entirely (never fired) — treat that the same as zero audio.
        if not audio_count:
            if audio_count is None:
                # Callback never fired — ensure the session is marked failed
                # so it doesn't linger as ACTIVE.
                db.fail_session(session_id)
            await interaction.followup.send(
                f"Recording stopped for session `{session_id}`, but no audio was captured "
                "(nobody spoke, or the recording callback did not fire). "
                "The session has been marked as failed — no transcript will be generated.",
                ephemeral=True,
            )
            bot.logger.info(f"Session {session_id}: stopped with no audio (count={audio_count}), marked as failed")
            return

        # Mark session as queued for transcription
        db.end_session(session_id)

        # Build the followup message.  When a dedicated transcript channel
        # is configured, tell the user where to look instead of implying the
        # current channel.
        delivery_channel_id = bot.config.transcript_channel_id
        if delivery_channel_id:
            channel = bot.get_channel(delivery_channel_id)
            channel_name = channel.name if channel else f"<#{delivery_channel_id}>"
            await interaction.followup.send(
                f"Recording stopped! Session `{session_id}` has been queued for transcription.\n"
                f"You'll receive the transcript in **#{channel_name}** when it's ready.",
            )
        else:
            await interaction.followup.send(
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

        # Permission check — generating a bot invite URL is an admin action,
        # not something regular users should be able to do.
        allowed, error = _check_permission(interaction, bot.config)
        if not allowed:
            await interaction.response.send_message(error, ephemeral=True)
            return

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


def _sanitize_filename(name: str) -> str:
    """Make a string safe for use as a filename.

    Discord display names can contain slashes, parentheses, and other
    characters that are problematic on the filesystem (e.g. a '/' in
    'Duckinell/DM' is interpreted as a path separator, creating a
    spurious directory).  Replace any character that is unsafe on any
    common filesystem with an underscore.
    """
    # Characters forbidden on Windows, plus '/' and '\' on Unix.
    unsafe = '<>:"/\\|?*\0'
    result = "".join("_" if c in unsafe else c for c in name)
    # Collapse leading/trailing spaces and dots (problematic on Windows).
    result = result.strip(" .")
    # Collapse runs of underscores so 'Duckinell/DM' -> 'Duckinell_DM'
    # rather than 'Duckinell_DM' (already fine, but handles cases like
    # 'a//b' -> 'a__b' -> we don't want double underscores).
    while "__" in result:
        result = result.replace("__", "_")
    # Fallback if the name was entirely unsafe characters.
    return result or "unknown"


def _write_wav_file(filepath: str, wav_bytes: bytes, temp_path: str) -> None:
    """Write WAV data to the final path and remove the raw-PCM temp file.

    Args:
        filepath: Final output path (with proper WAV header).
        wav_bytes: BytesIO content from WaveSink.format_audio() —
            contains a proper WAV header followed by PCM frames.
        temp_path: Temporary file with raw PCM (no header) — deleted
            after the WAV file is written.

    This runs in a thread pool (``asyncio.to_thread``) so disk I/O
    does not block the event loop on large files.
    """
    with open(filepath, "wb") as f:
        f.write(wav_bytes)
    # Remove the raw-PCM temp file — no longer needed.
    try:
        os.unlink(temp_path)
    except OSError:
        pass


def make_recording_after_callback(bot, session_id, guild_id, channel_id, sink):
    """Create a py-cord after-callback for recording completion.

    PR #3159's ``after`` callback is a synchronous callable that receives
    the sink as its first positional argument (``after(sink, *args)``),
    not an exception.  We close over the sink here so that the async
    processing logic can access it.

    The callback may be invoked from a non-event-loop thread (py-cord's socket
    listener or the AudioReader._stop() path), so we use
    :func:`asyncio.run_coroutine_threadsafe` to schedule the async audio
    processing on the bot's event loop.

    PR #3159's _stop() only fires the callback if ``self.after and self.args``
    are both truthy — the call site must pass at least one positional arg
    to start_recording() so self.args is non-empty.

    Returns ``(after_cb, future)`` where ``after_cb`` is the sync callback to
    pass to ``vc.start_recording()`` and ``future`` is an
    :class:`asyncio.Future` that resolves once audio processing is complete.
    The caller (typically ``/stop``) should await ``future`` before marking the
    session as queued, so that audio files are registered in the DB before the
    transcriber picks up the session.
    """
    loop = asyncio.get_running_loop()
    # Future shared between the sync callback and the async processing task.
    # The result is the number of audio files saved (0 means empty recording).
    # Set when audio processing finishes (or errors out).
    done_future: asyncio.Future = loop.create_future()
    # py-cord 2.8.0 may call after_cb twice — guard against double processing.
    _processing_started = False

    async def _process_recording(exc: Exception | None):
        """Process recorded audio and save to disk."""
        nonlocal _processing_started
        _processing_started = True
        audio_count = 0
        try:
            if exc is not None:
                bot.logger.error(
                    f"Session {session_id}: recording stopped with error: {exc}"
                )
                # Still try to save whatever audio was captured
            db = bot.db

            # PR #3159's AudioReader._stop() calls sink.cleanup() after the
            # callback — this calls audio_data.cleanup() + format_audio()
            # for each AudioData, finalising WAV headers and seeking the
            # temp file to 0.  We must NOT call cleanup() again here —
            # AudioData.cleanup() raises if already finished.
            # Resolve guild for member lookup — needed to map user IDs
            # to display names. The transcriber has no Discord API access,
            # so the bot must store speaker identity in the DB now.
            guild = bot.get_guild(int(guild_id)) if hasattr(bot, "get_guild") else None

            rec_dir = f"/data/recordings/{session_id}"

            # Build the list of files to rename. Speaker name resolution
            # touches the guild member cache (fast, no I/O). The sink
            # writes PCM directly to temporary files on disk as it
            # arrives, so renaming them to their final paths is instant
            # (filesystem metadata only) — no need for asyncio.to_thread.
            file_specs = []  # (filepath, speaker_name, user_obj)
            speaker_info = []  # (discord_user_id, speaker_name, size_bytes)
            for user_obj, audio_data in sink.audio_data.items():
                # pycord keys audio_data by User/Member objects, not IDs.
                # Use the member's display name (sanitised) for the filename
                # so files are human-readable, but strip path separators and
                # other unsafe characters first — a display name like
                # 'Duckinell/DM' would otherwise create a spurious directory.
                speaker_name = str(user_obj)  # fallback: 'username#1234'
                if guild is not None:
                    member = guild.get_member(user_obj)
                    if member is not None:
                        speaker_name = member.display_name

                safe_name = _sanitize_filename(speaker_name)
                filepath = f"{rec_dir}/{safe_name}.wav"
                temp_path = sink._temp_paths.get(user_obj)
                file_specs.append((filepath, temp_path, user_obj))

                speaker_info.append(
                    (str(user_obj.id), speaker_name, 0)  # size filled after rename
                )

            # Write WAV-formatted audio to the final path.  After
            # sink.cleanup() runs (triggered by AudioReader._stop()),
            # WaveSink.format_audio() has replaced each AudioData's
            # internal file with a BytesIO containing proper WAV headers
            # + PCM data.  The original temp file on disk still contains
            # only raw PCM — renaming it to .wav would produce a file
            # soundfile/libsoundfile rejects with "Format not recognised."
            os.makedirs(rec_dir, exist_ok=True)
            audio_count = 0
            for i, (filepath, temp_path, user_obj) in enumerate(file_specs):
                if temp_path is None or not os.path.exists(temp_path):
                    bot.logger.warning(
                        f"Session {session_id}: no temp file for {user_obj}, skipping"
                    )
                    continue
                try:
                    # Write the WAV-formatted BytesIO data to the final
                    # path.  The audio_data.file is a BytesIO (replaced
                    # by WaveSink.format_audio() during cleanup) with a
                    # proper WAV header — this is what soundfile needs.
                    audio_data = sink.audio_data[user_obj]
                    wav_bytes = audio_data.file.read()
                    await asyncio.to_thread(
                        _write_wav_file, filepath, wav_bytes, temp_path
                    )
                    audio_count += 1
                    # Update the size in speaker_info now that we know it
                    speaker_info[i] = (
                        speaker_info[i][0],
                        speaker_info[i][1],
                        os.path.getsize(filepath),
                    )
                except Exception:
                    bot.logger.exception(
                        "Failed to write audio file %s from temp %s",
                        filepath, temp_path,
                    )
                    # Clean up orphaned temp file
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

            # Register the files in the DB now that they're on disk.
            for i, (discord_user_id, speaker_name, size_bytes) in enumerate(
                speaker_info
            ):
                filepath = file_specs[i][0]
                db.add_audio_file(
                    session_id=session_id,
                    filepath=filepath,
                    size_bytes=size_bytes,
                    discord_user_id=discord_user_id,
                    speaker_name=speaker_name,
                )

            bot.logger.info(
                f"Session {session_id}: saved {audio_count} audio file(s) to {rec_dir}"
            )

            # When nobody spoke, the sink has no audio data. Mark the
            # session as failed immediately so /stop can inform the user
            # and the transcriber doesn't poll forever for files that
            # will never arrive.
            if audio_count == 0:
                bot.logger.warning(
                    f"Session {session_id}: no audio captured (nobody spoke?), "
                    "marking session as failed"
                )
                db.fail_session(session_id)
        except Exception as e:
            bot.logger.error(f"Session {session_id}: recording callback failed: {e}")
            # Ensure session is marked as failed if the callback itself blew up
            db = bot.db
            db.fail_session(session_id)
            audio_count = 0
        finally:
            # If the recording failed with an error (exc is not None) or
            # no audio was captured, disconnect from the voice channel and
            # notify the user. The /start command already told the user
            # "Recording started!" — without this they'd see that message
            # followed by silence, with the bot still sitting in the voice
            # channel.
            if exc is not None or audio_count == 0:
                try:
                    guild = bot.get_guild(int(guild_id)) if guild_id else None
                    if guild and guild.voice_client and guild.voice_client.is_connected():
                        try:
                            if guild.voice_client.is_recording():
                                guild.voice_client.stop_recording()
                        except Exception:
                            pass
                        await guild.voice_client.disconnect()
                        bot.logger.info(
                            f"Session {session_id}: auto-disconnected from voice "
                            f"channel after failed recording"
                        )
                    notify_channel = bot.get_channel(int(channel_id)) if channel_id else None
                    if notify_channel:
                        await notify_channel.send(
                            f"Recording session `{session_id}` failed — "
                            f"no audio was captured. The bot has left the voice channel."
                        )
                except Exception:
                    bot.logger.warning(
                        f"Session {session_id}: failed to auto-disconnect "
                        f"or notify after failed recording"
                    )

            # Signal the future regardless of success/failure so /stop
            # doesn't hang forever waiting. The result is the audio file
            # count so /stop can distinguish empty recordings from real ones.
            if not done_future.done():
                done_future.set_result(audio_count)

    def after_cb(_sink, *args) -> None:
        """Synchronous after-callback for py-cord PR #3159.

        PR #3159 changed the callback signature from ``after(exc)`` to
        ``after(sink, *args)`` — the sink is the first positional argument,
        not an exception.  The old 2.8.0 ``after(exc)`` signature is gone.

        The callback may be invoked from a non-loop thread (py-cord's socket
        listener or AudioReader._stop()), so we schedule the async audio
        processing on the bot's event loop via run_coroutine_threadsafe.

        PR #3159's _stop() also guards the callback with
        ``if self.after and self.args`` — if no *args are passed to
        start_recording(), self.args is an empty tuple (falsy) and the
        callback is never invoked.  We pass a dummy arg (None) at the
        call site to satisfy this check.

        The _processing_started guard ensures we only process audio once
        even if the callback is somehow invoked twice.
        """
        if _processing_started:
            return
        asyncio.run_coroutine_threadsafe(_process_recording(None), loop)

    return after_cb, done_future
