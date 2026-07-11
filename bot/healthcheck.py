"""Health check script for the bot Docker container.

Reads the heartbeat file written by the bot process and verifies it
was updated recently.  A stale or absent heartbeat indicates the bot
is not running or its event loop has died.

Exits 0 (healthy) or 1 (unhealthy).
"""
import sys
import time
from pathlib import Path

HEARTBEAT_FILE = Path("/data/bot.heartbeat")
# The bot updates the heartbeat every 30 seconds.  Allow up to 90
# seconds (3 missed cycles) before declaring unhealthy — tolerates
# momentary I/O pauses without false-positives.
STALE_THRESHOLD_SECONDS = 90


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
