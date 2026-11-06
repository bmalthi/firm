import html
import json
import re
import stat
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from firm_runtime import github
from firm_runtime.errors import FirmError
from firm_runtime.github import GitHubRefused, grants_for, validate_github
from firm_runtime.requests import withdraw_role
from firm_runtime.runtime import Runtime
from firm_runtime.store import OWNER, Store

PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write"}


def pem(private):
    return private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class FakeGitHub:
    """GitHub's App endpoints as the runtime uses them; tokens are ghs_1, ghs_2, ..."""

    def __init__(self, clock):
        self.clock = clock
        self.requests = []
        self.revoked = []
        self.minted = 0
        self.unreachable = set()
        self.pending_installation_checks = 0
        self.creates_public = False
        self.repositories = {}
        self.deleted = []
        self.admin_tokens = set()
        self.pem = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body=None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                auth = self.headers.get("Authorization")
                token = (auth or "").removeprefix("Bearer ")
                fake.requests.append((self.command, self.path, auth, body))
                route = (self.command, self.path)
                if route == ("GET", "/orgs/firmery/installation"):
                    if fake.pending_installation_checks:
                        fake.pending_installation_checks -= 1
                        return self._reply(404, {"message": "Not Found"})
                    return self._reply(200, {"id": 42})
                if route == ("GET", "/users/firmery-roles%5Bbot%5D"):
                    return self._reply(200, {"id": 777, "login": "firmery-roles[bot]"})
                if route == ("POST", "/app-manifests/abc123/conversions"):
                    return self._reply(
                        201,
                        {
                            "id": 1234567,
                            "slug": "firmery-roles",
                            "pem": fake.pem,
                            "client_id": "Iv1.client",
                            "client_secret": "client-secret-value",
                            "webhook_secret": "webhook-secret-value",
                        },
                    )
                if route == ("POST", "/app/installations/42/access_tokens"):
                    if fake.unreachable & set(body.get("repositories", [])):
                        return self._reply(422, {"message": "no access"})
                    fake.minted += 1
                    issued = f"ghs_{fake.minted}"
                    if "administration" in body["permissions"]:
                        fake.admin_tokens.add(issued)
                    expires = datetime.fromtimestamp(fake.clock() + 3600, UTC)
                    return self._reply(201, {"token": issued, "expires_at": expires.isoformat()})
                if route == ("DELETE", "/installation/token"):
                    fake.revoked.append(token)
                    return self._reply(204)
                if route == ("POST", "/orgs/firmery/repos"):
                    if token not in fake.admin_tokens:
                        return self._reply(403, {"message": "Forbidden"})
                    if body["name"] in fake.repositories:
                        return self._reply(422, {"message": "name already exists"})
                    fake.repositories[body["name"]] = {**body, "private": not fake.creates_public}
                    return self._reply(201, fake.repositories[body["name"]])
                name = self.path.removeprefix("/repos/firmery/")
                if self.path.startswith("/repos/firmery/") and name in fake.repositories:
                    if token not in fake.admin_tokens:
                        return self._reply(403, {"message": "Forbidden"})
                    if self.command == "GET":
                        return self._reply(200, fake.repositories[name])
                    if self.command == "DELETE":
                        fake.deleted.append(fake.repositories.pop(name)["name"])
                        return self._reply(204)
                return self._reply(404, {"message": "Not Found"})

            do_GET = do_POST = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def mints(self):
        return [r for r in self.requests if r[1].endswith("/access_tokens")]


@pytest.fixture
def credentials(monkeypatch, tmp_path):
    directory = tmp_path / "credentials"
    monkeypatch.setattr(github, "CREDENTIALS_DIR", directory)
    return directory


@pytest.fixture
def key(credentials):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    github.save_app("firmery", 1234567, "firmery-roles", pem(private).decode())
    return private.public_key()


@pytest.fixture
def clock():
    now = [1_900_000_000.0]
    tick = lambda: now[0]  # noqa: E731
    tick.now = now
    return tick


@pytest.fixture
def fake(monkeypatch, clock):
    server = FakeGitHub(clock)
    monkeypatch.setattr(github, "API", server.url)
    yield server
    server.server.shutdown()


def settings(grants=None, **policy):
    return {
        "organization": "firmery",
        "grants": [{"repo": "firmery/firm-github-check", "address": "founder", "access": "write"}]
        if grants is None
        else grants,
        **policy,
    }


def firm(tmp_path, clock, grants=None, **policy):
    runtime = Runtime(Store.scratch(tmp_path / "firm.sqlite3", clock=clock))
    runtime.lifecycle.initialize(
        "test",
        "Build useful things.",
        {
            "max_active": 2,
            "staffing": False,
            "founder_profile": "default",
            "profiles": {"default": {}},
            "github": settings(grants, **policy),
        },
    )
    runtime.start()
    founder = runtime.admit()
    runtime.mark_started(founder.activation_id)
    return runtime, founder


def hire(runtime, founder, name):
    """A second role of the firm, admitted and running under the founder."""
    role = runtime.lifecycle.hire(
        founder.employee_id, name, "Do work", "default", request_id=f"hire-{name}"
    )
    invocation = runtime.admit()
    assert invocation.employee_id == role["employee_id"]
    runtime.mark_started(invocation.activation_id)
    return invocation


def events(runtime):
    with runtime.store.read() as db:
        return [
            (row["kind"], json.loads(row["payload_json"]))
            for row in db.execute(
                "SELECT kind,payload_json FROM analysis_events WHERE kind LIKE 'github.%' "
                "ORDER BY sequence"
            )
        ]


