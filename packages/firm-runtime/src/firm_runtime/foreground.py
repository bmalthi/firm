"""Foreground owner host: local console, role MCP, and the sandbox dispatcher."""

import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from firm_sandbox import Config, Sandbox

from .analysis_history import AnalysisObserver
from .console import build_console_app, console_scope
from .errors import FirmError
from .execution_activity import DEFAULT_IDLE_TIMEOUT
from .hosting import LocalServer
from .mcp_http import build_http_app
from .notify import OwnerNotifier
from .providers import Providers
from .sandbox_dispatcher import SandboxDispatcher

HOST_RECORD = "owner-host.json"
HOST_LOG = "owner-host.log"
HOST_LOG_MAX = 1024 * 1024
HOST_LOG_ENV = "FIRM_OWNER_HOST_LOG"


class BoundedHostLog:
    """Process-owned rotating stdout/stderr for the detached local host."""

    encoding = "utf-8"

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._fd = None

    def _open(self):
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            os.close(descriptor)
            raise FirmError("Detached host log is not a private owner file")
        self._fd = descriptor

    def _rotate(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        second = self.path.with_name(self.path.name + ".2")
        first = self.path.with_name(self.path.name + ".1")
        second.unlink(missing_ok=True)
        if first.exists() or first.is_symlink():
            os.replace(first, second)
        if self.path.exists() or self.path.is_symlink():
            os.replace(self.path, first)
        self._open()

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        data = text.encode("utf-8", errors="replace")
        with self._lock:
            offset = 0
            while offset < len(data):
                if self._fd is None:
                    self._open()
                size = os.fstat(self._fd).st_size
                if size >= HOST_LOG_MAX:
                    self._rotate()
                    size = 0
                chunk = data[offset : offset + HOST_LOG_MAX - size]
                offset += os.write(self._fd, chunk)
        return len(text)

    def flush(self):
        return None

    def isatty(self):
        return False

    def writable(self):
        return True

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None


class HostControl:
    """Thread-safe lifecycle requests made by the authenticated local console."""

    def __init__(self, stop):
        self._stop = stop
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._start = False
        self._close = False
        self._startup_error = None

    def request_start(self):
        with self._lock:
            self._start = True
        self._wakeup.set()

    def set_startup_error(self, message):
        with self._lock:
            self._startup_error = message

    def clear_startup_error(self):
        with self._lock:
            self._startup_error = None

    def startup_error(self):
        with self._lock:
            return self._startup_error

    def request_close(self):
        with self._lock:
            self._close = True
        self._wakeup.set()

    def consume(self, timeout=1.0):
        self._wakeup.wait(timeout)
        self._wakeup.clear()
        with self._lock:
            start, close = self._start, self._close
            self._start = self._close = False
        return start, close

    def stop(self, force=False):
        with self._lock:
            self._start = False
        return self._stop(force=force)


def _write_host_record(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    path.chmod(0o600)


def _read_host_record(path: Path) -> dict | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or not all(type(value.get(key)) is int for key in ("pid", "mcp_port", "console_port"))
            or not isinstance(value.get("firm_id"), str)
            or not isinstance(value.get("console_scope"), str)
        ):
            return None
        return value
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _console_read(record: dict, durable: Path, endpoint: str) -> dict | None:
    try:
        os.kill(record["pid"], 0)
    except (OSError, TypeError):
        return None
    key_path = durable / "console-key"
    try:
        descriptor = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="ascii") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                return None
            key = stream.read(128).strip()
    except (OSError, UnicodeError):
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", key) is None:
        return None
    expected_scope = console_scope(durable / "firm.sqlite3", record["firm_id"])
    if record.get("console_scope") != expected_scope:
        return None
    port = record["console_port"]
    cookie = f"firm_owner_console_127.0.0.1_{port}={key}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{endpoint}",
        headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            # A startup error carries its cause's text, such as a command's stderr.
            value = json.loads(response.read(256 * 1024))
            return value if response.status == 200 and isinstance(value, dict) else None
    except urllib.error.HTTPError:
        return None
    except (OSError, TimeoutError, ValueError, AttributeError):
        return None


