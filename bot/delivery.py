"""Transcript delivery — polls the database for completed transcripts and uploads to Discord.

Runs as a background task in the bot process, checking every few seconds
for new transcripts ready to deliver.
"""

import asyncio
import re
from pathlib import Path

import discord

from shared.database import Database, STATUS_COMPLETE
from shared.lexicon import Lexicon


class DeliveryLoop:
    """Background task that polls for completed transcripts and delivers them."""
    
    def __init__(self, bot, db: Database, logger):
        self.bot = bot
        self.db = db
        self.logger = logger
        self.poll_interval = 5  # seconds
        # Track the background task for is_running checks
        self._task: asyncio.Task | None = None
        # Load lexicon for post-correction of transcripts
        self._lexicon = None
        try:
            lexicon_file = bot.config.lexicon_file
            self._lexicon = Lexicon(lexicon_file)
            self.logger.info(
                f"Loaded lexicon with {len(self._lexicon.terms)} terms "
                f"for transcript correction"
            )
        except Exception as e:
            self.logger.warning(f"Could not load lexicon for correction: {e}")
    
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
        # Find sessions that are complete but haven't been delivered yet
        # (transcript_path set but thread hasn't been notified)
        with self.db._cursor() as cur:
            cur.execute(
                "SELECT * FROM sessions WHERE status = ? AND transcript_path IS NOT NULL "
                "AND thread_id IS NULL ORDER BY started_at",
                (STATUS_COMPLETE,),
            )
            rows = cur.fetchall()
        
        for row in rows:
            session = dict(row)
            await self._deliver(session)
    
    def _correct_text(self, text: str) -> str:
        """Apply lexicon fuzzy correction to transcript text.

        Splits on word boundaries, corrects each word independently,
        and reassembles with original whitespace preserved.
        """
        if not self._lexicon or not self._lexicon.terms:
            return text

        def _replace_word(match):
            word = match.group(0)
            corrected = self._lexicon.correct(word)
            return corrected if corrected else word

        return re.sub(r"\b\w+\b", _replace_word, text)

    async def _deliver(self, session: dict):
        """Deliver a single transcript to its Discord channel."""
        session_id = session["id"]
        channel_id = int(session["discord_channel_id"])
        transcript_path = session["transcript_path"]
        
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

        # Apply lexicon-based post-correction
        if self._lexicon and self._lexicon.terms:
            transcript_text = self._correct_text(transcript_text)

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
