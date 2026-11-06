"""Host-side resource readings: the host's own counters and each VM process's share."""

import os
from types import SimpleNamespace

from firm_sandbox import resources


def test_host_counters_are_the_mac_guest_readings(monkeypatch):
    monkeypatch.setattr(resources, "mac_sample", lambda: {"boot": "host", "cpus": 10})
    assert resources.host_sample() == {"boot": "host", "cpus": 10}


def test_cpu_time_converts_mach_units_with_the_machine_timebase():
    # Apple Silicon counts a 24 MHz timer; Intel reports nanoseconds already.
    assert resources.cpu_usec(24_000_000, 0, (125, 3)) == 1_000_000
    assert resources.cpu_usec(1_000_000_000, 500_000_000, (1, 1)) == 1_500_000


def instance(tmp_path, name):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "disk").write_text("vm")
    return SimpleNamespace(paths=SimpleNamespace(instance=directory, durable_instance=directory))


def test_a_vm_is_attributed_to_the_one_process_holding_its_disk(tmp_path, monkeypatch):
    vm = instance(tmp_path, "linux")
    seen = []

    def lsof(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="4242\n")

    monkeypatch.setattr(resources.subprocess, "run", lsof)
    monkeypatch.setattr(
        resources,
        "process_usage",
        lambda pid: {"host_footprint_bytes": 100 * pid, "host_cpu_usec": pid},
    )
    assert resources.vm_process(vm) == {"host_footprint_bytes": 424200, "host_cpu_usec": 4242}
    assert seen == [[resources.LSOF, "-t", str(vm.paths.instance / "disk")]]


def test_an_ambiguous_or_absent_holder_leaves_the_host_numbers_unknown(tmp_path, monkeypatch):
    vm = instance(tmp_path, "mac")
    monkeypatch.setattr(
        resources, "process_usage", lambda pid: {"host_footprint_bytes": 1, "host_cpu_usec": 1}
    )
    for output in ("", "17\n23\n"):
        monkeypatch.setattr(
            resources.subprocess,
            "run",
            lambda argv, output=output, **kwargs: SimpleNamespace(returncode=0, stdout=output),
        )
        assert resources.vm_process(vm) == resources.UNKNOWN_PROCESS


def test_the_running_process_reports_its_own_footprint_and_cpu_time():
    usage = resources.process_usage(os.getpid())
    assert usage["host_footprint_bytes"] > 0
    assert usage["host_cpu_usec"] > 0
