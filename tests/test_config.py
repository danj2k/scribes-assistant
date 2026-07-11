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
