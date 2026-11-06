from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import yaml


def resize_instance(instance: Path, cpus: int, memory_gib: int) -> None:
    """Write new CPU and memory settings into a stopped Lima instance's definition.

    Only these two keys change, so the guest keeps its disk, identity and boot
    policy; the caller owns the record that says what the VM was created with.
    """
    definition = instance / "lima.yaml"
    config = yaml.safe_load(definition.read_text())
    config.update(cpus=cpus, memory=f"{memory_gib}GiB")
    definition.write_text(yaml.safe_dump(config, sort_keys=False))


def mac_sudo_command(*argv: str) -> str:
    """Build a Mac guest sudo command using the control account's private password file.

    sudo is named by its absolute path, so the control account's PATH never picks it.
    sudo reads the password from the file on its stdin, and would hand that open file
    to the command as its own stdin: anything the command starts, a station's Homebrew
    included, could rewind it and read the password. So the command is started by a
    shell that first puts /dev/null there.
    """
    command = shlex.join(["/usr/bin/sudo", "-k", "-S", "-p", "",
                          "/bin/sh", "-c", 'exec "$@" 0</dev/null', "sh", *argv])
    return command + " < /Users/firmvm/password"


def parse_lima_list(raw: bytes | str) -> list[dict]:
    """Decode Lima's object, array, or newline-delimited JSON list response."""
    if isinstance(raw, bytes):
        raw = raw.decode(errors="strict")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        try:
            value = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise ValueError("Lima returned invalid JSON") from exc
    rows = value if isinstance(value, list) else [value]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Lima returned invalid VM records")
    return rows


def lima_status(row: dict) -> str:
    """Map a ``parse_lima_list`` row's status to ``"running"``, ``"stopped"``, or its raw text.

    Lima's status casing is not a contract callers should depend on, so the two
    recognized states are normalized; any other status (Broken, Installing,
    Unknown, ...) is returned unchanged for the caller to report or raise on.
    """
    status = str(row.get("status", ""))
    lowered = status.lower()
    if lowered in ("running", "stopped"):
        return lowered
    return status


class CommandError(RuntimeError):
    pass


class VMStateError(RuntimeError):
    """Lima reports an instance that needs recovery before normal operation."""

    def __init__(self, status):
        self.status = status
        super().__init__(f"VM state needs recovery: {status}")


class Commands:
    def __init__(self, env: dict[str, str]):
        self.env = env

    def run(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        check: bool = True,
        timeout: float | None = 120,
        merge_output: bool = False,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            list(argv),
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_output else subprocess.PIPE,
            env=self.env,
            timeout=timeout,
        )
        if check and result.returncode:
            # Never include stdin or the complete command: both can contain credentials.
            detail = (result.stderr or b"").decode(errors="replace").strip()
            raise CommandError(f"{argv[0]} failed ({result.returncode}): {detail}")
        return result

    def stream(self, argv: Sequence[str]) -> Iterator[bytes]:
        """Yield merged output; closing the iterator also reaps the client process."""
        process = subprocess.Popen(
            list(argv), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        try:
            while chunk := process.stdout.read1(65536):
                yield chunk
            if process.wait():
                raise CommandError(f"{argv[0]} stream failed ({process.returncode})")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
