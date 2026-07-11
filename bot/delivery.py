"""Transcript delivery — polls the database for completed transcripts and uploads to Discord.

Runs as a background task in the bot process, checking every few seconds
for new transcripts ready to deliver.
"""

import asyncio
from pathlib import Path

import discord

from shared.database import Database


class DeliveryLoop:
    """Background task that polls for completed transcripts and delivers them."""
    
    def __init__(self, bot, db: Database, logger):
        self.bot = bot
        self.db = db
        self.logger = logger
        self.poll_interval = 5  # seconds
        # Track the background task for is_running checks
        self._task: asyncio.Task | None = None
    
    @property
    def is_running(self) -> bool:
        """Whether the delivery loop task is currently active."""
        return self._task is not None and not self._task.done()

    def start(self):
        """Start the delivery loop as a background task.

        Safe to call multiple times — subsequent calls are no-ops
        if the loop is already running.
        """
        if self.is_running:
            return
        self._task = asyncio.create_task(self.run())

    async def stop(self):
        """Cancel the delivery loop task and wait for it to finish.

        Called during graceful shutdown so a transcript delivery in
        flight is not abandoned mid-send.
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self.logger.info("Delivery loop stopped")

    async def run(self):
        """Main polling loop — runs until the bot shuts down."""
        self.logger.info("Transcript delivery loop started")
        
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                self.logger.info("Delivery loop cancelled")
                break
            except Exception as e:
                self.logger.error(f"Delivery loop error: {e}", exc_info=True)
            
            await asyncio.sleep(self.poll_interval)
    
    async def _poll(self):
        """Check for completed transcripts and deliver them."""
        sessions = self.db.get_sessions_for_delivery()
        for session in sessions:
            await self._deliver(session)
    
    async def _deliver(self, session: dict):
        """Deliver a single transcript to its Discord channel.

        If discord.transcript_channel_id is configured, all transcripts are
        delivered there. Otherwise they go to the channel where the session
        was started.
        """
        session_id = session["id"]
        transcript_path = session["transcript_path"]

        # Use the configured transcript channel if set, otherwise the session's channel
        config_channel_id = self.bot.config.transcript_channel_id
        if config_channel_id:
            channel_id = config_channel_id
        else:
            channel_id = int(session["discord_channel_id"])
        
        channel = self.bot.get_channel(channel_id)
        if not channel:
            self.logger.warning(
                f"Cannot deliver transcript for {session_id}: "
                f"channel {channel_id} not found"
            )
            return
        
        # Read transcript content
        path = Path(transcript_path)
        if not path.exists():
            self.logger.warning(
                f"Transcript file not found: {transcript_path}"
            )
            return
        
        transcript_text = path.read_text()

        # Create a thread for this transcript
        try:
            thread = await channel.create_thread(
                name=f"Transcript — {session_id}",
                auto_archive_duration=1440,  # 24 hours
            )
            
            # Send the transcript in parts if needed (Discord 2000 char limit)
            if len(transcript_text) <= 2000:
                await thread.send(f"```\n{transcript_text}\n```")
            else:
                # Split into chunks
                lines = transcript_text.split("\n")
                chunk = ""
                for line in lines:
                    if len(chunk) + len(line) + 1 > 1900:
                        await thread.send(f"```\n{chunk}\n```")
                        chunk = line + "\n"
                    else:
                        chunk += line + "\n"
                if chunk:
                    await thread.send(f"```\n{chunk}\n```")
            
            # Update database with thread ID
            self.db.set_thread_id(session_id, str(thread.id))
            
            self.logger.info(
                f"Delivered transcript for session {session_id} "
                f"to thread {thread.id} in channel {channel_id}"
            )
            
        except discord.Forbidden:
            self.logger.error(
                f"Permission denied delivering transcript for {session_id}"
            )
        except Exception as e:
            self.logger.error(
                f"Failed to deliver transcript for {session_id}: {e}"
            )
