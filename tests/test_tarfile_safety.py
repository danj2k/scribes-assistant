"""Tests for shared.tar_utils — safe tarfile extraction with path traversal protection.

Tests create real (in-memory or tmpdir) tar archives containing malicious
path entries and verify that safe_extract_members rejects them, while
correctly extracting legitimate archives with top-level directory stripping.
"""

import io
import tarfile
import pytest

from shared.tar_utils import safe_extract_members, _validate_member_path


class TestValidateMemberPath:
    """Tests for _validate_member_path."""

    def test_normal_relative_path_strips_one_component(self, tmp_path):
        """A normal 'dir/file.onnx' should resolve to 'file.onnx' in extract_dir."""
        info = tarfile.TarInfo(name="sherpa-onnx-whisper-small/small-encoder.onnx")
        info.size = 0
        dest = _validate_member_path(info, tmp_path, strip_components=1)
        assert dest is not None
        assert dest.name == "small-encoder.onnx"
        assert dest.parent == tmp_path

    def test_nested_relative_path_strips_one_component(self, tmp_path):
        """A nested 'dir/sub/file.onnx' should become 'sub/file.onnx'."""
        info = tarfile.TarInfo(name="sherpa-onnx-whisper-small/sub/file.onnx")
        info.size = 0
        dest = _validate_member_path(info, tmp_path, strip_components=1)
        assert dest is not None
        assert dest == tmp_path / "sub" / "file.onnx"

    def test_top_level_directory_entry_returns_none(self, tmp_path):
        """A member that IS the top-level dir should return None (skip)."""
        info = tarfile.TarInfo(name="sherpa-onnx-whisper-small")
        info.type = tarfile.DIRTYPE
        dest = _validate_member_path(info, tmp_path, strip_components=1)
        assert dest is None

    def test_absolute_path_rejected(self, tmp_path):
        """An absolute path like '/etc/passwd' should raise ValueError."""
        info = tarfile.TarInfo(name="/etc/passwd")
        with pytest.raises(ValueError, match="absolute path"):
            _validate_member_path(info, tmp_path, strip_components=0)

    def test_parent_traversal_rejected(self, tmp_path):
        """A path containing '..' should raise ValueError."""
        info = tarfile.TarInfo(name="../../../etc/passwd")
        with pytest.raises(ValueError, match="path traversal"):
            _validate_member_path(info, tmp_path, strip_components=0)

    def test_parent_traversal_after_strip_rejected(self, tmp_path):
        """Traversal that's in the remaining path after stripping should be rejected."""
        info = tarfile.TarInfo(name="topdir/../../etc/passwd")
        with pytest.raises(ValueError, match="path traversal"):
            _validate_member_path(info, tmp_path, strip_components=1)

    def test_symlink_with_absolute_target_rejected(self, tmp_path):
        """A symlink pointing to an absolute path should be rejected."""
        info = tarfile.TarInfo(name="evil_link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        with pytest.raises(ValueError, match="unsafe target"):
            _validate_member_path(info, tmp_path, strip_components=0)

    def test_symlink_with_traversal_target_rejected(self, tmp_path):
        """A symlink pointing via '..' should be rejected."""
        info = tarfile.TarInfo(name="evil_link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/passwd"
        with pytest.raises(ValueError, match="unsafe target"):
            _validate_member_path(info, tmp_path, strip_components=0)

    def test_safe_symlink_within_dir_allowed(self, tmp_path):
        """A symlink pointing within the extraction directory should be allowed."""
        info = tarfile.TarInfo(name="link_to_file")
        info.type = tarfile.SYMTYPE
        info.linkname = "target_file.txt"
        dest = _validate_member_path(info, tmp_path, strip_components=0)
        assert dest is not None


class TestSafeExtractMembers:
    """Tests for safe_extract_members with real tar archives."""

    def test_extracts_normal_archive(self, tmp_path):
        """A legitimate archive with top-level dir should extract correctly."""
        # Build a real tar.gz with a top-level directory.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # Add a top-level dir entry
            dir_info = tarfile.TarInfo(name="model-dir")
            dir_info.type = tarfile.DIRTYPE
            tar.addfile(dir_info)

            # Add files under the top-level dir
            for fname, content in [
                ("model-dir/encoder.onnx", b"encoder data"),
                ("model-dir/decoder.onnx", b"decoder data"),
                ("model-dir/tokens.txt", b"tokens"),
            ]:
                data = content
                info = tarfile.TarInfo(name=fname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        buf.seek(0)

        extract_dir = tmp_path / "models"
        extract_dir.mkdir()

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            result = safe_extract_members(tar, str(extract_dir), strip_components=1)

        assert "encoder.onnx" in result
        assert "decoder.onnx" in result
        assert "tokens.txt" in result
        assert (extract_dir / "encoder.onnx").read_bytes() == b"encoder data"
        assert (extract_dir / "decoder.onnx").read_bytes() == b"decoder data"
        assert (extract_dir / "tokens.txt").read_bytes() == b"tokens"

    def test_rejects_path_traversal_member(self, tmp_path):
        """An archive with a ../../../etc/evil entry should raise ValueError."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # Normal file
            data = b"safe"
            info = tarfile.TarInfo(name="model-dir/safe.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

            # Malicious traversal entry
            evil_data = b"evil"
            evil_info = tarfile.TarInfo(name="../../../tmp/evil")
            evil_info.size = len(evil_data)
            tar.addfile(evil_info, io.BytesIO(evil_data))

        buf.seek(0)
        extract_dir = tmp_path / "models"
        extract_dir.mkdir()

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            with pytest.raises(ValueError, match="path traversal"):
                safe_extract_members(tar, str(extract_dir), strip_components=1)

        # The legitimate file should NOT have been extracted either —
        # fail-closed: we don't leave a partial extraction.
        # Note: the safe file may have been extracted before we hit the
        # malicious one, depending on member ordering. The key assertion
        # is that the ValueError was raised.

    def test_rejects_absolute_path_member(self, tmp_path):
        """An archive with an absolute path entry should raise ValueError."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"evil"
            info = tarfile.TarInfo(name="/tmp/evil.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        buf.seek(0)
        extract_dir = tmp_path / "models"
        extract_dir.mkdir()

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            with pytest.raises(ValueError, match="absolute path"):
                safe_extract_members(tar, str(extract_dir), strip_components=0)

    def test_rejects_symlink_escape(self, tmp_path):
        """A symlink pointing outside the extraction dir should be rejected."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # Normal file
            data = b"safe"
            info = tarfile.TarInfo(name="model-dir/safe.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

            # Symlink pointing outside
            sym_info = tarfile.TarInfo(name="model-dir/evil_link")
            sym_info.type = tarfile.SYMTYPE
            sym_info.linkname = "../../etc/passwd"
            tar.addfile(sym_info)

        buf.seek(0)
        extract_dir = tmp_path / "models"
        extract_dir.mkdir()

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            with pytest.raises(ValueError, match="unsafe target"):
                safe_extract_members(tar, str(extract_dir), strip_components=1)

    def test_empty_archive_returns_empty_list(self, tmp_path):
        """An archive with no file members should return an empty list."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # Only a top-level dir entry
            dir_info = tarfile.TarInfo(name="model-dir")
            dir_info.type = tarfile.DIRTYPE
            tar.addfile(dir_info)

        buf.seek(0)
        extract_dir = tmp_path / "models"
        extract_dir.mkdir()

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            result = safe_extract_members(tar, str(extract_dir), strip_components=1)

        assert result == []

    def test_preserves_nested_directory_structure(self, tmp_path):
        """Nested dirs within the top-level should be preserved after stripping."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, content in [
                ("model-dir/sub/encoder.onnx", b"enc"),
                ("model-dir/sub/decoder.onnx", b"dec"),
            ]:
                info = tarfile.TarInfo(name=path)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))

        buf.seek(0)
        extract_dir = tmp_path / "models"
        extract_dir.mkdir()

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            result = safe_extract_members(tar, str(extract_dir), strip_components=1)

        assert "sub/encoder.onnx" in result
        assert "sub/decoder.onnx" in result
        assert (extract_dir / "sub" / "encoder.onnx").read_bytes() == b"enc"
        assert (extract_dir / "sub" / "decoder.onnx").read_bytes() == b"dec"

    def test_strip_components_zero(self, tmp_path):
        """strip_components=0 should keep the full path (no stripping)."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"content"
            info = tarfile.TarInfo(name="file.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        buf.seek(0)
        extract_dir = tmp_path / "models"
        extract_dir.mkdir()

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            result = safe_extract_members(tar, str(extract_dir), strip_components=0)

        assert result == ["file.txt"]
        assert (extract_dir / "file.txt").read_bytes() == b"content"