def create(tools, request_id, name, purpose):
    return tools.create_repository(name=name, purpose=purpose, request_id=request_id)


def ask(tools, request_id, resource):
    return tools.request_access(
        kind="github_repository", resource=resource, purpose="Work", request_id=request_id
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"app_id": 1}, "unsupported settings"),
        ({"private_key_file": "/k.pem"}, "unsupported settings"),
        ({"organization": "not/an/org"}, "organization"),
        ({"auto_create": "yes"}, "auto_create"),
        ({"max_repositories": 0}, "max_repositories"),
        ({"grants": [{"repo": "other/x", "address": "founder", "access": "read"}]}, "firmery/"),
        ({"grants": [{"repo": "firmery/x", "address": "Founder!", "access": "read"}]}, "address"),
        ({"grants": [{"repo": "firmery/x", "address": "founder", "access": "admin"}]}, "access"),
        ({"grants": [{"repo": "firmery/x", "address": "founder"}]}, "exactly"),
    ],
)
def test_validation_rejects_unsafe_or_unknown_settings(change, message):
    with pytest.raises(FirmError, match=message):
        validate_github({"github": {**settings(), **change}})


def test_validation_defaults_policy_and_grants():
    config = {"github": {"organization": "firmery"}}
    validate_github(config)
    assert config["github"] == {
        "organization": "firmery",
        "auto_create": True,  # naming an organization is asking for repositories in it
        "max_repositories": 20,
        "grants": [],
    }


def test_grants_cover_the_address_subtree_requests_the_exact_address_and_widest_wins():
    config = {
        "grants": [
            {"repo": "firmery/App", "address": "founder", "access": "read"},
            {"repo": "firmery/app", "address": "founder/engineering", "access": "write"},
            {"repo": "firmery/docs", "address": "founder/engineering", "access": "read"},
        ]
    }
    assert grants_for(config, "founder") == {"firmery/app": "read"}
    assert grants_for(config, "founder/engineering/web") == {
        "firmery/app": "write",
        "firmery/docs": "read",
    }
    assert grants_for(config, "founderx") == {}
    assert grants_for(config, "founder", [("firmery/docs", "write")]) == {
        "firmery/app": "read",
        "firmery/docs": "write",
    }


def test_the_app_is_saved_host_wide_per_organization_and_privately(credentials, key):
    assert github.saved_app("Firmery") == {"app_id": 1234567, "slug": "firmery-roles"}
    for name in ("github-firmery.pem", "github-firmery.json"):
        assert stat.S_IMODE((credentials / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o700
    assert github.saved_app("other") is None
    (credentials / "github-firmery.pem").chmod(0o644)
    with pytest.raises(github.GitHubUnavailable, match="private"):
        github.read_key(credentials / "github-firmery.pem")


def test_connect_resolves_identity_and_probes_every_grant(fake, key, clock):
    app = github.connect(settings(), now=clock())
    assert app == {
        "app_id": 1234567,
        "organization": "firmery",
        "installation_id": 42,
        "bot_login": "firmery-roles[bot]",
        "bot_user_id": 777,
    }
    by_path = {(method, route): (auth, body) for method, route, auth, body in fake.requests}
    claims = jwt.decode(
        by_path[("GET", "/orgs/firmery/installation")][0].removeprefix("Bearer "),
        key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "1234567" and claims["exp"] - claims["iat"] <= 600
    assert by_path[("GET", "/users/firmery-roles%5Bbot%5D")][0] is None
    assert by_path[("POST", "/app/installations/42/access_tokens")][1] == {
        "repositories": ["firm-github-check"],
        "permissions": PERMISSIONS,
    }
    assert fake.revoked == ["ghs_1"]

    fake.unreachable.add("firm-github-check")
    with pytest.raises(FirmError, match="cannot reach every granted repository"):
        github.connect(settings(), now=clock())
    fake.pending_installation_checks = 1
    with pytest.raises(FirmError, match="apps/firmery-roles/installations/new"):
        github.connect(settings(), now=clock())


def test_connect_without_a_saved_app_says_how_to_register(credentials):
    with pytest.raises(FirmError, match="in a terminal to register"):
        github.connect(settings())


def test_registration_posts_the_manifest_saves_the_app_and_waits_for_installation(
    fake, credentials, clock
):
    fake.pem = pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)).decode()
    fake.pending_installation_checks = 2
    opened = []

    def browser(url):
        opened.append(url)
        if len(opened) > 1:
            return
        page = urllib.request.urlopen(url).read().decode()
        action = re.search(r'action="([^"]*)"', page).group(1)
        assert action.startswith(
            "https://github.com/organizations/firmery/settings/apps/new?state="
        )
        state = html.unescape(action).split("state=", 1)[1]
        manifest = json.loads(
            html.unescape(re.search(r'name="manifest" value="([^"]*)"', page).group(1))
        )
        assert manifest["name"] == "firmery-roles" and manifest["public"] is False
        assert manifest["hook_attributes"]["active"] is False
        assert manifest["default_events"] == []
        assert manifest["default_permissions"] == {
            "administration": "write",
            **PERMISSIONS,
            "metadata": "read",
        }
        callback = manifest["redirect_url"]
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/callback", callback)
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{callback}?code=abc123&state=wrong")
        urllib.request.urlopen(f"{callback}?code=abc123&state={state}")

    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock.now[0] += seconds

    github.register("firmery", open_browser=browser, clock=clock, sleep=sleep)
    assert opened[1] == "https://github.com/apps/firmery-roles/installations/new"
    assert slept == [2, 2]
    assert github.saved_app("firmery") == {"app_id": 1234567, "slug": "firmery-roles"}
    assert (credentials / "github-firmery.pem").read_text() == fake.pem
    saved = (credentials / "github-firmery.json").read_text()
    assert "secret" not in saved and "client" not in saved


def test_registration_gives_up_when_the_app_is_never_installed(
    fake, credentials, clock, monkeypatch
):
    fake.pem = pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)).decode()
    fake.pending_installation_checks = 100
    monkeypatch.setattr(github, "REGISTRATION_TIMEOUT", 6)

    def browser(url):
        if "/installations/new" in url:
            return
        page = urllib.request.urlopen(url).read().decode()
        state = html.unescape(re.search(r'action="([^"]*)"', page).group(1)).split("state=")[1]
        port = url.split(":")[2].rstrip("/")
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=abc123&state={state}")

    def sleep(seconds):
        clock.now[0] += seconds

    with pytest.raises(FirmError, match="not installed on firmery"):
        github.register("firmery", open_browser=browser, clock=clock, sleep=sleep)
    assert github.saved_app("firmery") is not None


