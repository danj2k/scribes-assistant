#!/usr/bin/env python3
"""Download the sherpa-onnx Whisper small model.

Standalone script that can be run from the CLI or imported by the transcriber.
Downloads the model archive from GitHub releases, extracts it, and verifies
the required files exist.

Usage:
    python -m scripts.download_model                  # default: data/models/whisper-small
    python -m scripts.download_model /data/models/whisper-small
    python scripts/download_model.py /data/models/whisper-small
"""

import sys
import os
import time
import hashlib
import urllib.request
import tarfile
from pathlib import Path
from typing import Optional

# sherpa-onnx Whisper small model URL
DEFAULT_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "asr-models/sherpa-onnx-whisper-small.tar.bz2"
)

# Expected SHA-256 hash for the model archive
EXPECTED_SHA256 = "486a46afbb7ba798507190ffe02fea2dd726049af212e774537efac6afb210a6"

# Files that must exist after extraction
REQUIRED_FILES = [
    "encoder-epoch-99-avg-1.onnx",
    "decoder-epoch-99-avg-1.onnx",
    "joiner-epoch-99-avg-1.onnx",
]

# Expected size in bytes for progress display (approximate)
ARCHIVE_SIZE_MB = 630


def _progress_hook(block_num: int, block_size: int, total_size: int) -> None:
    """Print download progress to stderr."""
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(100.0, downloaded / total_size * 100)
        mb_downloaded = downloaded / (1024 * 1024)
        mb_total = total_size / (1024 * 1024)
        sys.stderr.write(
            f"\r  Downloading: {pct:5.1f}% ({mb_downloaded:.0f}/{mb_total:.0f} MB)"
        )
    else:
        mb_downloaded = downloaded / (1024 * 1024)
        sys.stderr.write(f"\r  Downloading: {mb_downloaded:.0f} MB")
    sys.stderr.flush()


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file, reading in chunks to handle large files."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def download_model(
    model_dir: str,
    model_url: Optional[str] = None,
    force: bool = False,
) -> str:
    """Download or verify the sherpa-onnx Whisper small model.

    Args:
        model_dir: Directory to store the model files.
        model_url: Override URL for model download.
        force: If True, re-download even if files exist.

    Returns:
        Path to the model directory.

    Raises:
        RuntimeError: If download or extraction fails.
        FileNotFoundError: If required files are missing after extraction.
    """
    model_path = Path(model_dir)
    model_path.mkdir(parents=True, exist_ok=True)

    # Check if model already exists
    if not force:
        all_exist = all((model_path / f).exists() for f in REQUIRED_FILES)
        if all_exist:
            print(f"Model already present at {model_path}")
            return str(model_path)

    url = model_url or DEFAULT_MODEL_URL
    archive_path = model_path / "model.tar.bz2"

    print(f"Downloading Whisper small model to {model_path}...")
    print(f"  Source: {url}")
    print(f"  Approximate size: ~{ARCHIVE_SIZE_MB} MB")
    print()

    start_time = time.time()

    try:
        urllib.request.urlretrieve(url, str(archive_path), reporthook=_progress_hook)
        sys.stderr.write("\n")
    except Exception as e:
        if archive_path.exists():
            archive_path.unlink()
        raise RuntimeError(f"Download failed: {e}") from e

    elapsed = time.time() - start_time
    print(f"  Download completed in {elapsed:.1f}s")

    # Verify SHA-256 hash
    print("  Verifying SHA-256 hash...")
    actual_hash = _sha256_file(archive_path)
    if actual_hash != EXPECTED_SHA256:
        archive_path.unlink()
        raise RuntimeError(
            f"SHA-256 verification failed!"
            f"  Expected: {EXPECTED_SHA256}"
            f"  Got:      {actual_hash}"
            f"The downloaded file may be corrupted or tampered with."
            f"Please try again or report this issue."
        )
    print(f"  Hash verified: {actual_hash[:16]}...")

    # Extract
    print("  Extracting archive...")
    try:
        with tarfile.open(archive_path, "r:bz2") as tar:
            tar.extractall(model_path)
    except Exception as e:
        raise RuntimeError(f"Extraction failed: {e}") from e
    finally:
        if archive_path.exists():
            archive_path.unlink()

    # Verify
    print("  Verifying files...")
    missing = [f for f in REQUIRED_FILES if not (model_path / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"Model extraction incomplete — missing files: {missing}\n"
            f"Expected in: {model_path}"
        )

    size_mb = sum(
        (model_path / f).stat().st_size for f in REQUIRED_FILES
    ) / (1024 * 1024)
    print(f"  Model ready: {size_mb:.0f} MB across {len(REQUIRED_FILES)} files")
    print()
    return str(model_path)


def main() -> None:
    """CLI entry point."""
    model_dir = sys.argv[1] if len(sys.argv) > 1 else "data/models/whisper-small"
    force = "--force" in sys.argv or "-f" in sys.argv

    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    try:
        download_model(model_dir, force=force)
    except (RuntimeError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
