"""Validate Docker inside a Linux role: build, Compose, bind mounts, ports, SDK and settlement.

Run with uv against an explicitly selected disposable firm. The role builds an
image, runs a root container and a uid-1000 container with bind mounts from its
home and from its drive and records the host-visible owner of what they wrote,
publishes a port and reaches it from its own loopback. A second run installs the
Docker Python SDK with uv in its private home and pings, execs in and follows the
logs of containers on its own daemon over DOCKER_HOST. A third run kills dockerd
with a container still running: settlement must still leave the slice empty and
the run resolved.

It also proves the resource readings live: a container holding 300 MiB must show
in that run's memory peak, a later small run's peak must be lower (the slice is
per run), and machine-wide samples must keep answering while the first run's
Docker install holds the guest's global lock. The kernel and cgroup facts those
readings depend on are recorded in the evidence.

Further periods check disk retention: scratch, stopped containers, tagged and dangling
images and ordinary build cache survive pause/resume alongside volumes and home files.
Explicit disposal then clears scratch and scoped Docker state, preserving tagged images
and volumes. Settlement preserves a top-level scratch symlink; the next launch replaces it
without touching its target.
"""

import argparse
import json
import threading
import time
from pathlib import Path

from firm_sandbox import Config, Organization, RunSpec, Sandbox

MIB = 1024 * 1024

SCRIPT = r"""set -eux
command -v docker
id -u; cat /proc/self/cgroup
test -n "$DOCKER_HOST"
docker info --format '{{.Name}}'
sleep 12
docker info --format '{{.Name}}'
printf '\nDOCKER_IDLE_SURVIVAL_PASS\n'
work=$(pwd); mkdir -p "$work/probe" "$HOME/probe"
chmod 777 "$HOME/probe"
cd "$work/probe"
cp "$FIRM_CREDENTIALS_DIR/dockerfile" Dockerfile
cp "$FIRM_CREDENTIALS_DIR/compose" compose.yaml
printf before > index.txt
DOCKER_BUILDKIT=1 docker build -t firm-role-probe:latest .
docker compose -f compose.yaml run --name firm-compose-output probe
docker cp firm-compose-output:/output.txt compose-output.txt
test "$(cat compose-output.txt)" = compose-output
docker rm firm-compose-output
for source in "$HOME/probe" "$work/probe"; do
    docker run --rm -v "$source:/out" alpine:3.20 sh -ec 'touch /out/root-wrote; id -u'
    docker run --rm --user 1000 -v "$source:/out" alpine:3.20 \
        sh -ec 'touch /out/uid1000-wrote; id -u'
    stat -c 'OWNER %u %g %n' "$source/root-wrote" "$source/uid1000-wrote"
done
docker compose -f compose.yaml up -d web
container=$(docker compose -f compose.yaml ps -q web)
test -n "$container"
for n in $(seq 1 60); do
    value=$(curl -fsS --max-time 2 http://127.0.0.1:18088/index.txt || true)
    [ "$value" = before ] && break
    sleep 0.25
done
test "$value" = before
docker compose -f compose.yaml down
printf '\nDOCKER_BUILD_COMPOSE_MOUNTS_PORT_PASS\n'
"""

SDK = r"""set -eu
project="$HOME/.firm-docker-sdk-project"
mkdir -p "$project"
cd "$project"
if [ ! -f pyproject.toml ]; then
    uv init --bare --name firm-docker-sdk-probe --python 3.12
fi
uv python pin 3.12
uv add 'docker>=7,<8'
exec uv run python - <<'PY'
import docker

client = docker.from_env(timeout=240)
assert client.ping()
target = client.containers.run("alpine:3.20", ["sleep", "300"], detach=True)
try:
    exit_code, output = target.exec_run(["echo", "sdk-exec-pass"])
    if exit_code != 0 or b"sdk-exec-pass" not in output:
        raise RuntimeError(f"Docker SDK exec failed: {exit_code} {output!r}")
finally:
    target.remove(force=True)
stream = client.containers.run("alpine:3.20", ["echo", "sdk-stream-pass"], detach=True)
try:
    stream.wait(timeout=30)
    logs = b"".join(stream.logs(stream=True, follow=True))
finally:
    stream.remove(force=True)
if b"sdk-stream-pass" not in logs:
    raise RuntimeError(f"Docker SDK log stream failed: {logs!r}")
print("DOCKER_SDK_PING_EXEC_STREAM_PASS", flush=True)
PY
"""

HOLD_MEMORY = r"""set -eux
docker run --rm --shm-size=512m alpine:3.20 \
    sh -ec 'dd if=/dev/zero of=/dev/shm/block bs=1M count=300; sleep 5'
printf '\nDOCKER_HELD_300_MIB\n'
"""

