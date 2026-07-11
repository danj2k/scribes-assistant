"""Health check script for the transcriber Docker container.

Reads the heartbeat file written by the transcriber process and
verifies it was updated recently.  The transcriber writes the heartbeat
after each poll cycle, so a stale or absent file means the worker is
either stuck in a long transcription, has crashed, or hasn't finished
loading its model yet.

Exits 0 (healthy) or 1 (unhealthy).
"""
import sys
import time
from pathlib import Path

HEARTBEAT_FILE = Path("/data/transcriber.heartbeat")
# The transcriber polls every 10 seconds by default, but a single
# transcription can take several minutes for long recordings.  Allow
# up to 5 minutes before declaring unhealthy — enough headroom for
# a long file, but short enough to catch a genuinely stuck process.
STALE_THRESHOLD_SECONDS = 300


def main():
    if not HEARTBEAT_FILE.exists():
        print("unhealthy: heartbeat file not found", file=sys.stderr)
        sys.exit(1)

    try:
        mtime = HEARTBEAT_FILE.stat().st_mtime
    except OSError as exc:
        print(f"unhealthy: cannot stat heartbeat: {exc}", file=sys.stderr)
        sys.exit(1)

    age = time.time() - mtime
    if age > STALE_THRESHOLD_SECONDS:
        print(
            f"unhealthy: heartbeat is {age:.0f}s old "
            f"(threshold {STALE_THRESHOLD_SECONDS}s)",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"healthy: heartbeat {age:.0f}s old")
    sys.exit(0)


if __name__ == "__main__":
    main()
