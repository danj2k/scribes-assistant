"""Tests for shared.config — configuration loading."""

import yaml
import pytest
from shared.config import load_config, Config


def _write_yaml(path, data):
    """Helper: write a YAML dict to a file."""
    path.write_text(yaml.dump(data))


class TestLoadConfig:
    """Tests for load_config."""

    def test_load_default(self, tmp_path):
        """Loading a non-existent config returns defaults."""
        config = load_config(str(tmp_path / "nonexistent.yaml"))
        assert isinstance(config, dict)
        assert "discord" in config

    def test_load_yaml(self, tmp_path):
        """A config file overrides defaults."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("discord:\n  guild_id: '999'\n")
        config = load_config(str(cfg_file))
        assert config["discord"]["guild_id"] == "999"

    def test_nested_defaults_are_independent(self, tmp_path):
        """Mutating a nested dict in the returned config must not corrupt _DEFAULTS.

        Regression test for the shallow-copy bug: _DEFAULTS.copy() only copies
        the top-level dict, so nested dicts (discord, permissions, lexicon, etc.)
        are shared references.  Any in-place mutation of a nested default would
        persist across all future load_config() calls.
        """
        import shared.config as cfg_mod
        # Capture the original default value before any mutation
        original_roles = cfg_mod._DEFAULTS["discord"]["permissions"]["allowed_roles"]
        assert original_roles == []

        config = load_config(str(tmp_path / "nonexistent.yaml"))
        config["discord"]["permissions"]["allowed_roles"].append(12345)

        # _DEFAULTS must NOT be affected
        assert cfg_mod._DEFAULTS["discord"]["permissions"]["allowed_roles"] == []

        # A fresh load_config call must also be unaffected
        config2 = load_config(str(tmp_path / "nonexistent.yaml"))
        assert config2["discord"]["permissions"]["allowed_roles"] == []


class TestConfig:
    """Tests for the Config convenience class."""

    def test_config_get(self, tmp_path):
        """Config.get returns value for a dotted key."""
        _write_yaml(tmp_path / "cfg.yaml", {"a": {"b": {"c": 42}}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.get("a.b.c") == 42

    def test_config_get_default(self, tmp_path):
        """Config.get returns default for missing keys."""
        _write_yaml(tmp_path / "cfg.yaml", {})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.get("missing.key", "fallback") == "fallback"

    def test_config_guild_id(self, tmp_path):
        """Config.guild_id returns an integer."""
        _write_yaml(tmp_path / "cfg.yaml", {
            "discord": {"guild_id": "123", "permissions": {"restrict_commands": False, "allowed_roles": []}},
            "lexicon": {"enabled": False, "fuzzy_threshold": 0.8},
        })
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.guild_id == 123


class TestBotToken:
    """Tests for Config.bot_token — Bug #10 regression tests."""

    def test_bot_token_reads_file(self, tmp_path):
        """bot_token returns the stripped content of the token file."""
        token_file = tmp_path / "bot_token"
        token_file.write_text("my.secret.token\n")
        _write_yaml(tmp_path / "cfg.yaml", {"bot": {"token_file": str(token_file)}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.bot_token == "my.secret.token"

    def test_bot_token_strips_whitespace(self, tmp_path):
        """bot_token strips leading/trailing whitespace from the file content."""
        token_file = tmp_path / "bot_token"
        token_file.write_text("  my.secret.token  \n\n")
        _write_yaml(tmp_path / "cfg.yaml", {"bot": {"token_file": str(token_file)}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.bot_token == "my.secret.token"

    def test_bot_token_raises_when_file_missing(self, tmp_path):
        """bot_token raises FileNotFoundError when the token file does not exist.

        Regression test for Bug #10: previously the property returned None
        silently, causing bot.run(None) to fail with a cryptic py-cord error.
        """
        _write_yaml(tmp_path / "cfg.yaml", {"bot": {"token_file": str(tmp_path / "nonexistent")}})
        c = Config(str(tmp_path / "cfg.yaml"))
        with pytest.raises(FileNotFoundError, match="Bot token file not found"):
            c.bot_token

    def test_bot_token_raises_when_file_empty(self, tmp_path):
        """bot_token raises ValueError when the token file exists but is empty."""
        token_file = tmp_path / "bot_token"
        token_file.write_text("")
        _write_yaml(tmp_path / "cfg.yaml", {"bot": {"token_file": str(token_file)}})
        c = Config(str(tmp_path / "cfg.yaml"))
        with pytest.raises(ValueError, match="Bot token file is empty"):
            c.bot_token

    def test_bot_token_raises_when_file_whitespace_only(self, tmp_path):
        """bot_token raises ValueError when the token file has only whitespace."""
        token_file = tmp_path / "bot_token"
        token_file.write_text("   \n\n\t\n")
        _write_yaml(tmp_path / "cfg.yaml", {"bot": {"token_file": str(token_file)}})
        c = Config(str(tmp_path / "cfg.yaml"))
        with pytest.raises(ValueError, match="Bot token file is empty"):
            c.bot_token


class TestLoggingConfig:
    """Tests for logging configuration — Bug #23 regression tests.

    Previously both ``bot.log_level`` / ``bot.log_max_size_mb`` /
    ``bot.log_backup_count`` AND ``logging.level`` / ``logging.max_size_mb`` /
    ``logging.backup_count`` existed in _DEFAULTS, with the Config properties
    falling back from ``logging.*`` to ``bot.*``.  The ``bot.*`` keys have been
    removed — logging is now configured solely through the shared ``logging``
    section.
    """

    def test_log_level_default(self, tmp_path):
        """log_level defaults to INFO when no logging config is present."""
        _write_yaml(tmp_path / "cfg.yaml", {})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.log_level == "INFO"

    def test_log_max_size_mb_default(self, tmp_path):
        """log_max_size_mb defaults to 10 when no logging config is present."""
        _write_yaml(tmp_path / "cfg.yaml", {})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.log_max_size_mb == 10

    def test_log_backup_count_default(self, tmp_path):
        """log_backup_count defaults to 5 when no logging config is present."""
        _write_yaml(tmp_path / "cfg.yaml", {})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.log_backup_count == 5

    def test_log_level_from_logging_section(self, tmp_path):
        """log_level reads from logging.level."""
        _write_yaml(tmp_path / "cfg.yaml", {"logging": {"level": "DEBUG"}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.log_level == "DEBUG"

    def test_log_max_size_mb_from_logging_section(self, tmp_path):
        """log_max_size_mb reads from logging.max_size_mb."""
        _write_yaml(tmp_path / "cfg.yaml", {"logging": {"max_size_mb": 25}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.log_max_size_mb == 25

    def test_log_backup_count_from_logging_section(self, tmp_path):
        """log_backup_count reads from logging.backup_count."""
        _write_yaml(tmp_path / "cfg.yaml", {"logging": {"backup_count": 3}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.log_backup_count == 3

    def test_bot_log_keys_are_ignored(self, tmp_path):
        """bot.log_* keys must NOT be read — only the shared logging section.

        Regression test for Bug #23: the old fallback chain
        ``self.get("logging.level", self.get("bot.log_level", ...))`` meant
        users could accidentally configure logging via ``bot.log_level``.
        Now that logging is a shared concern, only ``logging.*`` is read.
        """
        _write_yaml(tmp_path / "cfg.yaml", {
            "bot": {
                "log_level": "ERROR",
                "log_max_size_mb": 99,
                "log_backup_count": 99,
            },
        })
        c = Config(str(tmp_path / "cfg.yaml"))
        # Must fall through to defaults, NOT read the bot.* keys
        assert c.log_level == "INFO"
        assert c.log_max_size_mb == 10
        assert c.log_backup_count == 5

    def test_logging_section_overrides_bot_section(self, tmp_path):
        """When both sections are present, logging.* wins and bot.* is ignored."""
        _write_yaml(tmp_path / "cfg.yaml", {
            "bot": {
                "log_level": "ERROR",
                "log_max_size_mb": 99,
                "log_backup_count": 99,
            },
            "logging": {
                "level": "WARNING",
                "max_size_mb": 5,
                "backup_count": 2,
            },
        })
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.log_level == "WARNING"
        assert c.log_max_size_mb == 5
        assert c.log_backup_count == 2


class TestConfigValidate:
    """Tests for Config.validate() — startup validation."""

    def test_valid_config_returns_no_errors(self, tmp_path):
        """A config with guild_id and transcript_channel_id set passes validation."""
        _write_yaml(tmp_path / "cfg.yaml", {
            "discord": {
                "guild_id": "123456789",
                "transcript_channel_id": "987654321",
            },
        })
        c = Config(str(tmp_path / "cfg.yaml"))
        errors = c.validate()
        assert errors == []

    def test_missing_guild_id_returns_error(self, tmp_path):
        """Missing guild_id is reported as an error."""
        _write_yaml(tmp_path / "cfg.yaml", {
            "discord": {
                "transcript_channel_id": "987654321",
            },
        })
        c = Config(str(tmp_path / "cfg.yaml"))
        errors = c.validate()
        assert len(errors) == 1
        assert "guild_id" in errors[0]

    def test_missing_transcript_channel_id_returns_error(self, tmp_path):
        """Missing transcript_channel_id is reported as an error."""
        _write_yaml(tmp_path / "cfg.yaml", {
            "discord": {
                "guild_id": "123456789",
            },
        })
        c = Config(str(tmp_path / "cfg.yaml"))
        errors = c.validate()
        assert len(errors) == 1
        assert "transcript_channel_id" in errors[0]

    def test_empty_config_returns_both_errors(self, tmp_path):
        """An empty config (all defaults) returns both errors."""
        _write_yaml(tmp_path / "cfg.yaml", {})
        c = Config(str(tmp_path / "cfg.yaml"))
        errors = c.validate()
        assert len(errors) == 2
        assert "guild_id" in errors[0]
        assert "transcript_channel_id" in errors[1]

    def test_validate_includes_path_in_error(self, tmp_path):
        """Error messages include the config file path when provided."""
        cfg_path = str(tmp_path / "cfg.yaml")
        _write_yaml(tmp_path / "cfg.yaml", {})
        c = Config(cfg_path)
        errors = c.validate(cfg_path)
        assert any(cfg_path in err for err in errors)


class TestRecordingRetentionConfig:
    """Tests for recording retention configuration."""

    def test_retention_days_default(self, tmp_path):
        """recording_retention_days defaults to 8."""
        _write_yaml(tmp_path / "cfg.yaml", {})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.recording_retention_days == 8

    def test_retention_days_custom(self, tmp_path):
        """recording_retention_days reads from recording.retention_days."""
        _write_yaml(tmp_path / "cfg.yaml", {"recording": {"retention_days": 30}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.recording_retention_days == 30

    def test_retention_days_zero(self, tmp_path):
        """retention_days=0 means keep forever (no purge)."""
        _write_yaml(tmp_path / "cfg.yaml", {"recording": {"retention_days": 0}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.recording_retention_days == 0

    def test_purge_interval_default(self, tmp_path):
        """purge_interval_hours defaults to 6."""
        _write_yaml(tmp_path / "cfg.yaml", {})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.purge_interval_hours == 6

    def test_purge_interval_custom(self, tmp_path):
        """purge_interval_hours reads from recording.purge_interval_hours."""
        _write_yaml(tmp_path / "cfg.yaml", {"recording": {"purge_interval_hours": 12}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.purge_interval_hours == 12

    def test_purge_interval_zero(self, tmp_path):
        """purge_interval_hours=0 means startup-only sweep."""
        _write_yaml(tmp_path / "cfg.yaml", {"recording": {"purge_interval_hours": 0}})
        c = Config(str(tmp_path / "cfg.yaml"))
        assert c.purge_interval_hours == 0

