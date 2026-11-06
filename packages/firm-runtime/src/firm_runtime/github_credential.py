"""Guest GitHub credentials from the runtime: a git credential helper and a `gh` wrapper.

`git` mode speaks the git credential helper protocol; `gh` mode picks a token for the
repository the command targets and execs the real gh with GH_TOKEN. The activation token
is read from the run's credentials directory, never from the environment.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GH = "/usr/bin/gh"
REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
REMOTE_PREFIXES = ("https://github.com/", "ssh://git@github.com/", "git@github.com:", "github.com/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def repository(value):
    """`owner/name` from a repository argument or GitHub remote URL, else None."""
    value = value.strip()
    for prefix in REMOTE_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    else:
        if "://" in value or "@" in value:
            return None
    value = value.rstrip("/").removesuffix(".git")
    return value if REPOSITORY.fullmatch(value) else None


def fetch(repo):
    url = os.environ["FIRM_GITHUB_URL"]
    if repo is not None:
        url += "?" + urllib.parse.urlencode({"repo": repo})
    token = (Path(os.environ["FIRM_CREDENTIALS_DIR"]) / "activation-token").read_text().strip()
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=20) as response:
            status, raw = 200, response.read(65536)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(65536)
    except OSError:
        return 503, {}
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict) or (status == 200 and not isinstance(body.get("token"), str)):
        return (503 if status == 200 else status), {}
    return status, body


def explain(status, body, repo):
    error = body.get("error")
    if status == 401:
        return "firm: GitHub credentials exist only during your work period"
    if error == "github_not_configured":
        return "firm: this firm has no GitHub access"
    if error == "github_repo_invalid":
        return f"firm: {repo} is not a GitHub repository name"
    if error == "github_owner_revoked":
        return (
            f"firm: an owner revoked access to {repo}; request_access with kind "
            "github_repository opens a request for the owner to decide"
        )
    if error == "github_not_granted":
        if repo is None:
            return "firm: your role is not granted any GitHub repository"
        return (
            f"firm: {repo} is not granted to your role; use request_access with kind "
            "github_repository, or create_repository for a new one"
        )
    return "firm: GitHub access is temporarily unavailable; try again shortly"


def git(action):
    fields = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        fields[key] = value
    if action != "get" or fields.get("protocol") != "https" or fields.get("host") != "github.com":
        return 0
    # Git LFS asks for `owner/name.git/info/lfs`; the repository is the first two segments.
    repo = repository("/".join(fields.get("path", "").split("/")[:2]))
    if repo is None:
        return 0
    status, body = fetch(repo)
    if status != 200:
        print(explain(status, body, repo), file=sys.stderr)
        print("quit=1")
        return 0
    print("username=x-access-token")
    print(f"password={body['token']}")
    print(f"password_expiry_utc={int(body['expires_at'])}")
    return 0


def _argument_repo(args):
    for index, arg in enumerate(args):
        if arg == "--":
            break
        if arg in ("-R", "--repo") and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--repo="):
            return arg.split("=", 1)[1]
        # `-Rowner/name`; anything else starting with -R is another flag's value.
        if arg.startswith("-R") and repository(arg[2:]):
            return arg[2:]
    return None


def _origin_repo():
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return repository(remote)


def gh(args):
    named = _argument_repo(args) or os.environ.get("GH_REPO")
    repo = repository(named) if named else _origin_repo()
    if named and repo is None:
        print(f"firm: {named} is not a GitHub repository name", file=sys.stderr)
        return 1
    status, body = fetch(repo)
    if status == 403 and body.get("error") == "github_not_granted" and not named:
        # A checkout of someone else's repository still gets read access to the grants.
        repo = None
        status, body = fetch(None)
    if status != 200:
        print(explain(status, body, named or repo), file=sys.stderr)
        return 1
    os.execve(GH, [GH, *args], {**os.environ, "GH_TOKEN": body["token"]})
    return 1


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("git", "gh"):
        print("usage: github-credential git ACTION | gh ARGS...", file=sys.stderr)
        return 2
    if not os.environ.get("FIRM_GITHUB_URL") or not os.environ.get("FIRM_CREDENTIALS_DIR"):
        if sys.argv[1] == "gh":
            os.execv(GH, [GH, *sys.argv[2:]])
        return 0
    if sys.argv[1] == "git":
        return git(sys.argv[2] if len(sys.argv) > 2 else "")
    return gh(sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
