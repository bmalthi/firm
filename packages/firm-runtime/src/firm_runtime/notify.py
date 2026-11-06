"""A nudge for an owner who is not looking; the console and `firm analyze` stay the record."""

import logging
import subprocess
import sys
import threading
import time

from .errors import FirmError

LOGGER = logging.getLogger(__name__)
THROTTLE_SECONDS = 600
MAX_TEXT = 200


def validate_owner(config):
    """``[owner] notifications = false`` silences the nudge; it is on by default."""
    owner = config.get("owner")
    if owner is None:
        return
    if not isinstance(owner, dict) or set(owner) - {"notifications"}:
        raise FirmError("owner has unsupported settings")
    if type(owner.get("notifications", True)) is not bool:
        raise FirmError("owner.notifications must be true or false")


def _literal(text):
    """An AppleScript string literal: one line, bounded, with quotes and backslashes escaped."""
    flat = " ".join(str(text).split())
    if len(flat) > MAX_TEXT:
        flat = flat[: MAX_TEXT - 1] + "…"
    return '"' + flat.replace("\\", "\\\\").replace('"', '\\"') + '"'


def script(title, body):
    return f"display notification {_literal(body)} with title {_literal(title)}"


def _launch(argv):
    # Never waited on: a slow notification centre must not hold a firm transaction.
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


class OwnerNotifier:
    """Post at most one notification per key (a role and a kind) per ten minutes."""

    def __init__(self, *, runner=_launch, clock=time.monotonic, platform=sys.platform):
        self.runner, self.clock = runner, clock
        self.available = platform == "darwin"
        self.sent = {}
        self.lock = threading.Lock()

    def notify_owner(self, title, body, *, key, enabled=True):
        """Best effort: never raises; returns whether a notification was posted."""
        if not self.available or not enabled:
            return False
        now = self.clock()
        with self.lock:
            last = self.sent.get(key)
            if last is not None and now - last < THROTTLE_SECONDS:
                return False
            self.sent[key] = now
        try:
            self.runner(["osascript", "-e", script(title, body)])
        except Exception:
            LOGGER.warning("Could not post an owner notification", exc_info=True)
            return False
        return True
