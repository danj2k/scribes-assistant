"""Tests for SIGTERM graceful shutdown in the transcriber main loop.

Bug #11: Docker sends SIGTERM on `docker stop`. Without a handler,
the process is SIGKILLed after the grace period, losing in-progress
transcription work. The fix registers a SIGTERM handler that sets a
module-level flag; the main loop checks this flag between operations
and exits cleanly after the current transcription completes.
"""

import signal
import transcriber.main as transcriber_main


class TestSigtermHandler:
    """Tests for _handle_sigterm and the _shutdown_requested flag."""

    def setup_method(self):
        """Reset the shutdown flag before each test."""
        transcriber_main._shutdown_requested = False

    def test_handler_sets_shutdown_flag(self):
        """Calling _handle_sigterm sets _shutdown_requested to True."""
        assert transcriber_main._shutdown_requested is False
        transcriber_main._handle_sigterm(signal.SIGTERM, None)
        assert transcriber_main._shutdown_requested is True

    def test_handler_is_idempotent(self):
        """Calling the handler multiple times keeps the flag set."""
        transcriber_main._handle_sigterm(signal.SIGTERM, None)
        transcriber_main._handle_sigterm(signal.SIGTERM, None)
        assert transcriber_main._shutdown_requested is True

    def test_handler_does_not_raise(self):
        """The handler must not raise — it runs in a signal context."""
        # If it raised, the signal would propagate an exception into
        # potentially non-Python code (C extensions like sherpa-onnx).
        transcriber_main._handle_sigterm(signal.SIGTERM, None)

    def test_flag_resets(self):
        """The flag can be reset (used by the test fixture and by the
        module itself on fresh imports)."""
        transcriber_main._shutdown_requested = True
        transcriber_main._shutdown_requested = False
        assert transcriber_main._shutdown_requested is False