SMALL = "set -eux\ndocker run --rm alpine:3.20 echo small\n"

RETENTION_SEED = r"""set -eux
docker rm -f firm-reclaim-stopped firm-reclaim-restart 2>/dev/null || true
docker version --format '{{.Server.Version}}'
docker info --format '{{.Driver}} {{.DriverStatus}}'
docker image inspect firm-role-probe:latest --format '{{.Id}}' > "$HOME/probe/image-id"
docker volume create firm-reclaim-volume
docker run --rm -v firm-reclaim-volume:/data alpine:3.20 sh -ec 'echo kept >/data/value'
docker run --name firm-reclaim-stopped alpine:3.20 echo retained
docker run -d --restart=always --name firm-reclaim-restart alpine:3.20 sleep 600
mkdir -p "$PROBE_SCRATCH/checkout/.git"
printf 'ref: refs/heads/main\n' > "$PROBE_SCRATCH/checkout/.git/HEAD"
printf 'FROM alpine:3.20\nCOPY payload /payload\n' > "$PROBE_SCRATCH/checkout/Dockerfile"
printf first > "$PROBE_SCRATCH/checkout/payload"
docker build -t firm-retention-build:latest "$PROBE_SCRATCH/checkout"
docker build --iidfile "$HOME/probe/dangling-id" "$PROBE_SCRATCH/checkout"
docker image ls -a --no-trunc
printf second > "$PROBE_SCRATCH/checkout/payload"
docker build -t firm-retention-build:latest "$PROBE_SCRATCH/checkout"
docker image ls -a --no-trunc
mkdir -p "$PROBE_SCRATCH/build" "$HOME/.cache/uv"
chmod 777 "$PROBE_SCRATCH/build"
docker run --rm --user 1000 -v "$PROBE_SCRATCH/build:/out" alpine:3.20 touch /out/subuid
echo kept > "$HOME/.cache/uv/firm-retain"
echo kept > "$HOME/work/firm-retain"
dd if=/dev/zero of="$PROBE_SCRATCH/output" bs=1M count=8
"""

RETENTION_VERIFY = r"""set -eux
test -f "$PROBE_SCRATCH/build/subuid"
test "$(stat -c %s "$PROBE_SCRATCH/output")" = 8388608
grep 'ref: refs/heads/main' "$PROBE_SCRATCH/checkout/.git/HEAD"
test "$(cat "$PROBE_SCRATCH/checkout/payload")" = second
docker ps -a --format '{{.Names}}' | grep -x firm-reclaim-stopped
dangling=$(cat "$HOME/probe/dangling-id")
docker image ls -a --no-trunc
docker image inspect "$dangling"
docker image ls --no-trunc --quiet --filter dangling=true
docker image ls --no-trunc --quiet --filter dangling=true | grep -Fx "$dangling"
docker system df --format '{{.Type}} {{.TotalCount}}' | grep -E '^Build Cache [1-9][0-9]*$'
image=$(docker image inspect firm-role-probe:latest --format '{{.Id}}')
test "$(cat "$HOME/probe/image-id")" = "$image"
docker run --rm --pull=never firm-role-probe:latest cat /built.txt
docker run --rm --pull=never -v firm-reclaim-volume:/data alpine:3.20 grep kept /data/value
grep kept "$HOME/.cache/uv/firm-retain" "$HOME/work/firm-retain"
printf '\nDOCKER_PAUSE_RESUME_RETENTION_PASS\n'
"""

DISPOSE_VERIFY = r"""set -eux
test -d "$PROBE_SCRATCH"
test -z "$(ls -A "$PROBE_SCRATCH")"
image=$(docker image inspect firm-role-probe:latest --format '{{.Id}}')
test "$(cat "$HOME/probe/image-id")" = "$image"
docker run --rm --pull=never firm-role-probe:latest cat /built.txt
docker run --rm --pull=never -v firm-reclaim-volume:/data alpine:3.20 grep kept /data/value
test -z "$(docker ps -a --quiet --filter status=exited)"
if docker container inspect firm-reclaim-restart; then exit 1; fi
test -z "$(docker image ls --quiet --filter dangling=true)"
if docker image inspect "$(cat "$HOME/probe/dangling-id")"; then exit 1; fi
# The host checks positive build/prune reclaim; cache shared with kept images may remain.
docker system df
docker image inspect firm-retention-build:latest
grep kept "$HOME/.cache/uv/firm-retain" "$HOME/work/firm-retain"
printf '\nDOCKER_DISPOSAL_RETENTION_PASS\n'
"""

