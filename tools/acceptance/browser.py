"""Validate a web backend inspected by a headless browser inside a Linux role.

Run with uv against an explicitly selected disposable firm. The default native
mode installs Playwright and Chromium in an ordinary role's home, without sudo or
Docker, and exercises the base image's browser libraries with trusted local HTML.
It uses Playwright's default disabled Chromium sandbox; --chromium-sandbox tests
the stricter launch, which stock Ubuntu AppArmor user-namespace policy may deny.
Optional --mode docker
retains the Compose backend and official Playwright image check. The
page's JavaScript fetches data from the backend, sets the title and an element's
text and logs a console message; the browser waits for the title, reads the text,
collects the console and produces a full-page screenshot and PDF. The host checks
the values, PNG dimensions, PDF framing, the run's settlement with an
empty slice and that the VMs stop. No [mac] is needed.
"""

import argparse
import base64
import json
import struct
import time
from pathlib import Path

from firm_sandbox import Config, Organization, RunSpec, Sandbox

# Official Playwright Python image and its matching PyPI release
# (mcr.microsoft.com/v2/playwright/python/tags/list, pypi.org/project/playwright).
PLAYWRIGHT_VERSION = "1.63.0"
PLAYWRIGHT_IMAGE = f"mcr.microsoft.com/playwright/python:v{PLAYWRIGHT_VERSION}-noble"
WEB_IMAGE = "python:3.13-slim"

STATIC_TITLE = "firm static title"
STATIC_TEXT = "firm static text"
EXPECTED_TITLE = "Firm browser probe: rendered"
EXPECTED_TEXT = "Backend data rendered by JavaScript"
EXPECTED_CONSOLE = "FIRM_BROWSER_CONSOLE_PROBE backend-data-loaded"
VIEWPORT = (1024, 700)
TALL = 1800

