"""Safe tarfile extraction utilities.

Provides path-traversal-protected extraction for model archive unpacking.
Used by both ``transcriber/worker.py`` and ``scripts/download_model.py``
to avoid code duplication.

Python 3.12+ offers ``tarfile.extract(..., filter='data')`` which handles
path traversal, symlink attacks, and other dangers. However, the Docker
base image uses Python 3.11, so we implement equivalent protection
manually:

1. Reject absolute paths (``/etc/passwd``)
2. Reject path traversal (``../../etc/passwd``)
3. Reject symlinks that escape the extraction directory
4. Verify the resolved path stays within the target directory

The ``strip_components`` parameter controls how many leading path
components are removed from each member name before extraction —
used to strip the top-level archive directory (e.g.
``sherpa-onnx-whisper-small/small-encoder.onnx`` -> ``small-encoder.onnx``).
"""

import os
import sys
import tarfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _validate_member_path(member: tarfile.TarInfo, extract_dir: Path,
                          strip_components: int = 1) -> Path:
    """Resolve and validate a tar member's extraction path.

    Strips the leading ``strip_components`` path segments from
    ``member.name``, then ensures the resulting path stays within
    ``extract_dir`` — rejecting absolute paths, parent-directory
    traversal, and symlink escapes.

    Args:
        member: The tar archive member to validate.
        extract_dir: The target extraction directory.
        strip_components: Number of leading path segments to remove.

    Returns:
        The resolved destination path within ``extract_dir``.

    Raises:
        ValueError: If the member path is unsafe (absolute, escapes
            the extraction directory, or is a symlink pointing outside).
    """
    name = member.name

    # Strip leading path components (e.g. top-level archive dir).
    parts = name.split("/")
    if len(parts) > strip_components:
        name = "/".join(parts[strip_components:])
    else:
        # The member IS a top-level directory entry — skip it.
        # Returning a sentinel that callers can detect.
        return None  # type: ignore[return-value]

    dest = extract_dir / name

    # Reject absolute paths — member.name should be relative.
    if Path(name).is_absolute():
        raise ValueError(
            f"Refusing to extract member with absolute path: {member.name!r}"
        )

    # Reject parent-directory traversal.
    if ".." in Path(name).parts:
        raise ValueError(
            f"Refusing to extract member with path traversal: {member.name!r}"
        )

    # Reject symlinks/hardlinks that escape the extraction directory.
    if member.issym() or member.islnk():
        link_target = Path(member.linkname)
        if link_target.is_absolute() or ".." in link_target.parts:
            raise ValueError(
                f"Refusing to extract symlink with unsafe target: "
                f"{member.linkname!r} from member {member.name!r}"
            )

    # Final check: resolved path must be inside extract_dir.
    resolved = dest.resolve()
    extract_resolved = extract_dir.resolve()
    try:
        resolved.relative_to(extract_resolved)
    except ValueError:
        raise ValueError(
            f"Resolved path {resolved} escapes extraction directory "
            f"{extract_resolved}"
        )

    return dest


def safe_extract_members(tar: tarfile.TarFile, extract_dir: str,
                         strip_components: int = 1) -> list[str]:
    """Extract all members from a tar archive with path traversal protection.

    Each member's destination path is validated before extraction.
    Members that would escape ``extract_dir`` raise ``ValueError`` and
    halt extraction — this is fail-closed by design. A corrupted or
    malicious archive should not be partially extracted.

    Args:
        tar: An open ``tarfile.TarFile`` instance.
        extract_dir: The directory to extract into.
        strip_components: Number of leading path segments to strip
            from each member name. Defaults to 1 (strip the top-level
            archive directory).

    Returns:
        A list of extracted file names (relative to ``extract_dir``).

    Raises:
        ValueError: If any member has an unsafe path.
        RuntimeError: If extraction of a validated member fails.
    """
    extract_path = Path(extract_dir)
    extracted_names: list[str] = []

    for member in tar.getmembers():
        dest = _validate_member_path(member, extract_path, strip_components)
        if dest is None:
            # Top-level directory entry — nothing to extract.
            continue

        # Update the member name to the stripped path so tarfile.extract
        # writes to the correct location.
        member.name = str(dest.relative_to(extract_path))

        # Use filter='data' on Python 3.12+ for additional safety. On 3.11
        # the parameter doesn't exist, so we fall back to manual validation
        # (already done above) and call extract() without it.
        if sys.version_info >= (3, 12):
            tar.extract(member, extract_path, filter="data")
        else:
            tar.extract(member, extract_path)
        extracted_names.append(member.name)

    return extracted_names