def test_init_and_configure_record_the_app_or_refuse_before_saving(tmp_path, fake, key, clock):
    runtime, _ = firm(tmp_path, clock)
    with runtime.store.read() as db:
        assert dict(db.execute("SELECT * FROM github_app").fetchone())["bot_user_id"] == 777
        config = runtime.accounts.config(db)
    config["github"]["grants"].append(
        {"repo": "firmery/missing", "address": "founder", "access": "read"}
    )
    fake.unreachable.add("missing")
    with pytest.raises(FirmError, match="cannot reach"):
        runtime.owner.configure(OWNER, config)
    with runtime.store.read() as db:
        assert len(runtime.accounts.config(db)["github"]["grants"]) == 1
    del config["github"]
    runtime.owner.configure(OWNER, config)
    with runtime.store.read() as db:
        assert db.execute("SELECT * FROM github_app").fetchone() is None


def test_tokens_are_narrow_cached_refreshed_and_revoked_at_settlement(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock)
    first = runtime.github.issue(founder.token, "firmery/Firm-GitHub-Check")
    assert first["access"] == "write" and first["token"] == "ghs_2"
    assert fake.mints()[-1][3] == {
        "repositories": ["firm-github-check"],
        "permissions": PERMISSIONS,
    }
    assert runtime.github.issue(founder.token, "firmery/firm-github-check") == first
    reading = runtime.github.issue(founder.token)
    assert reading["access"] == "read" and fake.mints()[-1][3]["permissions"]["contents"] == "read"

    clock.now[0] += 3600 - github.REFRESH_MARGIN + 1
    renewed = runtime.github.issue(founder.token, "firmery/firm-github-check")
    assert renewed["token"] == "ghs_4" and "ghs_2" not in fake.revoked
    runtime.github.settle()
    assert fake.revoked == ["ghs_1"]

    runtime.finish(founder.activation_id)
    runtime.github.settle()
    assert sorted(fake.revoked[1:]) == ["ghs_2", "ghs_3", "ghs_4"]
    assert runtime.github.tokens == {}
    with pytest.raises(FirmError):
        runtime.github.issue(founder.token, "firmery/firm-github-check")
    assert "ghs_" not in json.dumps(events(runtime))


def test_closing_the_host_revokes_every_live_token(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock)
    runtime.github.issue(founder.token, "firmery/firm-github-check")
    runtime.github.issue(founder.token)
    runtime.github.revoke_all()
    assert sorted(fake.revoked[1:]) == ["ghs_2", "ghs_3"]
    assert runtime.github.tokens == {}


def test_expired_tokens_are_dropped_without_a_revocation_call(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock)
    runtime.github.issue(founder.token, "firmery/firm-github-check")
    clock.now[0] += 3601
    runtime.finish(founder.activation_id)
    runtime.github.settle()
    assert fake.revoked == ["ghs_1"] and runtime.github.tokens == {}


def test_an_unusable_key_is_reported_to_the_owner(fake, key, clock, credentials):
    (credentials / "github-firmery.pem").write_bytes(
        b"-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----\n"
    )
    with pytest.raises(FirmError, match="not a usable RSA key"):
        github.connect(settings(), now=clock())


def test_refusals_are_recorded_and_ungranted_roles_mint_nothing(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock)
    before = len(fake.mints())
    for repo, code in (
        ("firmery/other", "github_not_granted"),
        ("firmery", "github_repo_invalid"),
    ):
        with pytest.raises(GitHubRefused) as refusal:
            runtime.github.issue(founder.token, repo)
        assert refusal.value.code == code
    assert len(fake.mints()) == before
    assert [kind for kind, _ in events(runtime)] == ["github.token_refused"] * 2


def test_removing_a_grant_revokes_cached_tokens(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock)
    token = runtime.github.issue(founder.token, "firmery/firm-github-check")["token"]
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    config["github"]["grants"][0]["access"] = "read"
    runtime.owner.configure(OWNER, config)
    runtime.github.settle()
    assert token in fake.revoked
    assert runtime.github.issue(founder.token, "firmery/firm-github-check")["access"] == "read"


