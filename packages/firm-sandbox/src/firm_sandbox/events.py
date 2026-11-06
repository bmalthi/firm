"""Host observations of the sandbox, stored outside guest mounts."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from .locking import locked
from .models import Config, Run


class Events:
    def __init__(self, config: Config):
        self.config = config

    def write(self, kind: str, run: Run | None = None, **fields) -> dict:
        record = {
            "time": datetime.now(UTC).isoformat(),
            "firm_id": self.config.firm_id,
            "kind": kind,
            **fields,
        }
        if run:
            record.update(role_id=run.role_id, run_id=run.run_id)
        # One append write per small event; raw command, output, env and secrets are omitted.
        encoded = (json.dumps(record) + "\n").encode()
        self.config.durable.mkdir(mode=0o700, exist_ok=True)
        with locked(self.config.locks, "events"):
            fd = os.open(
                self.config.durable / "events.jsonl",
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o600,
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
        return record

    def read(self) -> list[dict]:
        path = self.config.durable / "events.jsonl"
        with locked(self.config.locks, "events", shared=True):
            return (
                [json.loads(line) for line in path.read_text().splitlines()]
                if path.exists()
                else []
            )
