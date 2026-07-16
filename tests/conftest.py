"""Shared pytest fixtures for scribes-assistant tests."""

import sys
import os

# Add project root to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Pre-import discord.sinks so that test files which conditionally mock
# discord sub-modules find the real package in sys.modules.  bot.commands
# now transitively imports bot.timestamped_sink, which requires the real
# discord.sinks.WaveSink and discord.sinks.core.AudioData/Filters.
# Without this, the MagicMock fallback produces:
#   ModuleNotFoundError: No module named 'discord.sinks'; 'discord' is not a package
try:
    import discord.sinks  # noqa: F401
except ImportError:
    pass