PAGE = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{STATIC_TITLE}</title>
<style>
body {{ margin: 0; font-family: sans-serif; }}
header {{ padding: 24px; background: #1d4ed8; color: #fff; }}
.tall {{ height: {TALL}px; background: linear-gradient(#fff, #94a3b8); }}
</style>
</head>
<body>
<header><h1 id="status">{STATIC_TEXT}</h1></header>
<div class="tall"></div>
<script>
fetch("/data.json")
  .then((response) => response.json())
  .then((data) => {{
    document.getElementById("status").textContent = data.text;
    console.log(data.console);
    document.title = data.title;
  }});
</script>
</body>
</html>
"""

DATA = json.dumps({"title": EXPECTED_TITLE, "text": EXPECTED_TEXT, "console": EXPECTED_CONSOLE})

PROBE = f"""import functools
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

URL = "http://web:8000/"
native = os.environ.get("FIRM_BROWSER_NATIVE") == "1"
chromium_sandbox = "--chromium-sandbox" in sys.argv
out = Path("out" if native else "/out")
result = {{"native": native, "uid": os.getuid(), "chromium_sandbox": chromium_sandbox}}
server = None
try:
    if native:
        result["sudo_denied"] = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=10).returncode != 0
        result["outside_container"] = not Path("/.dockerenv").exists()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
            functools.partial(http.server.SimpleHTTPRequestHandler, directory="site"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        URL = f"http://127.0.0.1:{{server.server_port}}/"
    result["url"] = URL
    deadline = time.monotonic() + 120
    while True:
        try:
            with urllib.request.urlopen(URL, timeout=2) as response:
                served = response.read().decode()
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
    result["served_static_title"] = {STATIC_TITLE!r} in served
    from playwright.sync_api import sync_playwright

    console, errors = [], []
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(chromium_sandbox=chromium_sandbox)
        result["browser_version"] = browser.version
        page = browser.new_page(viewport={{"width": {VIEWPORT[0]}, "height": {VIEWPORT[1]}}})
        page.on("console", lambda message: console.append(
            {{"type": message.type, "text": message.text}}))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(URL, wait_until="load")
        page.wait_for_function(
            "static => document.title && document.title !== static",
            arg={STATIC_TITLE!r}, timeout=30000,
        )
        result["title"] = page.title()
        result["text"] = page.text_content("#status")
        page.screenshot(path=str(out / "page.png"), full_page=True)
        page.pdf(path=str(out / "page.pdf"), format="A4", print_background=True)
        result["user_agent"] = page.evaluate("navigator.userAgent")
        browser.close()
    result.update(console=console, page_errors=errors,
                  browser_seconds=round(time.monotonic() - started, 3), ok=True)
except Exception:
    result.update(ok=False, error=traceback.format_exc())
finally:
    if server:
        server.shutdown()
        server.server_close()
with (out / "result.json").open("w") as handle:
    json.dump(result, handle)
print("PROBE_DONE", result.get("ok"), flush=True)
raise SystemExit(0 if result["ok"] else 1)
"""

COMPOSE = f"""name: firm-browser-probe
services:
  web:
    image: {WEB_IMAGE}
    command: [python, -m, http.server, "8000", --bind, 0.0.0.0, --directory, /site]
    volumes: ["./site:/site:ro"]
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/')"]
      interval: 1s
      timeout: 3s
      retries: 60
  browser:
    image: {PLAYWRIGHT_IMAGE}
    depends_on:
      web:
        condition: service_healthy
    shm_size: 1gb
    environment:
      # The backend is on the Compose network, not an external proxy destination.
      NO_PROXY: web,localhost,127.0.0.1
      no_proxy: web,localhost,127.0.0.1
    volumes: ["./probe:/probe:ro", "./out:/out"]
    command:
      - sh
      - -ec
      - >-
        pip install --no-cache-dir --root-user-action=ignore playwright=={PLAYWRIGHT_VERSION}
        && python /probe/probe.py
"""

ARTIFACTS = r"""set +x
if [ -f out/result.json ]; then
    printf '\nBROWSER_RESULT_JSON %s\n' "$(tr -d '\n' < out/result.json)"
fi
for artifact in SCREENSHOT PDF; do
    case "$artifact" in SCREENSHOT) file=page.png ;; PDF) file=page.pdf ;; esac
    if [ -f "out/$file" ]; then
        printf '\nBROWSER_%s_BEGIN\n' "$artifact"
        base64 -w 76 "out/$file"
        printf 'BROWSER_%s_END\n' "$artifact"
    fi
done
set -x
"""

NATIVE_SCRIPT = r"""set -eux
test "$(id -u)" -ne 0
test ! -e /.dockerenv
if sudo -n true; then echo 'Role unexpectedly has sudo'; exit 1; fi
project="$HOME/browser-probe-native"
mkdir -p "$project/site" "$project/probe" "$project/out"
cd "$project"
cp "$FIRM_CREDENTIALS_DIR/page" site/index.html
cp "$FIRM_CREDENTIALS_DIR/data" site/data.json
cp "$FIRM_CREDENTIALS_DIR/probe" probe/probe.py
if [ ! -f pyproject.toml ]; then
    uv init --bare --name firm-browser-probe --no-workspace --no-managed-python
fi
uv add 'playwright==PLAYWRIGHT_VERSION'
uv run playwright install chromium
set +e
FIRM_BROWSER_NATIVE=1 uv run probe/probe.py "$@"
code=$?
set -e
""".replace("PLAYWRIGHT_VERSION", PLAYWRIGHT_VERSION) + ARTIFACTS + 'exit "$code"\n'

DOCKER_SCRIPT = r"""set -eux
command -v docker
id -u
test -n "$DOCKER_HOST"
project="$HOME/browser-probe"
rm -rf "$project"
mkdir -p "$project/site" "$project/probe" "$project/out"
cd "$project"
cp "$FIRM_CREDENTIALS_DIR/compose" compose.yaml
cp "$FIRM_CREDENTIALS_DIR/page" site/index.html
cp "$FIRM_CREDENTIALS_DIR/data" site/data.json
cp "$FIRM_CREDENTIALS_DIR/probe" probe/probe.py
# Use full nanoseconds: date ignores %3N, and systemd expands braced shell variables.
now() { local t; t=$(date +%s%N); echo $(( t / 1000000 )); }
start=$(now)
timeout 1800 docker compose pull
printf '\nPULL_MS %s\n' "$(( $(now) - start ))"
for image in $(docker compose config --images); do
    printf 'IMAGE_DIGEST %s %s\n' "$image" \
        "$(docker image inspect --format '{{join .RepoDigests ","}}' "$image")"
done
start=$(now)
set +e
timeout 900 docker compose up --abort-on-container-exit --exit-code-from browser
code=$?
set -e
printf '\nUP_MS %s\nCOMPOSE_EXIT %s\n' "$(( $(now) - start ))" "$code"
""" + ARTIFACTS + r"""
docker compose down --volumes --remove-orphans
test -z "$(docker compose ps -aq)"
printf '\nCOMPOSE_DOWN_CLEAN\n'
exit "$code"
"""


def slice_state(box, role):
    role_slice = box.linux.info()["guest"]["roles"][role]["slice"]
    return box.linux._shell(
        "sudo", "-n", "systemctl", "is-active", role_slice, check=False, timeout=30
    ).stdout.decode().strip()


def marker(lines, name):
    """The value after the last ``NAME `` marker line, or None."""
    values = [line[len(name) + 1:] for line in lines if line.startswith(name + " ")]
    return values[-1] if values else None


def artifact(lines, name):
    try:
        begin = lines.index(f"BROWSER_{name}_BEGIN")
        end = lines.index(f"BROWSER_{name}_END", begin)
    except ValueError:
        return None
    return base64.b64decode("".join(line.strip() for line in lines[begin + 1:end]))


def png_facts(data):
    """Signature, IHDR dimensions and IEND of a PNG, or the reason it is not one."""
    if data is None:
        return {"valid": False, "reason": "no screenshot in role output"}
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return {"valid": False, "reason": "missing PNG signature or IHDR", "bytes": len(data)}
    width, height = struct.unpack(">II", data[16:24])
    return {
        "valid": data.endswith(b"IEND\xaeB`\x82"),
        "bytes": len(data),
        "width": width,
        "height": height,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Firm containing sandbox.toml")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--role", default="browser-probe")
    parser.add_argument("--mode", choices=("native", "docker"), default="native")
    parser.add_argument("--chromium-sandbox", action="store_true",
                        help="Require Chromium's sandbox (native only; stock AppArmor may deny it)")
    args = parser.parse_args()
    if args.chromium_sandbox and args.mode != "native":
        parser.error("--chromium-sandbox requires --mode native")
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    box = Sandbox(Config.load(args.root / "sandbox.toml"))
    record = {
        "role": args.role,
        "mode": args.mode,
        "chromium_sandbox": args.chromium_sandbox,
        "mac": "not used" if not box.mac else "configured but not used",
        "images": {"browser": PLAYWRIGHT_IMAGE, "web": WEB_IMAGE} if args.mode == "docker" else {},
        "playwright_package": PLAYWRIGHT_VERSION,
        "passed": False,
    }
    try:
        started = time.monotonic()
        box.up()
        record["up_seconds"] = round(time.monotonic() - started, 1)
        organization = Organization(args.role)
        box.file_server.provision(args.role, organization)
        started = time.monotonic()
        script = NATIVE_SCRIPT if args.mode == "native" else DOCKER_SCRIPT
        command = ("/bin/bash", "-lc", script, "browser-probe")
        if args.chromium_sandbox:
            command += ("--chromium-sandbox",)
        run = box.start(RunSpec(
            args.role, command,
            organization=organization,
            secrets={
                "compose": COMPOSE.encode(), "page": PAGE.encode(),
                "data": DATA.encode(), "probe": PROBE.encode(),
            },
        ))
        result = box.wait(run, timeout=3000)
        record["run_seconds"] = round(time.monotonic() - started, 1)
        output = box.logs(run).decode(errors="replace")
        (args.evidence / "role.log").write_text(output)
        lines = output.splitlines()
        image = artifact(lines, "SCREENSHOT")
        if image is not None:
            (args.evidence / "page.png").write_bytes(image)
        pdf = artifact(lines, "PDF")
        if pdf is not None:
            (args.evidence / "page.pdf").write_bytes(pdf)
        raw = marker(lines, "BROWSER_RESULT_JSON")
        browser = json.loads(raw) if raw else {}
        (args.evidence / "browser-result.json").write_text(json.dumps(browser, indent=2))
        compose_exit = marker(lines, "COMPOSE_EXIT")
        durations = {name: marker(lines, name) for name in ("PULL_MS", "UP_MS")}
        record.update({
            "run_id": run.run_id,
            "result": result,
            "slice_after": slice_state(box, args.role),
            "compose_exit": None if compose_exit is None else int(compose_exit),
            "compose_down_clean": "COMPOSE_DOWN_CLEAN" in lines,
            "pull_ms": None if durations["PULL_MS"] is None else int(durations["PULL_MS"]),
            "compose_up_ms": None if durations["UP_MS"] is None else int(durations["UP_MS"]),
            "image_digests": [
                line.split(" ", 2)[1:] for line in lines if line.startswith("IMAGE_DIGEST ")
            ],
            "browser": {key: value for key, value in browser.items() if key != "error"},
            "browser_error": browser.get("error"),
            "screenshot": png_facts(image),
            "pdf_bytes": len(pdf) if pdf else 0,
        })
        png = record["screenshot"]
        checks = {
            "settled_clean": result.get("exit_code") == 0 and not result.get("unresolved"),
            "slice_inactive": record["slice_after"] == "inactive",
            "browser_succeeded": browser.get("ok") is True,
            "backend_served_static_page": browser.get("served_static_title") is True,
            "title_set_by_javascript": browser.get("title") == EXPECTED_TITLE,
            "text_set_by_javascript": browser.get("text") == EXPECTED_TEXT,
            "console_captured": any(
                entry.get("text") == EXPECTED_CONSOLE for entry in browser.get("console", [])
            ),
            "no_page_errors": browser.get("page_errors") == [],
            "screenshot_png": png["valid"],
            "screenshot_width": png.get("width") == VIEWPORT[0],
            "screenshot_full_page": (png.get("height") or 0) >= TALL,
            "screenshot_size": 2_000 <= (png.get("bytes") or 0) <= 20_000_000,
            "pdf_document": bool(pdf and pdf.startswith(b"%PDF-")
                                 and pdf.rstrip().endswith(b"%%EOF") and len(pdf) > 1000),
        }
        if args.mode == "native":
            checks.update(
                native_browser=browser.get("native") is True,
                ordinary_role=2000 <= browser.get("uid", 0) <= 49999,
                sudo_denied=browser.get("sudo_denied") is True,
                outside_container=browser.get("outside_container") is True,
                chromium_sandbox_setting=browser.get("chromium_sandbox") is args.chromium_sandbox,
            )
        else:
            checks.update(compose_exit_zero=record["compose_exit"] == 0,
                          compose_down_clean=record["compose_down_clean"])
        record["checks"] = checks
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise RuntimeError(f"Browser workload failed {failed}; inspect role.log")
        record["passed"] = True
    finally:
        (args.evidence / "result.json").write_text(json.dumps(record, indent=2))
        box.down()
    if box.state() != "stopped":
        record["passed"] = False
        (args.evidence / "result.json").write_text(json.dumps(record, indent=2))
        raise RuntimeError("Browser acceptance cleanup left a VM running")
    record["vms_stopped"] = True
    (args.evidence / "result.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2), flush=True)


if __name__ == "__main__":
    main()