def _console_alive(record: dict, durable: Path) -> bool:
    value = _console_read(record, durable, "/api/session")
    return bool(
        value
        and value.get("principal") == "owner"
        and value.get("firm_id") == record["firm_id"]
        and value.get("console_scope") == record["console_scope"]
    )


def startup_error(directory: Path) -> str | None:
    """Read the live host's startup failure from its authenticated console."""
    durable = directory / "durable"
    record = _read_host_record(durable / HOST_RECORD)
    if record is None or not _console_alive(record, durable):
        return None
    value = _console_read(record, durable, "/api/control/status")
    error = value.get("startup_error") if value else None
    return error if isinstance(error, str) else None


def _host_lock_active(directory: Path) -> bool:
    path = directory / "durable" / "firm.host.lock"
    if not path.exists() and not path.is_symlink():
        return False
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise FirmError("Cannot safely inspect the existing Firm host lock") from exc
    with os.fdopen(descriptor, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise FirmError("Existing Firm host lock is not an owner file")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return False


def configured_providers(runtime):
    return Providers(runtime)


def drive_announcement(access: dict) -> str:
    """The shared-files block the foreground host prints once the drive is up."""
    return "\n".join(
        [
            f"Drive: {access['url']}",
            f"  Log in as {access['user']} with password {access['password']}",
            '  Press ⌘K in Finder, paste the address, then Connect; tick "Remember this',
            '  password in my keychain". Eject the drive before stopping the firm.',
            "  The address and password stay the same across restarts, so reconnect from",
            "  Go › Connect to Server › Recent Servers.",
            "  A server named \"127.0.0.1\" in Finder's sidebar is only Finder's memory of a",
            "  server, not the firm drive: clicking it browses that server rather than the",
            '  share and can sit on "Connecting…". Always connect with the address above.',
        ]
    )


def console_announcement(port: int, key: str) -> str:
    """The console block `firm start` prints. The address and key are stable across restarts."""
    return "\n".join(
        [
            f"Console: http://127.0.0.1:{port}/",
            "  Bookmark that address. A browser that has not opened this firm's console",
            "  before — or one whose cookies were cleared — unlocks it once with",
            f"  http://127.0.0.1:{port}/unlock/{key}",
            "  The key lives in the firm's durable directory and does not change.",
        ]
    )


def serve(
    runtime,
    *,
    sandbox_config: Path,
    mcp_port=8765,
    console_port=8766,
    timeout=DEFAULT_IDLE_TIMEOUT,
    wait_for_resume=False,
):
    host_log = os.environ.get(HOST_LOG_ENV)
    if host_log:
        requested_log = Path(host_log).resolve()
        if requested_log.parent != runtime.store.path.parent.resolve():
            raise FirmError("Detached host log must be inside this firm's durable directory")
        bounded_log = BoundedHostLog(requested_log)
        sys.stdout = sys.stderr = bounded_log
    config = Config.load(sandbox_config)
    if config.root.resolve() != runtime.store.path.resolve().parent.parent:
        raise FirmError("sandbox configuration belongs to a different firm directory")
    if config.firm_id != runtime.status()["firm_id"]:
        raise FirmError("sandbox firm.id must match the initialized firm's id")
    providers = configured_providers(runtime)
    runtime.messaging.notifier = OwnerNotifier()
    sandbox = Sandbox(config)
    host_address = sandbox.host_address()
    endpoint = f"{host_address}:{mcp_port}"
    sandbox.configure_runtime(mcp_port)
    shutdown = False
    signal_drain_applied = False
    drive_announced = False
    start_pending = False
    startup_failure = None
    host_control = None
    host_record = runtime.store.path.parent / HOST_RECORD

    def interrupt(signum, frame):
        nonlocal shutdown
        # A signal can interrupt BEGIN before Store records its active connection.
        # Never reenter SQLite (or other blocking work) from the signal handler.
        shutdown = True

    def apply_signal_request():
        nonlocal signal_drain_applied, start_pending
        if shutdown and not signal_drain_applied:
            start_pending = False
            dispatcher.stop()
            signal_drain_applied = True
            print(
                "Draining before closing the host. "
                "Use firm stop <directory> --force in another terminal to hard stop.",
                flush=True,
            )

    def console_app(port):
        app = build_console_app(
            runtime,
            allowed_host=f"127.0.0.1:{port}",
            sandbox=sandbox,
            control=host_control,
        )
        print(console_announcement(port, app.state.console_key), flush=True)
        return app

    def announce_drive():
        # The address, login and password are stable for the life of the firm.
        nonlocal drive_announced
        if drive_announced:
            return
        try:
            access = sandbox.data_status()
        except RuntimeError:
            return
        drive_announced = True
        print(drive_announcement(access), flush=True)

    def mcp_app(port):
        return build_http_app(
            runtime,
            allowed_hosts=[f"127.0.0.1:{port}", f"{host_address}:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        )

    with SandboxDispatcher(
        runtime, sandbox, providers, mcp_url=f"http://{endpoint}/mcp", execution_timeout=timeout
    ) as dispatcher:
        host_control = HostControl(dispatcher.stop)
        observer = AnalysisObserver(
            runtime.store, sandbox, messaging=runtime.messaging,
            sandbox_toml=Path(sandbox_config).resolve(),
        )
        observer.start()
        try:
            with (
                LocalServer(mcp_app, port=mcp_port) as mcp_server,
                LocalServer(console_app, port=console_port) as console_server,
            ):
                _write_host_record(
                    host_record,
                    {
                        "pid": os.getpid(),
                        "mcp_port": getattr(mcp_server, "port", mcp_port),
                        "console_port": getattr(console_server, "port", console_port),
                        "firm_id": runtime.status()["firm_id"],
                        "console_scope": console_scope(
                            runtime.store.path, runtime.status()["firm_id"]
                        ),
                    },
                )
                previous = {
                    sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)
                }
                try:
                    start_pending = runtime.status()["state"] not in {"draining", "stopping"}
                    last_error = None
                    while True:
                        firm_state = runtime.status()["state"]
                        infrastructure_state = None
                        if startup_failure:
                            with runtime.store.read() as db:
                                host = db.execute(
                                    "SELECT infrastructure_state FROM execution_host "
                                    "WHERE singleton=1"
                                ).fetchone()
                            infrastructure_state = (
                                host["infrastructure_state"] if host is not None else "stopped"
                            )
                        if (
                            wait_for_resume
                            and not start_pending
                            and firm_state == "stopped"
                            and (not startup_failure or infrastructure_state == "stopped")
                        ):
                            if startup_failure:
                                host_control.set_startup_error(
                                    f"{startup_failure}. VM shutdown is confirmed; correct the "
                                    "cause, then retry."
                                )
                            resume, close = host_control.consume()
                            if close:
                                print("The owner closed the Firm host.", flush=True)
                                return
                            if resume:
                                start_pending = True
                        if startup_failure and (
                            firm_state not in {"stopping", "stopped"}
                            or infrastructure_state != "stopped"
                        ):
                            try:
                                dispatcher.stop(force=True)
                            except (
                                FirmError,
                                OSError,
                                RuntimeError,
                                subprocess.SubprocessError,
                            ) as stop_error:
                                host_control.set_startup_error(
                                    f"{startup_failure}. The host could not close admission: "
                                    f"{stop_error}. VM shutdown is not confirmed, so retry "
                                    "remains disabled."
                                )
                        starting = start_pending
                        try:
                            apply_signal_request()
                            if start_pending:
                                start_pending = False
                                dispatcher.start()
                                startup_failure = None
                                host_control.clear_startup_error()
                                starting = False
                                announce_drive()
                            apply_signal_request()
                            dispatcher.tick()
                            observer.sample()
                            last_error = None
                        except (
                            FirmError,
                            OSError,
                            RuntimeError,
                            subprocess.SubprocessError,
                        ) as exc:
                            if starting and not shutdown:
                                if not wait_for_resume:
                                    raise  # Non-persistent serve reports to its caller.
                                startup_failure = f"Firm startup failed: {exc}"
                                status_message = (
                                    f"{startup_failure}. The host is confirming VM shutdown "
                                    "before allowing retry."
                                )
                                host_control.set_startup_error(status_message)
                                print(status_message, flush=True)
                                try:
                                    dispatcher.stop(force=True)
                                except (
                                    FirmError,
                                    OSError,
                                    RuntimeError,
                                    subprocess.SubprocessError,
                                ) as stop_error:
                                    host_control.set_startup_error(
                                        f"{startup_failure}. The shutdown request failed: "
                                        f"{stop_error}. Resume remains disabled until VM "
                                        "shutdown is confirmed."
                                    )
                                starting = False
                            else:
                                message = f"Execution host: {exc}"
                                if startup_failure and runtime.status()["state"] != "stopped":
                                    host_control.set_startup_error(
                                        f"{startup_failure}. VM shutdown is not yet confirmed: "
                                        f"{exc}. Resume remains disabled."
                                    )
                                if message != last_error:
                                    print(message, flush=True)
                                    last_error = message
                        # Resource history is independent of the tick's outcome: a failing
                        # tick is exactly when the machines' counters are worth having.
                        observer.resources()
                        # A local or external drain has finished; release the host's ports.
                        if runtime.status()["state"] == "stopped" and not start_pending:
                            if shutdown:
                                print(
                                    "The Firm is stopped; closing the host after the signal.",
                                    flush=True,
                                )
                                return
                            if not wait_for_resume:
                                print("The firm is stopped; closing the host.", flush=True)
                                return
                            continue
                        time.sleep(1)
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
        finally:
            observer.stop()
            host_record.unlink(missing_ok=True)


def launch_local_host(
    directory: Path,
    *,
    sandbox_config: Path,
    mcp_port=8765,
    console_port=8766,
    timeout=DEFAULT_IDLE_TIMEOUT,
    open_browser=True,
) -> dict:
    """Start one detached host or reconnect to its still-live local console."""
    directory = Path(directory).resolve()
    from .compatibility import validate_firm

    validate_firm(directory)
    durable = directory / "durable"
    durable.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = durable / HOST_RECORD
    lock_path = durable / "owner-host-launch.lock"
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise FirmError(f"Cannot safely open local host lock: {lock_path}") from exc
    with os.fdopen(lock_fd, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise FirmError("Local host launch lock is not a private owner file")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = _read_host_record(record)
        reused = bool(existing and _console_alive(existing, durable))
        if reused:
            pid, actual_mcp, actual_console = (
                existing["pid"],
                existing["mcp_port"],
                existing["console_port"],
            )
        else:
            if _host_lock_active(directory):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    existing = _read_host_record(record)
                    if existing and _console_alive(existing, durable):
                        reused = True
                        break
                    if not _host_lock_active(directory):
                        break
                    time.sleep(0.05)
                else:
                    raise FirmError(
                        "A Firm host owns this firm but its console did not become reachable; "
                        "inspect the existing host before starting another."
                    )
            if reused:
                pid, actual_mcp, actual_console = (
                    existing["pid"],
                    existing["mcp_port"],
                    existing["console_port"],
                )
            else:
                record.unlink(missing_ok=True)
                log_path = durable / HOST_LOG
                command = [
                    sys.executable,
                    "-m",
                    "firm_runtime.cli",
                    "start",
                    str(directory),
                    "--foreground",
                    "--no-browser",
                    "--mcp-port",
                    str(mcp_port),
                    "--console-port",
                    str(console_port),
                    "--timeout",
                    str(timeout),
                    "--sandbox-config",
                    str(Path(sandbox_config).resolve()),
                ]
                environment = os.environ.copy()
                environment[HOST_LOG_ENV] = str(log_path)
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                    env=environment,
                )
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    existing = _read_host_record(record)
                    if existing and _console_alive(existing, durable):
                        break
                    if process.poll() is not None:
                        raise FirmError(
                            "The local host exited before its console was ready; "
                            f"inspect {log_path}"
                        )
                    time.sleep(0.05)
                else:
                    raise FirmError(f"The local host did not become ready; inspect {log_path}")
                pid, actual_mcp, actual_console = (
                    existing["pid"],
                    existing["mcp_port"],
                    existing["console_port"],
                )
    url = f"http://127.0.0.1:{actual_console}/"
    key_path = durable / "console-key"
    key = key_path.read_text(encoding="ascii").strip()
    result = {
        "pid": pid,
        "mcp_port": actual_mcp,
        "console_port": actual_console,
        "url": url,
        "unlock_url": f"{url}unlock/{key}",
        "reused": reused,
    }
    if open_browser:
        webbrowser.open(result["unlock_url"])
    return result
