"""Public account credential facade; guests never receive renewal authority."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import agy_auth, claude_auth, codex_auth, oauth_auth
from .credential_files import MAX_AUTH_BYTES, CredentialFile, failure

DEFAULT_MARGIN = object()


@dataclass(frozen=True)
class _Backend:
    read: Callable
    project: Callable
    snapshot: Callable
    default_margin: int
    prepare_save: Callable | None = None


def _backend(provider):
    if provider == "agy":
        return _Backend(
            agy_auth.read_auth,
            agy_auth.project,
            agy_auth.snapshot,
            agy_auth.RENEWAL_MARGIN,
            agy_auth.prepare_profile,
        )
    if provider in ("claude", "codex"):
        codec = claude_auth if provider == "claude" else codex_auth
        reader = claude_auth.read if provider == "claude" else codex_auth.read_auth
        return _Backend(
            reader,
            codec.project,
            partial(oauth_auth.snapshot, provider, codec),
            oauth_auth.REFRESH_MARGIN,
        )
    raise failure("credential_provider_invalid")


def guest_credentials(provider, raw):
    """Project an already-read bundle without network or filesystem mutation."""
    return _backend(provider).project(raw)


def access_credentials(provider: str, raw: bytes) -> bytes:
    """Encode only projected access credentials for a sandbox launcher."""
    snapshot = guest_credentials(provider, raw)
    return (
        snapshot["data"].encode()
        if snapshot["kind"] == "oauth-token"
        else json.dumps(snapshot["data"]).encode()
    )


class CredentialStore:
    def __init__(
        self, provider, auth_file, *, clock=time.time, refresh=oauth_auth.refresh_credentials
    ):
        self.provider = provider
        self.path = Path(auth_file).expanduser()
        self.clock, self.refresh = clock, refresh
        self.backend = _backend(provider)
        self.files = CredentialFile(self.path, self.backend.read)

    def save(self, raw):
        """Validate and install owner credentials privately under the renewal lock."""
        if len(raw) > MAX_AUTH_BYTES:
            raise failure("credential_too_large")
        self.backend.project(raw)
        if self.backend.prepare_save is not None:
            self.backend.prepare_save(self.path)
        self.files.save(raw)

    def snapshot(self, force=False, *, margin=DEFAULT_MARGIN):
        """Return guest credentials; margin=None disables age-triggered renewal.

        The default trigger is 600 seconds for HTTP OAuth and 3,000 seconds for
        AGY. A margin triggers renewal rather than guaranteeing token lifetime.
        Native recovery still runs with margin=None, and force requests renewal
        for renewable modes regardless of age.
        """
        if margin is not DEFAULT_MARGIN and margin is not None:
            try:
                valid = type(margin) in (int, float) and math.isfinite(margin) and margin >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError(
                    "credential renewal margin must be a finite non-negative number or None"
                )
        if margin is DEFAULT_MARGIN:
            margin = self.backend.default_margin
        return self.backend.snapshot(
            self.files, force, margin=margin, clock=self.clock, refresh=self.refresh
        )
