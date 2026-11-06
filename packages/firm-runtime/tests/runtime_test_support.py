"""Reusable behavior fakes and builders for runtime tests.

Keep helpers here independent of test modules so tests do not rely on pytest's
collection order or expose another test's private setup as an implicit API.
"""

import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from firm_sandbox import Config, Layout, Run, RunSpec
from firm_sandbox.file_server import FileServer, local_runner
from firm_sandbox.models import host_entries

from firm_runtime.notify import OwnerNotifier
from firm_runtime.provider_contract import ProviderUsage
from firm_runtime.runtime import Runtime
from firm_runtime.store import Store
from firm_runtime.summaries import select_context


class FakeSandbox:
    def __init__(self, root):
        self.config = Config(root=root)
        self.config.root.mkdir(parents=True, exist_ok=True)
        self.config.root.chmod(0o700)
        tree = root / "tree"
        tree.mkdir(exist_ok=True)
        self.file_server = FileServer(self.config, local_runner(tree))
        self.file_server.start()
        self.file_server.tree = tree
        self.runtime_id = uuid4().hex[:16]
        self.mac = None
        self.live = False
        self.invocations = {}
        self.specs = {}
        self.unavailable = False
        self.refuse_stop = False
        self.refuse_down = False
        self.start_error_after_create = False
        self.block_up = False
        self.up_started = threading.Event()
        self.release_up = threading.Event()
        self.block_start = False
        self.resurrect_on_release = False
        self.start_created = threading.Event()
        self.release_start = threading.Event()
        self.stops = []
        self.force_downs = 0
        self.usage = None
        self.reclaim = None
        self.ended = {}
        self.status_calls = 0
        self.inventory_calls = 0
        self.log_calls = []
        self.allowed_hosts = []

    def set_allowed_hosts(self, hosts):
        self.allowed_hosts = list(hosts)

    def layout(self, role_id):
        return Layout()

    def identity(self):
        return self.runtime_id

    def mac_acquire(self, role_id, activation_id):
        raise RuntimeError("The Mac lease is not exercised by this fixture")

    def mac_release(self, lease_id):
        return {"status": "ok", "lease_id": lease_id, "gone": True}

    def mac_status(self):
        return {"configured": False, "enrolled": False, "stations": []}

    def mac_connect(self, lease_id):
        raise RuntimeError("The Mac lease is not exercised by this fixture")

    def up(self):
        self.up_started.set()
        if self.block_up:
            self.release_up.wait(timeout=5)
        self.live = True

    def down(self, *, expected_runs=(), force=False):
        if force:
            self.force_downs += 1
        else:
            assert all(not self.status(run)["running"] for run in expected_runs)
        if not self.refuse_down:
            self.live = False
            if force:
                for state in self.invocations.values():
                    state[1], state[3] = False, 137

    def infrastructure_status(self):
        return {"running": self.live}

    def start(self, spec):
        run = Run(spec.role_id, spec.run_id, f"{len(self.invocations) + 1:064x}")
        self.invocations[spec.run_id] = [run, True, b"", None]
        self.specs[spec.run_id] = spec
        self.start_created.set()
        if self.block_start:
            self.release_start.wait(timeout=5)
            if self.resurrect_on_release:
                self.live = True
                self.invocations[spec.run_id][1] = True
        if self.start_error_after_create:
            raise OSError("guest controller disconnected after create")
        return run

    def _status(self, state):
        return {
            "running": state[1],
            "exit_code": state[3],
            "usage": self.usage,
            "log_bytes": len(state[2]),
            "reclaim": self.reclaim,
            "ended": self.ended.get(state[0].run_id, time.time()),
        }

    def status(self, run):
        if self.unavailable:
            raise OSError("engine unavailable")
        current = self.invocations[run.run_id]
        assert current[0] == run
        self.status_calls += 1
        return self._status(current)

    def inventory(self):
        if self.unavailable:
            raise OSError("engine unavailable")
        self.inventory_calls += 1
        return {
            run_id: (state[0], self._status(state))
            for run_id, state in self.invocations.items()
        }

    def stop(self, run, timeout=0):
        self.stops.append(run)
        if not self.refuse_stop:
            self.invocations[run.run_id][1] = False
            self.invocations[run.run_id][3] = 137
        return self.status(run)

    def logs(self, run, *, offset=0, until=None):
        self.log_calls.append((run.run_id, offset))
        return self.invocations[run.run_id][2][offset:until]

    def complete(self, run_id, *, success=True, session="native-session"):
        state = self.invocations[run_id]
        state[1:] = [
            False,
            json.dumps({"success": success, "session": session}).encode(),
            0 if success else 1,
        ]

    def gate_refused(self, run_id):
        self.invocations[run_id][1:] = [
            False,
            json.dumps({"error": "activation_gate"}).encode(),
            70,
        ]


