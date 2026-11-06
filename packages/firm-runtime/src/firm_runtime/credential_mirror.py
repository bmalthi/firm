"""Guest access-token mirror. Refresh credentials never enter the sandbox."""

import json
import os
import signal
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CredentialMirror:
    def __init__(self, path, *, url=None, token=None, interval=30):
        self.path = Path(path)
        self.url = url or os.environ["FIRM_CREDENTIALS_URL"]
        self.token = (
            token or os.environ.get("FIRM_SUMMARY_TOKEN") or os.environ["FIRM_ACTIVATION_TOKEN"]
        )
        self.interval = interval
        self.stop = threading.Event()
        self.thread = None

    def refresh(self):
        request = urllib.request.Request(
            self.url, headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Credential response too large")
        snapshot = json.loads(raw)
        if snapshot["kind"] == "oauth-token":
            # Explicit long-lived tokens have no renewal credential.
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = snapshot["data"]
            return
        raw = json.dumps(snapshot["data"]).encode()
        if self.path.exists() and self.path.read_bytes() == raw:
            return
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".auth-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _poll(self):
        while not self.stop.wait(self.interval):
            try:
                self.refresh()
            except Exception:
                # Keep the last access token through temporary network failure.
                # Never print credential responses or exception bodies.
                pass

    def __enter__(self):
        try:
            self.refresh()
        except Exception as exc:
            code = (
                "authentication_failed"
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 403
                else "service_unavailable"
            )
            print(json.dumps({"type": "error", "error": {"code": code}}), flush=True)
            raise MirrorUnavailable from None
        self.thread = threading.Thread(target=self._poll, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=21)


class MirrorUnavailable(Exception):
    pass


def run_claude(command, path):
    try:
        mirror = CredentialMirror(path)
        mirror.__enter__()
    except MirrorUnavailable:
        return 72
    try:
        process = subprocess.Popen(command)

        def forward(signum, _frame):
            if process.poll() is None:
                process.send_signal(signum)

        previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            return process.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    finally:
        mirror.__exit__()