SCRATCH_SYMLINK = r"""set -eux
target="$HOME/work/firm-scratch-symlink-target"
mkdir -p "$HOME/work" "$target"
chmod 700 "$target"
printf retained > "$target/sentinel"
rmdir "$PROBE_SCRATCH"
ln -s "$target" "$PROBE_SCRATCH"
test -L "$PROBE_SCRATCH"
printf '\nDOCKER_SCRATCH_SYMLINK_SEEDED\n'
"""

SCRATCH_SYMLINK_VERIFY = r"""set -eux
test -d "$PROBE_SCRATCH"
test ! -L "$PROBE_SCRATCH"
test -z "$(ls -A "$PROBE_SCRATCH")"
test "$(cat "$HOME/work/firm-scratch-symlink-target/sentinel")" = retained
printf '\nDOCKER_SCRATCH_SYMLINK_LAUNCH_REPAIR_PASS\n'
"""

KILL_DAEMON = r"""set -eux
docker run -d --name firm-orphan alpine:3.20 sleep 600
pkill -u "$(id -u)" -x dockerd
sleep 2
printf '\nDOCKER_DAEMON_KILLED_WITH_CONTAINER_RUNNING\n'
"""

DOCKERFILE = (
    "FROM alpine:3.20\nRUN apk add --no-cache busybox-extras\n"
    "RUN --mount=type=cache,target=/cache printf built-inside-role >/built.txt\n"
    "COPY index.txt /work/index.txt\n"
)
COMPOSE = """services:
  probe:
    image: firm-role-probe:latest
    command:
      - sh
      - -ec
      - cat /built.txt; umask 077; printf compose-output >/output.txt
  web:
    image: firm-role-probe:latest
    ports: ["127.0.0.1:18088:8080"]
    command: [busybox-extras, httpd, -f, -p, "8080", -h, /work]
"""


GUEST_FACTS = (
    "uname -r; cat /sys/fs/cgroup/user.slice/cgroup.subtree_control; "
    "cat /proc/pressure/memory; systemctl is-active systemd-oomd"
)


def guest_facts(box):
    """The kernel and cgroup facts the resource readings depend on."""
    result = box.linux._shell("sudo", "-n", "/bin/sh", "-c", GUEST_FACTS, check=False, timeout=60)
    return result.stdout.decode(errors="replace").splitlines()


class Sampler(threading.Thread):
    """Machine-wide samples every few seconds, including while the guest lock is held."""

    def __init__(self, box):
        super().__init__(daemon=True)
        self.box = box
        self.samples = []
        self.done = threading.Event()

    def run(self):
        while not self.done.is_set():
            started = time.monotonic()
            try:
                observed = self.box.resources()
                entry = {"linux": observed["linux"], "host": observed["host"]}
            except Exception as exc:
                entry = {"error": f"{type(exc).__name__}: {exc}"}
            self.samples.append({"seconds": round(time.monotonic() - started, 3), **entry})
            self.done.wait(5)

    def stop(self):
        self.done.set()
        self.join(timeout=60)
        return {
            "samples": len(self.samples),
            "slowest_seconds": max((s["seconds"] for s in self.samples), default=None),
            "failures": [s for s in self.samples if "error" in s],
            "boots": sorted({s["linux"]["boot"] for s in self.samples if s.get("linux")}),
            "last": self.samples[-1] if self.samples else None,
        }