@pytest.mark.parametrize("change", ["remove", "downgrade", "finish"])
def test_token_minted_during_authority_change_is_revoked_before_delivery(
    tmp_path, fake, key, clock, monkeypatch, change
):
    runtime, founder = firm(tmp_path, clock)
    original = github._mint
    minted = []

    def mint_then_change(*args):
        result = original(*args)
        if not minted:
            minted.append(result[0])
            if change == "finish":
                runtime.finish(founder.activation_id)
            else:
                with runtime.store.read() as db:
                    config = runtime.accounts.config(db)
                if change == "remove":
                    config["github"]["grants"] = []
                else:
                    config["github"]["grants"][0]["access"] = "read"
                runtime.owner.configure(OWNER, config)
        return result

    monkeypatch.setattr(github, "_mint", mint_then_change)
    with pytest.raises(FirmError if change == "finish" else GitHubRefused):
        runtime.github.issue(founder.token, "firmery/firm-github-check")
    assert minted[0] in fake.revoked
    assert runtime.github.tokens == {}
    assert not any(kind == "github.token_minted" for kind, _ in events(runtime))


def test_a_created_repository_is_private_granted_announced_and_usable_at_once(
    tmp_path, fake, key, clock
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True, max_repositories=2)
    tools = runtime.tools(founder.token)
    granted = create(tools, "make-app", "app", "The web app")
    assert granted == {
        "status": "granted",
        "repo": "firmery/test-app",
        "access": "write",
        "clone_url": "https://github.com/firmery/test-app.git",
    }
    assert fake.repositories["test-app"] == {
        "name": "test-app",
        "description": "The web app",
        "private": True,
        "visibility": "private",
        "auto_init": True,
    }
    (admin,) = fake.admin_tokens
    assert admin in fake.revoked
    assert fake.mints()[-1][3] == {"permissions": {"administration": "write"}}
    assert create(tools, "make-app", "app", "The web app") == granted
    assert len(fake.repositories) == 1 and len(fake.admin_tokens) == 1

    issued = runtime.github.issue(founder.token, "firmery/test-app")
    assert issued["access"] == "write"
    assert fake.mints()[-1][3]["permissions"] == PERMISSIONS
    assert [kind for kind, _ in events(runtime)] == [
        "github.repository_created",
        "github.access_granted",
        "github.token_minted",
    ]
    assert events(runtime)[1][1] == {
        "repository": "firmery/test-app",
        "access": "write",
        "source": "created",
    }
    with runtime.store.read() as db:
        mail = db.execute(
            "SELECT author_id,recipients_json,subject,body FROM messages"
        ).fetchone()
    assert mail["author_id"] == "runtime" and OWNER in mail["recipients_json"]
    assert mail["subject"] == "GitHub repository created"
    assert "founder created the private GitHub repository firmery/test-app" in mail["body"]
    assert "ghs_" not in json.dumps(events(runtime))
    # The owner reads GitHub's immediate grants in the same list as requests awaiting them.
    listing = runtime.requests.listing(OWNER)
    assert listing["pending"] == []
    (granted,) = listing["recent"]
    assert (granted["kind"], granted["status"], granted["resource"], granted["purpose"]) == (
        "github_repository", "granted", "firmery/test-app", "The web app"
    )


def test_repository_requests_are_refused_by_policy(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True, max_repositories=1)
    tools = runtime.tools(founder.token)
    assert create(tools, "one", "one", "First")["status"] == "granted"
    again = create(tools, "again", "one", "Again")
    assert (again["status"], again["reason"]) == ("refused", "github_name_taken")
    for arguments, message in (
        ({"name": "Upper", "purpose": "x"}, "lower-case"),
        ({"name": "x" * 100, "purpose": "x"}, "at most 100"),
        ({"name": "ok", "purpose": ""}, "purpose"),
    ):
        with pytest.raises(FirmError, match=message):
            tools.create_repository(request_id="invalid", **arguments)
    with pytest.raises(FirmError, match="different operation or payload"):
        create(tools, "again", "three", "Third")
    refused = [payload["error"] for kind, payload in events(runtime) if kind.endswith("refused")]
    assert refused == ["github_name_taken"]


def test_a_full_cap_opens_an_owner_request_that_creates_the_repository_when_granted(
    tmp_path, fake, key, clock
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True, max_repositories=1)
    tools = runtime.tools(founder.token)
    assert create(tools, "one", "one", "First")["status"] == "granted"
    pending = create(tools, "two", "two", "Second")
    assert pending["status"] == "pending" and pending["cause"] == "github_limit_reached"
    assert "reason" not in pending and "two" not in fake.repositories
    # Retrying the same call, or asking again under a new id, returns the one open request.
    assert create(tools, "two", "two", "Second") == pending
    assert create(tools, "two-again", "two", "Second") == pending
    (request,) = runtime.requests.listing(OWNER)["pending"]
    assert (request["kind"], request["resource"], request["create"]) == (
        "github_repository", "firmery/test-two", True
    )

    # The owner decided, so the cap does not apply to the repository they allowed.
    result = runtime.requests.grant(OWNER, pending["request_id"])
    assert result["answered"] == [pending["request_id"]]
    assert fake.repositories["test-two"]["private"] is True
    assert all(token in fake.revoked for token in fake.admin_tokens)
    assert runtime.github.issue(founder.token, "firmery/test-two")["access"] == "write"
    with runtime.store.read() as db:
        assert tuple(db.execute(
            "SELECT repository,source FROM github_grants WHERE repository='firmery/test-two'"
        ).fetchone()) == ("firmery/test-two", "created")
        assert db.execute(
            "SELECT status FROM owner_requests WHERE request_id=?", (pending["request_id"],)
        ).fetchone()[0] == "granted"
        assert "firmery/test-two" in db.execute(
            "SELECT body FROM messages WHERE subject='Access granted: firmery/test-two'"
        ).fetchone()[0]
    assert ("github.repository_created", {"repository": "firmery/test-two"}) in events(runtime)
    # Its own call now finds the name taken.
    assert create(tools, "two-late", "two", "Second")["reason"] == "github_name_taken"