class FakeNetwork:
    """The sandbox's network surface, in the shapes ``firm_sandbox.Sandbox`` returns.

    ``egress`` is the Linux proxy's tally (``entries``, ``overflow``, ``live``); the firm
    view tags each entry with its ``vm`` as the real ``network_status`` does. Hosts are
    checked with the sandbox's own validator, so a grant the sandbox would refuse fails.
    """

    def __init__(self, baseline=()):
        self.baseline = list(baseline)
        self.granted = []
        self.writes = []
        # False: succeed. True: refuse before any VM changed. "applied": raise after the new
        # list reached a VM and the sandbox's restore did not take, so it is live anyway.
        self.fail = False
        self.enabled = True
        self.mode = "enforce"
        self.egress = {"live": True, "entries": [], "overflow": 0}

    def granted_hosts(self):
        return list(self.granted)

    def set_allowed_hosts(self, hosts):
        hosts = host_entries(hosts)
        if self.fail == "applied":
            self.writes.append(list(hosts))
            self.granted = list(hosts)
            raise RuntimeError("Allowed hosts change incomplete: mac: not confirmed")
        if self.fail:
            raise RuntimeError("Allowed hosts change incomplete: linux: not confirmed")
        self.writes.append(list(hosts))
        self.granted = list(hosts)
        return {"hosts": list(hosts)}

    def set_network_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Egress control requires a boolean")
        self.enabled = enabled
        return {"linux": {"egress": enabled, "live": False}}

    def network_status(self):
        linux = {
            "enabled": self.enabled,
            "egress": self.egress,
            "traffic": {"live": self.egress["live"], "egress": self.enabled, "destinations": []},
        }
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "allowed_hosts": [
                *({"host": host, "source": "baseline"} for host in self.baseline),
                *({"host": host, "source": "granted"} for host in self.granted),
            ],
            "egress": {
                "live": self.egress["live"],
                "entries": [{**entry, "vm": "linux"} for entry in self.egress["entries"]],
                "overflow": self.egress["overflow"],
            },
            "vms": {"linux": linux},
        }


class FakeProvider:
    def __init__(self):
        self.resumed = []
        self.setup_error = None

    def build_spec(
        self,
        invocation,
        role_id,
        run_id,
        mcp_url,
        provider_session_id=None,
        *,
        layout=None,
    ):
        if self.setup_error:
            raise RuntimeError(self.setup_error)
        self.resumed.append(provider_session_id)
        return RunSpec(
            role_id,
            ("analyst",),
            run_id=run_id,
            secrets={"activation-token": invocation.token.encode()},
        )

    def parse_result(self, logs, exit_code):
        result = json.loads(logs or b"{}")
        return SimpleNamespace(
            provider_session_id=result.get("session"),
            success=exit_code == 0 and result.get("success", False),
            error=SimpleNamespace(kind=result.get("error", "process_exit")),
        )


def summary_snapshot():
    participants = [{"employee_id": "a"}]
    rows = [
        {
            "message_id": "m1",
            "sequence": 1,
            "participants_json": json.dumps(participants),
            "body": "body",
        }
    ]
    return select_context(
        rows,
        "c1",
        envelope=lambda value: {
            "message_id": value["message_id"],
            "participants": json.loads(value["participants_json"]),
            "body": value["body"],
        },
    )


def telemetry_runtime(tmp_path, *, clock=lambda: 1000.0, context_window=10_000):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=clock))
    profile = {"handoff_reserve": 1000}
    if context_window is not None:
        profile = {
            "context_window": context_window,
            "context_advisory_fraction": 0.6,
            "handoff_reserve": 1_000,
        }
    runtime.lifecycle.initialize(
        "test",
        "Build useful things.",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": profile},
        },
    )
    runtime.start()
    return runtime


def start_activation(runtime):
    invocation = runtime.admit()
    assert invocation is not None
    runtime.mark_started(invocation.activation_id)
    return invocation


def request_firm(tmp_path, clock, baseline=()):
    """A started firm with a founder running, a fake sandbox and a recording notifier."""
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=clock))
    runtime.lifecycle.initialize(
        "test",
        "Build useful things.",
        {"max_active": 2, "staffing": False, "founder_profile": "default",
         "profiles": {"default": {}}},
    )
    runtime.start()
    founder = runtime.admit()
    runtime.mark_started(founder.activation_id)
    runtime.hosts.sandbox = FakeNetwork(baseline)
    posted = []
    runtime.messaging.notifier = OwnerNotifier(
        runner=posted.append, clock=clock, platform="darwin"
    )
    runtime.posted = posted
    return runtime, founder


def hire_running(runtime, founder, slug):
    runtime.lifecycle.hire(founder.employee_id, slug, "Work", "default", request_id=f"h-{slug}")
    return start_activation(runtime)


def rows(runtime, sql, *args):
    with runtime.store.read() as db:
        return [dict(row) for row in db.execute(sql, args)]


def request_events(runtime):
    return [
        (row["kind"], json.loads(row["payload_json"]))
        for row in rows(
            runtime,
            "SELECT kind,payload_json FROM analysis_events WHERE kind LIKE 'request.%' "
            "ORDER BY sequence",
        )
    ]


def mail_to(runtime, employee_id):
    return rows(
        runtime,
        "SELECT m.author_id,m.subject,m.body,d.scheduling_ready_at FROM messages m "
        "JOIN message_deliveries d USING(message_id) WHERE d.employee_id=? ORDER BY m.sequence",
        employee_id,
    )


def provider_usage(current, *, high=None, compactions=0, native_window=12_000, inputs=100):
    return ProviderUsage(
        current_context_tokens=current,
        high_water_context_tokens=current if high is None else high,
        provider_context_window=native_window,
        input_tokens=inputs,
        cached_input_tokens=20,
        output_tokens=10,
        reasoning_output_tokens=5,
        native_compactions=compactions,
        source="test_provider",
    )
