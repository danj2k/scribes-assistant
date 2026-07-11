"""Configuration loader — reads config.yaml with sensible defaults.

Provides load_config() for simple dict access and Config for convenience
with dotted-key lookup and property accessors.
"""
import copy
import os
from pathlib import Path

import yaml

_DEFAULTS = {
    "discord": {
        "guild_id": "",
        "transcript_channel_id": "",
        "permissions": {
            "restrict_commands": False,
            "allowed_roles": [],
        },
    },
    "bot": {
        "token_file": "/run/secrets/bot_token",
        "log_level": "INFO",
        "log_max_size_mb": 10,
        "log_backup_count": 5,
    },
    "lexicon": {
        "enabled": True,
        "fuzzy_threshold": 0.2,
        "file": "/data/lexicon.yaml",
    },
    "session": {
        "idle_timeout": 60,
    },
    "transcriber": {
        "poll_interval": 10,
        "threads": 0,
        "model": "small",
        "model_dir": "/data/models",
    },
    "logging": {
        "level": "INFO",
        "max_size_mb": 10,
        "backup_count": 5,
    },
    "database": {
        "path": "/data/queue.db",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | None = None) -> dict:
    """Load configuration, falling back to defaults.

    Returns a plain dict so callers can do ``config["discord"]["guild_id"]``.
    """
    result = copy.deepcopy(_DEFAULTS)
    if path and Path(path).exists():
        with open(path, "r") as f:
            file_cfg = yaml.safe_load(f) or {}
        result = _deep_merge(result, file_cfg)
    return result


class Config:
    """Convenience wrapper around a loaded config dict.

    Supports dotted-key lookup via ``get("a.b.c")`` and typed property
    accessors for commonly used values.
    """

    def __init__(self, path: str | None = None):
        self._data = load_config(path)

    def get(self, dotted_key: str, default=None):
        """Retrieve a value using a dotted key path.

        Example: ``config.get("a.b.c")`` walks into nested dicts.
        """
        keys = dotted_key.split(".")
        current = self._data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current

    # -- Discord properties ---------------------------------------------------

    @property
    def guild_id(self) -> int:
        """Return the Discord guild ID as an integer."""
        raw = self.get("discord.guild_id", "0")
        return int(raw) if raw else 0

    @property
    def transcript_channel_id(self) -> int:
        """Return the transcript delivery channel ID as an integer, or 0 if not set."""
        raw = self.get("discord.transcript_channel_id", "")
        return int(raw) if raw else 0

    @property
    def restrict_commands(self) -> bool:
        return bool(self.get("discord.permissions.restrict_commands", False))

    @property
    def allowed_roles(self) -> list:
        return self.get("discord.permissions.allowed_roles", [])

    # -- Bot properties -------------------------------------------------------

    @property
    def bot_token(self) -> str:
        """Return the Discord bot token, raising FileNotFoundError if absent.

        The token is read from a Docker secrets file (default
        ``/run/secrets/bot_token``).  If the file does not exist we raise
        ``FileNotFoundError`` with a actionable message rather than returning
        ``None`` — ``bot.run(None)`` produces a cryptic py-cord traceback that
        gives no hint about the real cause.
        """
        token_file = self.get("bot.token_file", "/run/secrets/bot_token")
        path = Path(token_file)
        if not path.exists():
            raise FileNotFoundError(
                f"Bot token file not found: {token_file}. "
                f"Create it (e.g. echo 'YOUR_TOKEN' > {token_file}) "
                f"or configure bot.token_file in config.yaml."
            )
        token = path.read_text().strip()
        if not token:
            raise ValueError(
                f"Bot token file is empty: {token_file}. "
                f"Put your Discord bot token in this file."
            )
        return token

    # -- Lexicon properties ---------------------------------------------------

    @property
    def lexicon_enabled(self) -> bool:
        return bool(self.get("lexicon.enabled", True))

    @property
    def lexicon_threshold(self) -> float:
        return float(self.get("lexicon.fuzzy_threshold", 0.2))

    @property
    def lexicon_file(self) -> str:
        return self.get("lexicon.file", "/data/lexicon.yaml")

    # -- Session properties ---------------------------------------------------

    @property
    def idle_timeout(self) -> int:
        """Seconds of idle time before auto-ending a session."""
        return int(self.get("session.idle_timeout", 60))

    # -- Transcriber properties -----------------------------------------------

    @property
    def poll_interval(self) -> int:
        """Seconds between queue polling cycles."""
        return int(self.get("transcriber.poll_interval", 10))

    @property
    def num_threads(self) -> int:
        """Number of CPU threads for sherpa-onnx."""
        return int(self.get("transcriber.threads", 0))

    @property
    def model_size(self) -> str:
        """Whisper model size (tiny, base, small, medium, large-v3)."""
        return self.get("transcriber.model", "small")

    @property
    def model_path(self) -> str:
        """Full path to the sherpa-onnx model directory.
        
        Composed from the base model_dir setting and the model size.
        E.g. /data/models + whisper-small -> /data/models/whisper-small
        """
        base = self.get("transcriber.model_dir", "/data/models")
        return os.path.join(base, f"whisper-{self.model_size}")

    @property
    def sample_rate(self) -> int:
        """Audio sample rate for transcription (sherpa-onnx uses 16kHz)."""
        return 16000

    # -- Logging properties ---------------------------------------------------

    @property
    def log_level(self) -> str:
        return self.get("logging.level", self.get("bot.log_level", "INFO"))

    @property
    def log_max_size_mb(self) -> int:
        return int(self.get("logging.max_size_mb", self.get("bot.log_max_size_mb", 10)))

    @property
    def log_backup_count(self) -> int:
        return int(self.get("logging.backup_count", self.get("bot.log_backup_count", 5)))

    # -- Database properties --------------------------------------------------

    @property
    def database_path(self) -> str:
        return self.get("database.path", "/data/queue.db")