def slice_state(box, role):
    role_slice = box.linux.info()["guest"]["roles"][role]["slice"]
    return box.linux._shell(
        "sudo", "-n", "systemctl", "is-active", role_slice, check=False, timeout=30
    ).stdout.decode().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--role", default="docker-probe")
    args = parser.parse_args()
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {"role": args.role, "passed": False}
    sampler = None
    try:
        box.up()
        record["guest_facts"] = guest_facts(box)
        # The first run installs Docker under the guest's global lock; a lock-free
        # sample must keep answering throughout.
        sampler = Sampler(box)
        sampler.start()
        organization = Organization(args.role)
        box.file_server.provision(args.role, organization)
        run = box.start(RunSpec(
            args.role, ("/bin/bash", "-lc", SCRIPT), organization=organization,
            secrets={"dockerfile": DOCKERFILE.encode(), "compose": COMPOSE.encode()},
        ))
        result = box.wait(run, timeout=900)
        output = box.logs(run).decode(errors="replace")
        (args.evidence / "role.log").write_text(output)
        owners = [line for line in output.splitlines() if line.startswith("OWNER ")]
        record.update({
            "run_id": run.run_id,
            "result": result,
            "bind_mount_owners": owners,
        })
        if not (result.get("exit_code") == 0 and not result.get("unresolved")
                and "DOCKER_BUILD_COMPOSE_MOUNTS_PORT_PASS" in output):
            raise RuntimeError("Docker workload failed; inspect recorded role output")
        sdk = box.start(RunSpec(args.role, ("/bin/bash", "-lc", SDK), organization=organization))
        sdk_result = box.wait(sdk, timeout=900)
        sdk_output = box.logs(sdk).decode(errors="replace")
        (args.evidence / "sdk.log").write_text(sdk_output)
        record["sdk"] = {
            "run_id": sdk.run_id,
            "result": sdk_result,
            "passed": sdk_result.get("exit_code") == 0 and not sdk_result.get("unresolved")
            and "DOCKER_SDK_PING_EXEC_STREAM_PASS" in sdk_output,
        }
        if not record["sdk"]["passed"]:
            raise RuntimeError("Docker SDK workload failed; inspect sdk.log")
        record["retention"] = {}

        def period(name, script):
            probe = box.start(RunSpec(
                args.role, ("/bin/bash", "-lc", script), organization=organization,
                env={"PROBE_SCRATCH": box.layout(args.role).scratch},
            ))
            result = box.wait(probe, timeout=300)
            (args.evidence / f"retention-{name}.log").write_bytes(box.logs(probe))
            record["retention"][name] = result
            assert result["exit_code"] == 0 and not result.get("unresolved"), result
            assert "reclaim" not in result
            return result

        def dispose(name):
            result = box.dispose(args.role)
            record["retention"][name] = result
            categories = result["reclaim"]["categories"]
            assert set(categories) == {
                "scratch", "docker_containers", "docker_image_deletion_records",
                "docker_build_cache",
            }, categories
            assert all(c["completed"] and not c["skipped"] for c in categories.values()), categories
            return categories

        period("seed", RETENTION_SEED)
        period("verify", RETENTION_VERIFY)
        categories = dispose("dispose")
        assert categories["scratch"]["bytes"] >= 8 * MIB
        assert all(categories[name]["count"] > 0 for name in (
            "docker_containers", "docker_image_deletion_records", "docker_build_cache"))
        period("disposed-verify", DISPOSE_VERIFY)
        period("scratch-symlink", SCRATCH_SYMLINK)
        # Settlement preserves the link; the next launch heals scratch without
        # touching the target.
        scratch = box.layout(args.role).scratch
        preserved = box.linux._shell(
            "sudo", "-n", "/bin/sh", "-ec",
            'test -L "$1"; test "$(cat "$1/sentinel")" = retained',
            "scratch-check", scratch, timeout=30,
        )
        assert preserved.returncode == 0
        period("scratch-symlink-verify", SCRATCH_SYMLINK_VERIFY)
        held = box.start(RunSpec(
            args.role, ("/bin/bash", "-lc", HOLD_MEMORY), organization=organization,
        ))
        held_result = box.wait(held, timeout=600)
        (args.evidence / "held-memory.log").write_bytes(box.logs(held))
        small = box.start(RunSpec(
            args.role, ("/bin/bash", "-lc", SMALL), organization=organization,
        ))
        small_result = box.wait(small, timeout=600)
        record["usage"] = {"held_300_mib": held_result.get("usage"),
                           "later_small_run": small_result.get("usage")}
        held_usage, small_usage = held_result.get("usage"), small_result.get("usage")
        assert held_usage and small_usage, record["usage"]
        assert held_usage["cpu_usec"] > 0, record["usage"]
        assert held_usage["memory_peak_bytes"] > 300 * MIB, record["usage"]
        # The slice is created and removed per run, so the next peak starts again.
        assert small_usage["memory_peak_bytes"] < held_usage["memory_peak_bytes"], record["usage"]
        record["sampling"] = sampler.stop()
        sampler = None
        assert not record["sampling"]["failures"], record["sampling"]
        assert len(record["sampling"]["boots"]) == 1, record["sampling"]
        assert record["sampling"]["slowest_seconds"] < 30, record["sampling"]
        killed = box.start(RunSpec(
            args.role, ("/bin/bash", "-lc", KILL_DAEMON), organization=organization,
        ))
        settled = box.wait(killed, timeout=300)
        (args.evidence / "daemon-killed.log").write_bytes(box.logs(killed))
        record["daemon_killed"] = {
            "result": settled,
            "slice_after": slice_state(box, args.role),
            "settled_clean": not settled.get("unresolved") and settled.get("exit_code") == 0,
        }
        assert record["daemon_killed"]["slice_after"] == "inactive", record["daemon_killed"]
        assert record["daemon_killed"]["settled_clean"], settled
    finally:
        if sampler is not None:
            record["sampling"] = sampler.stop()
        (args.evidence / "result.json").write_text(json.dumps(record, indent=2))
        box.down()
    if box.state() != "stopped":
        raise RuntimeError("Docker acceptance cleanup left a VM running")
    record["vms_stopped"] = True
    record["passed"] = True
    (args.evidence / "result.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2), flush=True)


if __name__ == "__main__":
    main()