def test_a_repository_created_after_its_request_ended_is_still_the_firms(
    tmp_path, fake, key, clock, monkeypatch
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=False)
    pending = create(runtime.tools(founder.token), "late", "late", "Late")
    make = runtime.github._make

    def retired_meanwhile(*args):
        made = make(*args)
        with runtime.store.transaction() as db:
            withdraw_role(db, clock(), founder.employee_id)
        return made

    monkeypatch.setattr(runtime.github, "_make", retired_meanwhile)
    with pytest.raises(FirmError, match="already withdrawn; no role was given access"):
        runtime.requests.grant(OWNER, pending["request_id"])
    assert "test-late" in fake.repositories
    with runtime.store.read() as db:
        assert db.execute(
            "SELECT repository FROM github_repositories"
        ).fetchone()[0] == "firmery/test-late"
        assert db.execute("SELECT 1 FROM github_grants").fetchone() is None


def test_creation_is_on_by_default_and_the_owner_can_withhold_it(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=False)
    result = create(runtime.tools(founder.token), "make", "app", "App")
    assert result["status"] == "pending" and result["cause"] == "github_auto_create_off"
    assert fake.repositories == {}

    runtime, founder = firm(tmp_path / "default", clock, grants=[])
    result = create(runtime.tools(founder.token), "make", "app", "App")
    assert result["status"] == "granted" and fake.repositories["test-app"]["private"] is True


def test_a_denied_creation_stays_denied_for_the_next_asker(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=False)
    tools = runtime.tools(founder.token)
    pending = create(tools, "make", "app", "App")
    runtime.requests.deny(OWNER, pending["request_id"], "Not this quarter")
    assert fake.repositories == {}
    # The retry replays what the role was told then; a new ask meets the owner's answer.
    assert create(tools, "make", "app", "App")["status"] == "pending"
    assert create(tools, "again", "app", "App")["reason"] == "access_denied"
    assert create(tools, "again", "app", "App")["owner_note"] == "Not this quarter"


