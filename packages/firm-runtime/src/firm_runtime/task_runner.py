"""Child execution with bounded logs and confirmed guest cleanup."""

import threading
from concurrent.futures import Future
from dataclasses import dataclass, field

from firm_sandbox import Run

from .errors import FirmError
from .tasks import TaskExecutionError, TaskLaunch

LOG_LIMIT = 4 * 1024 * 1024
WORKER_INSTRUCTIONS = (
    "Complete the supplied task using ordinary file, search and shell tools as needed. "
    "Return one concise final answer. You share files with the caller; coordinate writes "
    "as instructed. Do not delegate or start background work."
)


@dataclass
class ChildHandle:
    run_id: str
    request: TaskLaunch
    future: Future = field(default_factory=Future)
    stopped: threading.Event = field(default_factory=threading.Event)
    confirmed: bool = False
    output_lock: threading.Lock = field(default_factory=threading.Lock)


class GuestTaskRunner:
    """Own ambiguous launches until the guest confirms their cgroup empty.

    TaskService owns admission/recovery exclusion through the durable answer commit.
    This runner's lock only protects local handles and its shutdown launch gate.
    """

    def __init__(self, runtime, sandbox, *, build_spec, parse_output=None):
        self.runtime, self.sandbox = runtime, sandbox
        self.build_spec, self.parse_output = build_spec, parse_output
        self._lock = threading.RLock()
        self._handles = {}
        self._closed = False
        self._workers = set()

    @property
    def closed(self):
        return self._closed

    def start(self, request: TaskLaunch):
        with self._lock:
            if self._closed:
                raise RuntimeError("Task runner is closed")
            with self.runtime.store.read() as db:
                row = db.execute(
                    "SELECT s.*,a.state FROM sandbox_runs s JOIN activations a "
                    "USING(activation_id) WHERE activation_id=?",
                    (request.caller_activation_id,),
                ).fetchone()
                task = db.execute(
                    "SELECT state FROM summary_runs WHERE run_id=? AND kind='task'",
                    (request.run_id,),
                ).fetchone()
            if (not row or row["state"] != "running" or row["stop_cause"]
                    or not row["vm_generation"] or not task or task["state"] != "running"):
                raise FirmError("Task parent is not live")
            parent = Run(row["role_id"], row["run_id"], row["vm_generation"])
            spec = self.build_spec(request, parent)
            payload = dict(argv=spec.argv, env=spec.env, secrets=spec.secrets,
                           secret_env=spec.secret_env, stdin=spec.stdin)
            handle = ChildHandle(request.run_id, request)
            self._handles[request.run_id] = handle
            worker = threading.Thread(
                target=self._execute, name=request.run_id,
                args=(handle, parent, payload, min(request.deadline, row["deadline"])),
            )
            self._workers.add(worker)
            try:
                worker.start()
            except Exception:
                self._workers.discard(worker)
                del self._handles[request.run_id]
                raise
            return handle

    def _output(self, handle, status):
        with handle.output_lock:
            output = read_child_logs(self.sandbox, handle.run_id)
            if self.parse_output:
                return self.parse_output(
                    handle.request, output, status.get("exit_code") or 0,
                    apply_holds=status.get("reason") == "exited" and not handle.stopped.is_set(),
                )
            return output.decode("utf-8")

    def _execute(self, handle, parent, payload, deadline):
        try:
            if handle.stopped.is_set():
                raise RuntimeError("Task stopped")
            self.sandbox.start_child(
                parent, run_id=handle.run_id,
                seconds_remaining=max(0, deadline - self.runtime.store.clock()), **payload,
            )
            while not handle.stopped.is_set():
                status = self.sandbox.child_status(handle.run_id)
                if not status["running"]:
                    handle.confirmed = status.get("confirmed") is True
                    try:
                        answer = self._output(handle, status)
                    except TaskExecutionError:
                        if status.get("reason") in {"timeout", "cancelled", "log_overflow"}:
                            raise TaskExecutionError(status["reason"]) from None
                        raise
                    if (status.get("exit_code") != 0 or status.get("reason") != "exited"
                            or not handle.confirmed):
                        reason = status.get("reason")
                        raise TaskExecutionError(reason if reason in {
                            "timeout", "cancelled", "log_overflow"
                        } else "provider_failure")
                    handle.future.set_result(answer)
                    return
                handle.stopped.wait(1.0)
            raise RuntimeError("Task stopped")
        except BaseException as exc:
            handle.future.set_exception(exc)
        finally:
            with self._lock:
                self._workers.discard(threading.current_thread())

    def wait(self, handle, timeout):
        return handle.future.result(timeout=timeout)

    def stop(self, handle):
        handle.stopped.set()
        if handle.future.done() and handle.confirmed:
            confirmed = True
        else:
            try:
                status = self.sandbox.stop_child(handle.run_id)
                confirmed = status.get("confirmed") is True
                # Even cancellation and failed cleanup may leave useful partial usage.
                if self.parse_output:
                    try:
                        self._output(handle, status)
                    except Exception:
                        pass
            except Exception:
                confirmed = False
        if confirmed:
            with self._lock:
                self._handles.pop(handle.run_id, None)
        return confirmed

    def reconcile(self, *, stopping=False):
        confirmed = reconcile_tasks(self.runtime, self.sandbox)
        with self._lock:
            for run_id in confirmed:
                self._handles.pop(run_id, None)
            handles = list(self._handles.values()) if stopping else []
        for handle in handles:
            self.stop(handle)

    def cancel_all(self):
        """Close the launch gate without blocking emergency VM power-off on guest RPCs."""
        with self._lock:
            self._closed = True
            for handle in self._handles.values():
                handle.stopped.set()

    def close(self):
        with self._lock:
            self._closed = True
            handles, workers = list(self._handles.values()), list(self._workers)
        for handle in handles:
            self.stop(handle)
        for worker in workers:
            worker.join()


def reconcile_tasks(runtime, sandbox):
    """Explicit stop receipts, including launch tombstones, are the sole exit proof."""
    confirmed_ids = set()
    for run_id in runtime.task_service.recovery_candidates():
        try:
            status = sandbox.stop_child(run_id)
            confirmed = status.get("confirmed") is True
        except Exception:
            continue
        if confirmed:
            # Restart has no handle, but the bounded guest journal may still contain usage.
            try:
                from .providers import record_task_output

                with runtime.store.read() as db:
                    row = db.execute(
                        "SELECT provider,account_id FROM summary_runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                record_task_output(
                    runtime, run_id, row["provider"], row["account_id"],
                    read_child_logs(sandbox, run_id), status.get("exit_code") or 0,
                    apply_holds=False,
                )
            except Exception:
                pass  # Unknown usage never invalidates positive process-exit evidence.
            runtime.task_service.recover_confirmed([run_id])
            confirmed_ids.add(run_id)
    return confirmed_ids


def read_child_logs(sandbox, run_id):
    output = bytearray()
    while len(output) < LOG_LIMIT:
        chunk = sandbox.child_logs(
            run_id, offset=len(output), limit=min(1024 * 1024, LOG_LIMIT - len(output)),
        )
        if not chunk:
            break
        output.extend(chunk)
    return bytes(output)
