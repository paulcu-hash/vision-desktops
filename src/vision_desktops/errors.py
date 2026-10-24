"""errors: never swallow silently - visible exceptions with a ring buffer.

Two rules from the project collided with reality: the code is full of
`except Exception: pass` (best effort everywhere), but a swallowed error is
indistinguishable from success - and that is exactly how the argtypes
clobbering bug (own WinDLL instances, see windows.py/capture.py/hop.py) and
the PID-recycling bug hid for so long.

This module makes every swallow visible WITHOUT changing behaviour:

  log.debug("...", exc_info=True)   # in every former `pass` body
  RingBufferHandler                 # keeps the last N errors in memory

The ring buffer is process-local (the MCP server process). It is read via
the `diagnose` MCP tool or `python -m vision_desktops.diagnose`. Nothing is
written to stdout (the MCP transport owns stdout - logs only ever go to an
optional stderr handler, opt-in via VISION_DESKTOPS_LOG=stderr).
"""
import collections
import datetime
import logging
import os
import threading
import traceback

DEFAULT_CAPACITY = 200

LOGGER_NAME = "vision_desktops"


class RingBufferHandler(logging.Handler):
    """Keeps the last `capacity` log records in memory (oldest dropped).

    Stores a structured entry per record: time, level, logger, message and
    the formatted traceback (from exc_info) - so `diagnose` can show WHY an
    error happened, not just that something was logged."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        super().__init__(level=logging.DEBUG)
        self.capacity = max(1, int(capacity))
        self._entries = collections.deque(maxlen=self.capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = self._entry(record)
        except Exception:  # noqa: BLE001 (a handler must never break logging)
            self.handleError(record)
            return
        with self._lock:
            self._entries.append(entry)

    @staticmethod
    def _entry(record: logging.LogRecord) -> dict:
        tb = ""
        if record.exc_info:
            tb = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        return {
            "time": datetime.datetime.fromtimestamp(
                record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "traceback": tb,
        }

    def recent(self, limit: int = 20) -> list:
        """Newest first, at most `limit` entries."""
        with self._lock:
            items = list(self._entries)
        if limit and limit > 0:
            items = items[-int(limit):]
        return list(reversed(items))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def ring_handler() -> RingBufferHandler | None:
    """The installed ring buffer handler (None when not installed)."""
    for h in _get_logger().handlers:
        if isinstance(h, RingBufferHandler):
            return h
    return None


def install(capacity: int = DEFAULT_CAPACITY,
            stderr: bool | None = None) -> RingBufferHandler:
    """Attach the ring buffer to the package logger. Idempotent.

    stderr: also print records to stderr (default: only when
    VISION_DESKTOPS_LOG is set to 1/stderr/debug). NEVER stdout - that is
    the MCP transport."""
    log = _get_logger()
    log.setLevel(logging.DEBUG)  # the ring buffer wants everything
    ring = ring_handler()
    if ring is None:
        ring = RingBufferHandler(capacity)
        log.addHandler(ring)
    if stderr is None:
        stderr = str(os.environ.get("VISION_DESKTOPS_LOG", "")).strip() \
            .lower() in ("1", "true", "stderr", "debug")
    if stderr and not any(
            isinstance(h, logging.StreamHandler)
            and getattr(h, "stream", None) is not None
            and getattr(h.stream, "name", "") == "<stderr>"
            for h in log.handlers):
        sh = logging.StreamHandler()  # defaults to sys.stderr
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(sh)
    return ring


def recent(limit: int = 20) -> list:
    """Newest-first snapshot of the ring buffer (empty list when not
    installed)."""
    ring = ring_handler()
    return ring.recent(limit) if ring else []


def clear() -> None:
    ring = ring_handler()
    if ring:
        ring.clear()
