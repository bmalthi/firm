import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from firm_sandbox.linux_vm import IMAGE

from firm_runtime import github_credential
from firm_runtime.github_credential import repository

ACTIVATION_TOKEN = "activation-secret"
GRANTS = {"firmery/app": "write", "firmery/docs": "read"}


@pytest.fixture
def route():
    """The runtime's /github/token route for one role granted GRANTS."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            repo = query.get("repo", [None])[0]
            calls.append(repo)
            if self.headers.get("Authorization") != f"Bearer {ACTIVATION_TOKEN}":
                status, body = 401, {"error": "invalid_token"}
            elif repo is None:
                status, body = 200, {"token": "ghs_all", "expires_at": 1.9e9, "access": "read"}
            elif repo in GRANTS:
                status = 200
                body = {"token": f"ghs_{repo}", "expires_at": 1.9e9, "access": GRANTS[repo]}
            else:
                status, body = 403, {"error": "github_not_granted", "repo": repo}
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/github/token", calls
    server.shutdown()


@pytest.fixture
def run(tmp_path, route):
    """A role's run: credentials directory, environment and the VM's system gitconfig."""
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    shutil.copy(github_credential.__file__, credentials / "github-credential")
    (credentials / "activation-token").write_text(ACTIVATION_TOKEN)
    template = IMAGE.read_text()
    start = template.index("cat > /etc/gitconfig <<'GITCONFIG'\n")
    block = template[template.index("\n", start) + 1 : template.index("      GITCONFIG\n", start)]
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(textwrap.dedent(block).replace("/usr/bin/python3", sys.executable))
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_SYSTEM": str(gitconfig),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "FIRM_CREDENTIALS_DIR": str(credentials),
        "FIRM_GITHUB_URL": route[0],
    }
    return env


def fill(env, path):
    return subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost=github.com\npath={path}\n\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def test_git_gets_a_repository_token_through_the_system_gitconfig(run, route):
    result = fill(run, "firmery/app.git")
    assert result.returncode == 0, result.stderr
    assert "username=x-access-token\n" in result.stdout
    assert "password=ghs_firmery/app\n" in result.stdout
    assert "password_expiry_utc=1900000000\n" in result.stdout
    assert route[1] == ["firmery/app"]


def test_git_lfs_paths_ask_for_their_repository(run, route):
    result = fill(run, "firmery/app.git/info/lfs")
    assert "password=ghs_firmery/app\n" in result.stdout
    assert route[1] == ["firmery/app"]


def test_git_stops_with_a_clear_refusal_for_an_ungranted_repository(run):
    result = fill(run, "firmery/secret")
    assert result.returncode != 0
    assert "firmery/secret is not granted to your role" in result.stderr
    assert "ghs_" not in result.stdout + result.stderr


def test_outside_a_run_the_helper_stays_out_of_the_way(run, route):
    env = {**run, "FIRM_CREDENTIALS_DIR": ""}
    result = fill(env, "firmery/app")
    assert result.returncode != 0
    assert route[1] == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("firmery/app", "firmery/app"),
        ("https://github.com/firmery/app.git", "firmery/app"),
        ("git@github.com:firmery/app.git\n", "firmery/app"),
        ("github.com/firmery/app/", "firmery/app"),
        ("https://gitlab.com/firmery/app", None),
        ("firmery", None),
    ],
)
def test_repository_names_come_from_arguments_and_remotes(value, expected):
    assert repository(value) == expected


@pytest.mark.parametrize(
    ("args", "env", "requested", "token"),
    [
        (["pr", "create", "-R", "firmery/app"], {}, ["firmery/app"], "ghs_firmery/app"),
        (["pr", "list", "--repo=firmery/docs"], {}, ["firmery/docs"], "ghs_firmery/docs"),
        (["issue", "list"], {"GH_REPO": "firmery/app"}, ["firmery/app"], "ghs_firmery/app"),
        (
            ["pr", "create"],
            {"origin": "https://github.com/firmery/app"},
            ["firmery/app"],
            "ghs_firmery/app",
        ),
        (
            ["pr", "list"],
            {"origin": "https://github.com/octo/tool"},
            ["octo/tool", None],
            "ghs_all",
        ),
        (["repo", "list"], {}, [None], "ghs_all"),
        (["pr", "create", "--title", "-Rework parser"], {}, [None], "ghs_all"),
        (["api", "--", "-R", "firmery/app"], {}, [None], "ghs_all"),
        (["pr", "view", "-Rfirmery/app"], {}, ["firmery/app"], "ghs_firmery/app"),
    ],
)
def test_gh_uses_the_token_for_the_targeted_repository(
    tmp_path, run, route, monkeypatch, args, env, requested, token
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, env=run)
    if "origin" in env:
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "add", "origin", env.pop("origin")],
            check=True,
            env=run,
        )
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("GH_REPO", raising=False)
    for name, value in {**run, **env}.items():
        monkeypatch.setenv(name, value)
    executed = []
    monkeypatch.setattr(
        github_credential.os, "execve", lambda *call: executed.append(call) or sys.exit(0)
    )
    with pytest.raises(SystemExit):
        github_credential.gh(args)
    ((program, argv, environment),) = executed
    assert program == github_credential.GH and argv == [github_credential.GH, *args]
    assert environment["GH_TOKEN"] == token
    assert route[1] == requested


def test_gh_refuses_an_explicit_ungranted_repository(run, monkeypatch, capsys):
    for name, value in run.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(github_credential.os, "execve", pytest.fail)
    assert github_credential.gh(["pr", "list", "-R", "firmery/secret"]) == 1
    assert "firmery/secret is not granted" in capsys.readouterr().err
    assert github_credential.gh(["pr", "list", "--repo", "https://gitlab.com/x/y"]) == 1
    assert "is not a GitHub repository name" in capsys.readouterr().err


def test_the_helper_fetches_credentials_without_site_packages(run, route):
    result = subprocess.run(
        [sys.executable, "-I", "-S", github_credential.__file__, "git", "get"],
        input="protocol=https\nhost=github.com\npath=firmery/app.git\n\n",
        capture_output=True,
        text=True,
        env=run,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "password=ghs_firmery/app\n" in result.stdout
    assert route[1] == ["firmery/app"]
