"""Boot disposable Linux firms and check their timing and independent identities."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from firm_sandbox import Config, Sandbox
from firm_sandbox.compatibility import initialize_firm_record, release_identifier


def boot(root: Path, index: int) -> dict:
    firm = root / f"firm-{index}"
    firm.mkdir(mode=0o700)
    definition = firm / "sandbox.toml"
    definition.write_text(
        f'[firm]\nid = "base-test-{index}"\ncpus = 2\nmemory_gib = 4\ndisk_gib = 60\n'
        '[network]\negress = "enforce"\nallow_hosts = []\n'
    )
    initialize_firm_record(firm, release_identifier())
    box = Sandbox(Config.load(definition))
    started = time.monotonic()
    result = {"firm": str(firm), "passed": False}
    try:
        state = box.up()
        result.update(seconds=time.monotonic() - started, state=state)
        if not all(state[key] for key in ("ready", "running", "network_ready")):
            raise RuntimeError("Firm did not become ready")
        result["kernel"] = box.linux._shell("uname", "-r").stdout.decode().strip()
        result["machine_id"] =box.linux._shell("cat", "/etc/machine-id").stdout.decode().strip()
        result["ssh_host_fingerprint"] = box.linux._shell(
            "sudo", "-n", "ssh-keygen", "-lf", "/etc/ssh/ssh_host_ed25519_key.pub"
        ).stdout.decode().split()[1]
        result["samba_sid"] = box.linux._shell(
            "sudo", "-n", "net", "getlocalsid"
        ).stdout.decode().split()[-1]
        result["certificate"] = box.linux._shell(
            "openssl", "x509", "-in", "/etc/ssl/certs/ssl-cert-snakeoil.pem",
            "-noout", "-fingerprint", "-sha256",
        ).stdout.decode().strip()
        result["passed"] = True
    except Exception as exc:
        result.update(seconds=time.monotonic() - started, error=str(exc))
    finally:
        try:
            box.down()
            box.discard()
            shutil.rmtree(firm)
        except Exception as exc:
            result.update(passed=False, cleanup_error=str(exc))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="New test-firm parent")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1)
    args = parser.parse_args()
    if min(args.count, args.parallel) < 1:
        parser.error("count and parallel must be positive")
    args.root = args.root.resolve()
    args.evidence = args.evidence.resolve()
    args.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        results = list(pool.map(lambda index: boot(args.root, index), range(args.count)))
    good = [result for result in results if result["passed"]]
    unique = {
        key: all(result[key] for result in good)
        and len({result[key] for result in good}) == len(good)
        for key in ("machine_id", "ssh_host_fingerprint", "samba_sid", "certificate")
    }
    report = {"passed": len(good) == args.count and all(unique.values()),
              "unique": unique, "results": results}
    (args.evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
