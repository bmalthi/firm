"""Offline simulation of the runtime contract: an in-memory executor and dispatcher.

This executor has no child processes or provider sessions. A production adapter must
supply process reconciliation and confirmed termination before using these transitions.
"""

import fcntl
from dataclasses import dataclass

from .errors import FirmError
from .runtime import Invocation, Runtime


@dataclass(frozen=True)
class Exit:
    outcome: str = "completed"
    failure: str | None = None
    category: str = "unknown"
    retry_after: float | None = None


class ControlledExecutor:
    def __init__(self):
        self.running: dict[str, Invocation] = {}
        self.exits: dict[str, Exit] = {}

    def launch(self, invocation: Invocation) -> None:
        self.running[invocation.activation_id] = invocation

    def complete(self, activation_id: str, result: Exit | None = None) -> None:
        if activation_id not in self.running:
            raise FirmError("controlled execution is not running")
        del self.running[activation_id]
        self.exits[activation_id] = result or Exit()

    def terminate(self, activation_id: str) -> None:
        # There is no external process: removing the invocation confirms its exit.
        self.running.pop(activation_id, None)
        self.exits.pop(activation_id, None)


class SimulationDispatcher:
    """Holds a local host lock for its full lifetime. All execution here is offline."""

    def __init__(self, runtime: Runtime, executor: ControlledExecutor):
        self.runtime = runtime
        self.executor = executor
        self._lock = None

    def __enter__(self):
        if self._lock is not None:
            raise FirmError("dispatcher is already open")
        handle = self.runtime.store.path.with_suffix(".host.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise FirmError("another dispatcher owns this firm") from exc
        self._lock = handle
        try:
            # Controlled executions cannot survive their host; terminate any same-instance
            # handles before reconciling the durable reservations left by an earlier host.
            for activation in self.runtime.status()["active"]:
                aid = activation["activation_id"]
                self.executor.terminate(aid)
                intentional = self.runtime.status()["state"] == "stopping"
                self.runtime.finish(
                    aid, "interrupted", category="owner_stop" if intentional else "host_restart"
                )
        except BaseException:
            self._release()
            raise
        return self

    def _require_open(self):
        if self._lock is None:
            raise FirmError("dispatcher must own the firm before controlling execution")

    def tick(self) -> list[Invocation]:
        self._require_open()
        for aid, result in list(self.executor.exits.items()):
            self.runtime.finish(
                aid,
                result.outcome,
                failure=result.failure,
                category=result.category,
                retry_after=result.retry_after,
            )
            del self.executor.exits[aid]
        launched = []
        # Controlled execution decides pending hire levels inline before admission.
        self.runtime.levels.tick(background=False)
        while invocation := self.runtime.admit():
            # Serialize the final admission gate check with launch. Controlled launch is
            # immediate; a provider adapter needs explicit in-flight launch cancellation.
            with self.runtime.store.transaction():
                try:
                    self.runtime.mark_started(invocation.activation_id)
                except FirmError:
                    self.runtime.finish(
                        invocation.activation_id, "interrupted", category="owner_stop"
                    )
                    continue
                self.executor.launch(invocation)
            launched.append(invocation)
        return launched

    def stop(self, force: bool = False) -> dict:
        self._require_open()
        self.runtime.stop(force)
        if force:
            for activation in self.runtime.status()["active"]:
                aid = activation["activation_id"]
                # Completed executions retain their successful handoff, even if the
                # dispatcher has not yet collected the completion when stop arrives.
                result = self.executor.exits.pop(aid, None)
                if result is not None:
                    self.runtime.finish(
                        aid,
                        result.outcome,
                        failure=result.failure,
                        category=result.category,
                        retry_after=result.retry_after,
                    )
                else:
                    self.executor.terminate(aid)
                    self.runtime.finish(aid, "interrupted", category="owner_stop")
        return self.runtime.status()

    def _release(self):
        if self._lock is not None:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.stop(force=True)
        finally:
            self._release()
