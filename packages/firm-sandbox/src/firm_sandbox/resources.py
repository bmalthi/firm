"""What the firm costs the host Mac: its own counters and each VM process's share.

A guest's own sample cannot answer whether another VM fits. Lima's VZ driver never
balloons memory back, so a VM eventually holds its full ``memory_gib`` on the host;
only the host's memory signals and each virtual-machine process's footprint show the
real fit. The host is a Mac, so its counters are the Mac guest's, read locally.
"""

from __future__ import annotations

import ctypes
import subprocess

from .mac_vm_guest import sample as mac_sample

LSOF = "/usr/sbin/lsof"
RUSAGE_INFO_V2 = 2
DISK_NAMES = ("disk", "diffdisk")
UNKNOWN_PROCESS = {"host_footprint_bytes": None, "host_cpu_usec": None}


class RUsageInfoV2(ctypes.Structure):
    """``rusage_info_v2`` of ``<libproc.h>``; only footprint and CPU time are read."""

    _fields_ = [
        ("uuid", ctypes.c_uint8 * 16),
        ("user_time", ctypes.c_uint64),
        ("system_time", ctypes.c_uint64),
        ("pkg_idle_wkups", ctypes.c_uint64),
        ("interrupt_wkups", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("wired_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("phys_footprint", ctypes.c_uint64),
        ("proc_start_abstime", ctypes.c_uint64),
        ("proc_exit_abstime", ctypes.c_uint64),
        ("child_user_time", ctypes.c_uint64),
        ("child_system_time", ctypes.c_uint64),
        ("child_pkg_idle_wkups", ctypes.c_uint64),
        ("child_interrupt_wkups", ctypes.c_uint64),
        ("child_pageins", ctypes.c_uint64),
        ("child_elapsed_abstime", ctypes.c_uint64),
        ("diskio_bytesread", ctypes.c_uint64),
        ("diskio_byteswritten", ctypes.c_uint64),
    ]


class Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


def host_sample():
    """The host Mac's own memory, pressure and disk counters."""
    return mac_sample()


def cpu_usec(user_time, system_time, timebase):
    """Mach time units to microseconds; the timebase is the identity ratio on Intel."""
    numer, denom = timebase
    return (user_time + system_time) * numer // denom // 1000


def process_usage(pid):
    """One process's physical footprint and CPU time, without privileges."""
    library = ctypes.CDLL(None, use_errno=True)
    usage = RUsageInfoV2()
    if library.proc_pid_rusage(
        ctypes.c_int(pid), ctypes.c_int(RUSAGE_INFO_V2), ctypes.byref(usage)
    ):
        return UNKNOWN_PROCESS
    timebase = Timebase()
    if library.mach_timebase_info(ctypes.byref(timebase)) or not timebase.denom:
        return UNKNOWN_PROCESS
    return {
        "host_footprint_bytes": usage.phys_footprint,
        "host_cpu_usec": cpu_usec(
            usage.user_time, usage.system_time, (timebase.numer, timebase.denom)
        ),
    }


def vm_process(vm):
    """The host cost of one VM, found by the instance disk its process holds open.

    Without exactly one holder the VM's host numbers stay unknown rather than become
    another VM's.
    """
    pids = set()
    for directory in {vm.paths.instance, vm.paths.durable_instance}:
        for name in DISK_NAMES:
            path = directory / name
            if not path.exists():
                continue
            found = subprocess.run(
                [LSOF, "-t", str(path)], capture_output=True, text=True, check=False, timeout=10
            )
            pids.update(int(pid) for pid in found.stdout.split() if pid.isdigit())
    if len(pids) != 1:
        return UNKNOWN_PROCESS
    return process_usage(pids.pop())
