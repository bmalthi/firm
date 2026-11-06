"""Reap upstream usernet even when the owning packet-filter process is killed."""

import os
import select
import signal
import subprocess
import sys


def main():
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    process = subprocess.Popen(
        [os.environ["FIRM_REAL_LIMACTL"], "usernet", *sys.argv[1:]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        while process.poll() is None and not stopping:
            readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.25)
            if readable and not os.read(sys.stdin.fileno(), 1):
                break
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
