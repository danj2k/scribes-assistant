"""Tests for bot/commands.py — specifically the _check_permission helper."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import _check_permission without polluting sys.modules for other test files.
#
# bot/commands.py imports discord, discord.commands, and discord.ext.commands.
# discord.commands (SlashCommandGroup, Option) may not be available in all environments, so we
# must temporarily mock those sub-modules during import.  We save and restore
# sys.modules so the mocks don't leak into test_error_handler.py (which imports
# the real discord module).
# ---------------------------------------------------------------------------

# 1. Snapshot the original discord-related entries.
_original: dict[str, types.ModuleType | None] = {}
for _key in list(sys.modules):
    if _key == "discord" or _key.startswith("discord."):
        _original[_key] = sys.modules[_key]

# 2. Inject temporary mocks for the missing sub-modules.
_mock_modules: dict[str, MagicMock] = {}
for _mod_name in ("discord", "discord.ext", "discord.ext.commands", "discord.commands"):
    if _mod_name not in sys.modules:
        _mock_modules[_mod_name] = MagicMock()
        sys.modules[_mod_name] = _mock_modules[_mod_name]

# 3. Import the function we need.
from bot.commands import _check_permission  # noqa: E402

# 4. Restore original sys.modules — remove any mocks we added, re-instate any
#    originals we saved.
for _key in list(sys.modules):
    if _key == "discord" or _key.startswith("discord."):
        if _key in _original:
            sys.modules[_key] = _original[_key]
        else:
            del sys.modules[_key]

# 5. Also clean up the bot.commands module so it re-imports with real discord
#    in other test files.
if "bot.commands" in sys.modules:
    del sys.modules["bot.commands"]
if "bot" in sys.modules:
    del sys.modules["bot"]


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------

class FakeRole:
    def __init__(self, name: str, role_id: int):
        self.name = name
        self.id = role_id


class FakeUser:
    def __init__(self, roles):
        self.roles = roles


class FakeInteraction:
    def __init__(self, user):
        self.user = user


class FakeConfig:
    def __init__(self, restrict_commands: bool, allowed_roles: list):
        self.restrict_commands = restrict_commands
        self.allowed_roles = allowed_roles


def _make_interaction(role_names=None, role_ids=None):
    """Build a FakeInteraction with the given role names and IDs."""
    roles = []
    for i, name in enumerate(role_names or []):
        rid = (role_ids or [100 + i])[min(i, len(role_ids or []) - 1)]
        roles.append(FakeRole(name, rid))
    return FakeInteraction(FakeUser(roles))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCheckPermission:

    def test_restriction_disabled_allows_everyone(self):
        config = FakeConfig(restrict_commands=False, allowed_roles=[])
        interaction = _make_interaction()
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None

    def test_empty_allowed_roles_allows_everyone(self):
        config = FakeConfig(restrict_commands=True, allowed_roles=[])
        interaction = _make_interaction()
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None

    def test_matching_role_name_allows(self):
        config = FakeConfig(restrict_commands=True, allowed_roles=["Moderator"])
        interaction = _make_interaction(["Moderator"], [111])
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None

    def test_role_name_match_is_case_insensitive(self):
        config = FakeConfig(restrict_commands=True, allowed_roles=["moderator"])
        interaction = _make_interaction(["Moderator"], [111])
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None

    def test_matching_role_id_allows(self):
        config = FakeConfig(restrict_commands=True, allowed_roles=[111])
        interaction = _make_interaction(["SomeRole"], [111])
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None

    def test_no_roles_denied(self):
        config = FakeConfig(restrict_commands=True, allowed_roles=["Admin"])
        interaction = _make_interaction()
        allowed, error = _check_permission(interaction, config)
        assert allowed is False
        assert "permission" in error.lower()

    def test_unrelated_role_denied(self):
        config = FakeConfig(restrict_commands=True, allowed_roles=["Admin", 999])
        interaction = _make_interaction(["Member"], [42])
        allowed, error = _check_permission(interaction, config)
        assert allowed is False
        assert error is not None

    def test_multiple_roles_one_match_allows(self):
        config = FakeConfig(restrict_commands=True, allowed_roles=["Admin"])
        interaction = _make_interaction(["Member", "Admin"], [42, 111])
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None

    def test_mixed_role_types_in_config(self):
        config = FakeConfig(restrict_commands=True, allowed_roles=["Admin", 555])
        interaction = _make_interaction(["Moderator"], [555])
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None

    def test_string_role_id_does_not_match_numeric_role_id(self):
        """'111' is a string, so it's compared by name, not ID."""
        config = FakeConfig(restrict_commands=True, allowed_roles=["111"])
        interaction = _make_interaction(["SomeRole"], [111])
        allowed, error = _check_permission(interaction, config)
        assert allowed is False
        assert error is not None


class TestInviteCommandPermission:
    """Verify the /invite permission gate.

    /invite generates a bot invite URL — an admin-only action.  We can't
    easily invoke the slash command handler (it's a closure inside
    setup_commands and requires a full py-cord bot mock), so we verify
    that _check_permission enforces the right policy for the /invite
    scenario: restricted when roles are configured, open when they're not.
    """

    def test_invite_denied_without_matching_role(self):
        """Non-admin user is denied /invite when roles are configured."""
        config = FakeConfig(restrict_commands=True, allowed_roles=["DM"])
        interaction = _make_interaction(["Player"], [42])
        allowed, error = _check_permission(interaction, config)
        assert allowed is False
        assert error is not None

    def test_invite_allowed_with_matching_role(self):
        """Admin user is allowed /invite when roles are configured."""
        config = FakeConfig(restrict_commands=True, allowed_roles=["DM"])
        interaction = _make_interaction(["DM"], [99])
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None

    def test_invite_allowed_when_restriction_disabled(self):
        """Everyone can use /invite when command restriction is off."""
        config = FakeConfig(restrict_commands=False, allowed_roles=[])
        interaction = _make_interaction(["Player"], [42])
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None

    def test_invite_allowed_when_no_roles_configured(self):
        """Everyone can use /invite when allowed_roles is empty."""
        config = FakeConfig(restrict_commands=True, allowed_roles=[])
        interaction = _make_interaction(["Player"], [42])
        allowed, error = _check_permission(interaction, config)
        assert allowed is True
        assert error is None