def test_requests_for_other_repositories_are_checked_then_decided_by_the_owner(
    tmp_path, fake, key, clock, monkeypatch
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    tools = runtime.tools(founder.token)
    for index, resource in enumerate(("firmery", "elsewhere/app", "firmery/a b", 7)):
        result = ask(tools, f"invalid-{index}", resource)
        assert result == {"status": "refused", "reason": "access_resource_invalid"}
    # Reachable but not made by this firm: the owner decides.
    pending = ask(tools, "other", "firmery/Elsewhere")
    assert pending["status"] == "pending" and pending["cause"] == "github_not_this_firm"
    assert fake.minted == 1 and fake.mints()[-1][3] == {
        "permissions": PERMISSIONS, "repositories": ["elsewhere"]
    }
    assert fake.revoked == ["ghs_1"]
    assert ask(tools, "other-again", "firmery/elsewhere")["request_id"] == pending["request_id"]
    (request,) = runtime.requests.listing(OWNER)["pending"]
    assert request["resource"] == "firmery/elsewhere" and request["cause"] == "github_not_this_firm"
    # Not reachable by the App: nothing to ask the owner about.
    fake.unreachable.add("hidden")
    assert ask(tools, "hidden", "firmery/hidden") == {
        "status": "refused", "reason": "access_resource_invalid"
    }
    assert len(runtime.requests.listing(OWNER)["pending"]) == 1
    # GitHub failing is not recorded, so the same id asks again.
    fake.unreachable.clear()

    def unavailable(*args):
        raise github.GitHubUnavailable("GitHub answered with 503", 503)

    monkeypatch.setattr(github, "_mint", unavailable)
    down = ask(tools, "down", "firmery/other-repo")
    assert down == {"status": "refused", "reason": "github_unavailable"}
    with runtime.store.read() as db:
        assert db.execute("SELECT 1 FROM requests WHERE request_id='down'").fetchone() is None


def test_a_configured_write_grant_answers_at_once_and_asks_no_one(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, auto_create=True)
    minted = fake.minted
    answer = ask(runtime.tools(founder.token), "configured", "firmery/firm-github-check")
    assert answer["status"] == "granted" and answer["repo"] == "firmery/firm-github-check"
    assert fake.minted == minted and runtime.requests.listing(OWNER)["pending"] == []


def test_the_owner_grants_a_repository_the_firm_did_not_create(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    tools = runtime.tools(founder.token)
    pending = ask(tools, "other", "firmery/elsewhere")
    assert runtime.requests.listing(OWNER)["pending"][0]["address"] == "founder"
    with pytest.raises(GitHubRefused):
        runtime.github.issue(founder.token, "firmery/elsewhere")

    fake.unreachable.add("elsewhere")
    with pytest.raises(FirmError, match="cannot reach firmery/elsewhere"):
        runtime.requests.grant(OWNER, pending["request_id"])
    assert len(runtime.requests.listing(OWNER)["pending"]) == 1
    fake.unreachable.clear()

    result = runtime.requests.grant(OWNER, pending["request_id"])
    assert result["answered"] == [pending["request_id"]] and result["granted"] == "write"
    assert runtime.github.issue(founder.token, "firmery/elsewhere")["access"] == "write"
    with runtime.store.read() as db:
        assert tuple(db.execute("SELECT address,source FROM github_grants").fetchone()) == (
            "founder", "owner"
        )
        mail = db.execute(
            "SELECT recipients_json,body FROM messages WHERE subject LIKE 'Access granted%'"
        ).fetchone()
    assert "founder" in mail["recipients_json"] and "firmery/elsewhere" in mail["body"]
    # Asking once more finds the access already there.
    assert ask(tools, "again", "firmery/elsewhere")["status"] == "granted"
    # Granting it twice is refused: the request is no longer open.
    with pytest.raises(FirmError, match="already granted"):
        runtime.requests.grant(OWNER, pending["request_id"])


def test_a_revoked_repository_asks_the_owner_who_lifts_it_with_the_grant(
    tmp_path, fake, key, clock
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    tools = runtime.tools(founder.token)
    create(tools, "make", "app", "App")
    runtime.owner.revoke_github(OWNER, "firmery/test-app", "founder")
    pending = ask(tools, "back", "firmery/test-app")
    assert pending["status"] == "pending" and pending["cause"] == "github_owner_revoked"
    with pytest.raises(GitHubRefused):
        runtime.github.issue(founder.token, "firmery/test-app")

    result = runtime.requests.grant(OWNER, pending["request_id"])
    assert result["cleared"] == ["founder"] and result["answered"] == [pending["request_id"]]
    assert runtime.github.issue(founder.token, "firmery/test-app")["access"] == "write"
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM github_revocations").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE subject='Access granted: firmery/test-app'"
        ).fetchone()[0] == 1


def test_the_owner_denies_a_repository_and_a_second_ask_is_refused(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    analyst = hire(runtime, founder, "analyst")
    pending = ask(runtime.tools(founder.token), "other", "firmery/elsewhere")
    runtime.requests.deny(OWNER, pending["request_id"], "Not ours to touch")
    with pytest.raises(GitHubRefused):
        runtime.github.issue(founder.token, "firmery/elsewhere")
    with runtime.store.read() as db:
        notice = db.execute(
            "SELECT body FROM messages WHERE subject='Access declined: firmery/elsewhere'"
        ).fetchone()
    assert "Not ours to touch" in notice["body"]
    # Standing for this repository, whoever asks and whatever the case.
    for tools, request_id in ((runtime.tools(founder.token), "again"),
                              (runtime.tools(analyst.token), "analyst")):
        refused = ask(tools, request_id, "firmery/Elsewhere")
        assert (refused["reason"], refused["resource"], refused["owner_note"]) == (
            "access_denied", "firmery/elsewhere", "Not ours to touch"
        )


def test_firm_github_grant_takes_any_repository_of_the_organization_and_answers_requests(
    tmp_path, fake, key, clock
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    analyst = hire(runtime, founder, "analyst")
    pending = ask(runtime.tools(analyst.token), "other", "firmery/elsewhere")
    with pytest.raises(FirmError, match="repo must be firmery/"):
        runtime.owner.grant_github(OWNER, "other-org/elsewhere", "founder")
    fake.unreachable.add("elsewhere")
    with pytest.raises(FirmError, match="cannot reach"):
        runtime.owner.grant_github(OWNER, "firmery/elsewhere", "founder")
    fake.unreachable.clear()

    # A grant to another address leaves the analyst's request waiting.
    assert runtime.owner.grant_github(OWNER, "firmery/Elsewhere", "founder") == {
        "resource": "firmery/elsewhere",
        "status": "granted",
        "address": "founder",
        "cleared": [],
        "granted": "write",
        "answered": [],
    }
    result = runtime.owner.grant_github(OWNER, "firmery/elsewhere", "founder/analyst")
    assert result["answered"] == [pending["request_id"]]
    assert runtime.github.issue(analyst.token, "firmery/elsewhere")["access"] == "write"
    assert runtime.requests.listing(OWNER)["pending"] == []


def test_firm_github_revoke_withdraws_open_requests_it_covers(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    analyst = hire(runtime, founder, "analyst")
    create(runtime.tools(founder.token), "make", "app", "App")
    runtime.owner.revoke_github(OWNER, "firmery/test-app", "founder/analyst")
    mine = ask(runtime.tools(analyst.token), "back", "firmery/test-app")
    other = ask(runtime.tools(founder.token), "other", "firmery/elsewhere")
    assert mine["status"] == "pending" and other["status"] == "pending"

    result = runtime.owner.revoke_github(OWNER, "firmery/test-app", "founder/analyst")
    assert result["withdrawn"] == [mine["request_id"]]
    # A repository nobody holds is not the firm's to revoke; so is one with no grant row.
    with pytest.raises(FirmError, match="not a repository this firm created"):
        runtime.owner.revoke_github(OWNER, "firmery/elsewhere")
    (left,) = runtime.requests.listing(OWNER)["pending"]
    assert left["request_id"] == other["request_id"]
    # After the owner grants the other one, it can be revoked like a created one.
    runtime.requests.grant(OWNER, other["request_id"])
    assert runtime.owner.revoke_github(OWNER, "firmery/elsewhere")["revoked"] == ["founder"]
    assert ask(runtime.tools(founder.token), "ask", "firmery/elsewhere")["cause"] == (
        "github_owner_revoked"
    )


def test_an_owner_creation_and_a_role_creation_of_one_name_make_one_repository(
    tmp_path, fake, key, clock
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True, max_repositories=1)
    tools = runtime.tools(founder.token)
    create(tools, "one", "one", "First")
    pending = create(tools, "two", "two", "Second")
    inside = threading.Event()
    release = threading.Event()
    real = runtime.github._make

    def slow(*args):
        inside.set()
        assert release.wait(10)
        return real(*args)

    runtime.github._make = slow
    results = {}
    owner = threading.Thread(
        target=lambda: results.update(
            owner=runtime.requests.grant(OWNER, pending["request_id"])
        )
    )
    owner.start()
    assert inside.wait(10)
    # The role's own call waits for the lock the owner's creation holds.
    role = threading.Thread(
        target=lambda: results.update(role=create(tools, "two-late", "two", "Second"))
    )
    role.start()
    role.join(0.3)
    assert role.is_alive()
    release.set()
    owner.join(10)
    role.join(10)
    assert results["owner"]["status"] == "created"
    assert results["role"]["reason"] == "github_name_taken"
    assert list(fake.repositories) == ["test-one", "test-two"]


def test_a_name_taken_on_github_is_refused(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    fake.repositories["test-app"] = {"name": "test-app", "private": True}
    result = create(runtime.tools(founder.token), "make", "app", "App")
    assert result["reason"] == "github_name_taken"
    with runtime.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM github_repositories").fetchone()[0] == 0


def test_a_public_repository_is_deleted_and_the_request_can_be_retried(
    tmp_path, fake, key, clock
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    tools = runtime.tools(founder.token)
    fake.creates_public = True
    assert create(tools, "make", "app", "App")["reason"] == "github_unavailable"
    assert fake.deleted == ["test-app"] and fake.repositories == {}
    assert all(token in fake.revoked for token in fake.admin_tokens)
    fake.creates_public = False
    assert create(tools, "make", "app", "App")["status"] == "granted"


def test_any_role_gets_write_on_a_firm_repository_until_the_owner_revokes_it(
    tmp_path, fake, key, clock
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    create(runtime.tools(founder.token), "make", "app", "App")
    with runtime.store.transaction() as db:
        db.execute("DELETE FROM github_grants")
    with pytest.raises(GitHubRefused):
        runtime.github.issue(founder.token, "firmery/test-app")
    granted = ask(runtime.tools(founder.token), "ask", "firmery/Test-App")
    assert granted["status"] == "granted" and granted["repo"] == "firmery/test-app"
    token = runtime.github.issue(founder.token, "firmery/test-app")["token"]
    with runtime.store.read() as db:
        assert [tuple(row) for row in db.execute("SELECT address,source FROM github_grants")] == [
            ("founder", "requested")
        ]

    assert runtime.owner.revoke_github(OWNER, "firmery/test-app", "founder") == {
        "resource": "firmery/test-app",
        "status": "revoked",
        "address": "founder",
        "revoked": ["founder"],
        "withdrawn": [],
    }
    runtime.github.settle()
    assert token in fake.revoked
    with pytest.raises(FirmError, match="not a repository this firm created"):
        runtime.owner.revoke_github(OWNER, "firmery/elsewhere")
    with pytest.raises(FirmError, match="only the owner"):
        runtime.owner.revoke_github(founder.employee_id, "firmery/test-app")


def test_a_revoked_repository_stays_refused_until_the_owner_grants_it_again(
    tmp_path, fake, key, clock
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    tools = runtime.tools(founder.token)
    create(tools, "make", "app", "App")
    runtime.owner.revoke_github(OWNER, "firmery/test-app", "founder")

    with pytest.raises(GitHubRefused) as refusal:
        runtime.github.issue(founder.token, "firmery/test-app")
    assert refusal.value.code == "github_owner_revoked"
    asked = ask(tools, "ask-again", "firmery/test-app")
    assert (asked["status"], asked["cause"]) == ("pending", "github_owner_revoked")

    assert runtime.owner.grant_github(OWNER, "firmery/Test-App", "founder") == {
        "resource": "firmery/test-app",
        "status": "granted",
        "address": "founder",
        "cleared": ["founder"],
        "granted": "write",
        "answered": [asked["request_id"]],
    }
    assert runtime.github.issue(founder.token, "firmery/test-app")["access"] == "write"
    assert ask(tools, "ask-once-more", "firmery/test-app")["status"] == "granted"


def test_a_firm_wide_revocation_stops_every_role_until_the_owner_lifts_it(
    tmp_path, fake, key, clock
):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    analyst = hire(runtime, founder, "analyst")
    create(runtime.tools(founder.token), "make", "app", "App")
    assert ask(runtime.tools(analyst.token), "ask", "firmery/test-app") == {
        "status": "granted",
        "repo": "firmery/test-app",
        "access": "write",
        "clone_url": "https://github.com/firmery/test-app.git",
    }

    assert runtime.owner.revoke_github(OWNER, "firmery/test-app") == {
        "resource": "firmery/test-app",
        "status": "revoked",
        "address": None,
        "revoked": ["founder", "founder/analyst"],
        "withdrawn": [],
    }
    asked = []
    for invocation in (founder, analyst):
        answer = ask(runtime.tools(invocation.token), "ask-after-revoke", "firmery/test-app")
        assert answer["cause"] == "github_owner_revoked"
        asked.append(answer["request_id"])
        with pytest.raises(GitHubRefused):
            runtime.github.issue(invocation.token, "firmery/test-app")

    assert runtime.owner.grant_github(OWNER, "firmery/test-app") == {
        "resource": "firmery/test-app",
        "status": "granted",
        "address": None,
        "cleared": ["every role"],
        "granted": None,
        "answered": asked,
    }
    # The rows an owner blocked apply again; nobody has to ask a second time.
    for invocation in (founder, analyst):
        assert runtime.github.issue(invocation.token, "firmery/test-app")["access"] == "write"


@pytest.mark.parametrize(("revoked", "named"), [(None, "every role"), ("founder", "founder")])
def test_granting_one_role_never_lifts_a_wider_revocation(tmp_path, fake, key, clock, revoked,
                                                           named):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    analyst = hire(runtime, founder, "analyst")
    create(runtime.tools(founder.token), "make", "app", "App")
    ask(runtime.tools(analyst.token), "ask", "firmery/test-app")
    runtime.owner.revoke_github(OWNER, "firmery/test-app", revoked)
    pending = ask(runtime.tools(analyst.token), "again", "firmery/test-app")
    assert pending["cause"] == "github_owner_revoked"

    for decide in (
        lambda: runtime.requests.grant(OWNER, pending["request_id"]),
        lambda: runtime.owner.grant_github(OWNER, "firmery/test-app", "founder/analyst"),
    ):
        with pytest.raises(FirmError, match=f"revoked for {named}, which covers founder/analyst"):
            decide()
    for invocation in (founder, analyst):
        with pytest.raises(GitHubRefused):
            runtime.github.issue(invocation.token, "firmery/test-app")
    assert runtime.requests.listing(OWNER)["pending"][0]["request_id"] == pending["request_id"]


def test_revoking_one_address_leaves_another_role_working(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    analyst = hire(runtime, founder, "analyst")
    create(runtime.tools(founder.token), "make", "app", "App")
    ask(runtime.tools(analyst.token), "ask", "firmery/test-app")

    assert runtime.owner.revoke_github(OWNER, "firmery/test-app", "founder/analyst")["revoked"] == [
        "founder/analyst"
    ]
    assert runtime.github.issue(founder.token, "firmery/test-app")["access"] == "write"
    with pytest.raises(GitHubRefused) as refusal:
        runtime.github.issue(analyst.token, "firmery/test-app")
    assert refusal.value.code == "github_owner_revoked"


def test_a_configured_grant_outlives_a_revocation_of_requested_access(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock, grants=[], auto_create=True)
    create(runtime.tools(founder.token), "make", "app", "App")
    runtime.owner.revoke_github(OWNER, "firmery/test-app")
    with runtime.store.read() as db:
        config = runtime.accounts.config(db)
    config["github"]["grants"] = [
        {"repo": "firmery/test-app", "address": "founder", "access": "read"}
    ]
    runtime.owner.configure(OWNER, config)
    assert runtime.github.issue(founder.token, "firmery/test-app")["access"] == "read"


def test_a_firm_without_github_refuses_requests(tmp_path, clock):
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
    result = ask(runtime.tools(founder.token), "ask", "firmery/app")
    assert result["reason"] == "github_not_configured"


def test_token_route_serves_only_running_activations_and_never_caches(tmp_path, fake, key, clock):
    import hashlib

    from starlette.testclient import TestClient

    from firm_runtime.mcp_http import build_http_app

    runtime, founder = firm(tmp_path, clock)
    summary_token = "summary-start-token"
    with runtime.store.transaction() as db:
        db.execute(
            "INSERT INTO summary_runs(run_id,started_at,state,input_bytes,account_id,provider,"
            "deadline,token_hash,provider_started_at) "
            "VALUES('s-test',?,'running',1,'a','claude',?,?,?)",
            (clock(), clock() + 60, hashlib.sha256(summary_token.encode()).hexdigest(), clock()),
        )
    app = build_http_app(runtime, allowed_hosts=["firm.test"], allowed_origins=["http://firm.test"])

    def get(token, query=""):
        return client.get(f"/github/token{query}", headers={"Authorization": f"Bearer {token}"})

    with TestClient(app, base_url="http://firm.test") as client:
        granted = get(founder.token, "?repo=firmery/firm-github-check")
        assert granted.status_code == 200 and granted.json()["access"] == "write"
        assert granted.headers["cache-control"] == "no-store"
        refused = get(founder.token, "?repo=firmery/other")
        assert refused.status_code == 403
        assert refused.json() == {"error": "github_not_granted", "repo": "firmery/other"}
        assert get(summary_token).status_code == 401
        assert client.get("/github/token").status_code == 401
        fake.server.shutdown()
        fake.server.server_close()
        clock.now[0] += 3600
        unavailable = get(founder.token, "?repo=firmery/firm-github-check")
        assert unavailable.status_code == 503 and unavailable.json() == {
            "error": "github_unavailable"
        }
        runtime.finish(founder.activation_id)
        assert get(founder.token, "?repo=firmery/firm-github-check").status_code == 401


def test_launch_settings_name_the_role_and_the_bot(tmp_path, fake, key, clock):
    runtime, founder = firm(tmp_path, clock)
    env, secrets, secret_env = runtime.github.launch_settings(
        founder.employee_id, "http://h/github/token"
    )
    assert env == {
        "FIRM_GITHUB_URL": "http://h/github/token",
        "GIT_AUTHOR_NAME": "founder",
        "GIT_AUTHOR_EMAIL": "777+firmery-roles[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "founder",
        "GIT_COMMITTER_EMAIL": "777+firmery-roles[bot]@users.noreply.github.com",
    }
    assert b"git credential" in secrets["github-credential"]
    assert secret_env == {}
