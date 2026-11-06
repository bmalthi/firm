"""GitHub App access for roles: the host keeps the App key, guests get one-hour tokens."""

import html
import http.client
import json
import logging
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from . import analysis_history
from .accounts import CREDENTIALS_DIR
from .errors import FirmError
from .prompt_resources import render
from .requests import AskOwner, GrantNow, Unrecorded, record_github
from .store import OWNER

LOGGER = logging.getLogger(__name__)
API = "https://api.github.com"
WEB = "https://github.com"
REFRESH_MARGIN = 600
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
REGISTRATION_TIMEOUT = 600
INSTALLATION_POLL = 2
PERMISSIONS = {
    access: {"contents": access, "pull_requests": access, "issues": access}
    for access in ("read", "write")
}
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
REPOSITORY = re.compile(r"[A-Za-z0-9._-]{1,100}")
CREATED_NAME = re.compile(r"[a-z0-9._-]{1,100}")
MAX_PURPOSE = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS github_app (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    app_id INTEGER NOT NULL,
    organization TEXT NOT NULL,
    installation_id INTEGER NOT NULL,
    bot_login TEXT NOT NULL,
    bot_user_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS github_repositories (
    repository TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL REFERENCES roles(employee_id),
    purpose TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS github_grants (
    repository TEXT NOT NULL,
    address TEXT NOT NULL,
    access TEXT NOT NULL CHECK(access IN ('read','write')),
    source TEXT NOT NULL CHECK(source IN ('created','requested','owner')),
    created_at REAL NOT NULL,
    PRIMARY KEY(repository,address)
);
-- An owner's standing refusal. The address covers its subtree; '' is every role.
CREATE TABLE IF NOT EXISTS github_revocations (
    repository TEXT NOT NULL,
    address TEXT NOT NULL,
    revoked_at REAL NOT NULL,
    PRIMARY KEY(repository,address)
);
"""


class GitHubRefused(Exception):
    """The request is not allowed; ``code`` is safe to return to the role."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class GitHubUnavailable(Exception):
    """GitHub or the App key could not be used; the message never carries a token."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _Fresh(Exception):
    pass


def validate_github(config):
    """Check the owner's ``[github]`` table; grants name an address and cover its subtree."""
    github = config.get("github")
    if github is None:
        return
    from .lifecycle import _require_address

    if not isinstance(github, dict) or set(github) - {
        "organization",
        "auto_create",
        "max_repositories",
        "grants",
    }:
        raise FirmError("github has unsupported settings")
    organization = github.get("organization")
    if not isinstance(organization, str) or not LOGIN.fullmatch(organization):
        raise FirmError("github.organization must be a GitHub organization name")
    # A firm that names an organization wants its roles working in it, and the only
    # way a role gets a repository is to ask the runtime for one, which creates it
    # private and refuses anything else. Set it false to withhold that.
    if type(github.setdefault("auto_create", True)) is not bool:
        raise FirmError("github.auto_create must be true or false")
    limit = github.setdefault("max_repositories", 20)
    if type(limit) is not int or limit <= 0:
        raise FirmError("github.max_repositories must be a positive integer")
    grants = github.setdefault("grants", [])
    if not isinstance(grants, list):
        raise FirmError("github.grants must be a list")
    for grant in grants:
        if not isinstance(grant, dict) or set(grant) != {"repo", "address", "access"}:
            raise FirmError("each github grant needs exactly repo, address and access")
        owner, _, name = str(grant["repo"]).partition("/")
        if owner.casefold() != organization.casefold() or not REPOSITORY.fullmatch(name):
            raise FirmError(f"github grant repo must be {organization}/<repository>")
        if not isinstance(grant["address"], str):
            raise FirmError("github grant address must be a role address")
        _require_address(grant["address"])
        if grant["access"] not in PERMISSIONS:
            raise FirmError("github grant access must be read or write")


def covers(scope, address):
    """A scope covers one address: itself and everything under it, or every role when ``''``."""
    return scope == "" or address == scope or address.startswith(scope + "/")


def grants_for(github, address, requested=()):
    """Repositories (lower-cased) this address may use, with the widest access granted.

    Configured grants cover the address's subtree; ``requested`` rows are the address's own.
    """
    granted = {}
    covered = [
        (grant["repo"], grant["access"])
        for grant in github["grants"]
        if covers(grant["address"], address)
    ]
    for repo, access in [*covered, *requested]:
        if granted.get(repo.casefold()) != "write":
            granted[repo.casefold()] = access
    return granted


def app_files(organization):
    """The host-wide App key and its ID and slug for one organization."""
    base = CREDENTIALS_DIR.expanduser() / f"github-{organization.lower()}"
    return base.with_suffix(".pem"), base.with_suffix(".json")


def saved_app(organization):
    key, description = app_files(organization)
    if not key.is_file() or not description.is_file():
        return None
    try:
        app = json.loads(description.read_text())
        if type(app["app_id"]) is not int or not isinstance(app["slug"], str):
            raise TypeError
    except (OSError, ValueError, KeyError, TypeError):
        raise FirmError(f"{description} is not a saved GitHub App; delete it and the key") from None
    return {"app_id": app["app_id"], "slug": app["slug"]}


def read_key(path):
    """The App key must be an owner-only regular file."""
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise GitHubUnavailable("GitHub App private key is not readable") from None
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
        ):
            raise GitHubUnavailable("GitHub App private key must be a private file")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            return handle.read(MAX_KEY_BYTES + 1)
    finally:
        os.close(descriptor)


def _write_private(path, raw):
    """Atomically write mode 0600 in a 0700 directory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".firm-key-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_app(organization, app_id, slug, pem):
    """Save a registered App; the key file is written first, so the description marks it saved."""
    raw = pem.encode() if isinstance(pem, str) else pem
    try:
        if len(raw) > MAX_KEY_BYTES or not isinstance(
            load_pem_private_key(raw, password=None), RSAPrivateKey
        ):
            raise ValueError
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise FirmError("GitHub returned an App key that is not an RSA key in PEM format") from None
    key, description = app_files(organization)
    _write_private(key, raw)
    _write_private(description, json.dumps({"app_id": app_id, "slug": slug}).encode())


def _app_jwt(organization, now):
    app = saved_app(organization)
    if app is None:
        raise GitHubUnavailable(f"no GitHub App is saved for {organization}")
    # GitHub accepts at most ten minutes of lifetime; backdating absorbs clock drift.
    payload = {"iat": int(now) - 60, "exp": int(now) + 540, "iss": str(app["app_id"])}
    key = read_key(app_files(organization)[0])
    try:
        return jwt.encode(payload, key, algorithm="RS256")
    except (ValueError, TypeError, jwt.PyJWTError, UnsupportedAlgorithm):
        raise GitHubUnavailable("GitHub App private key is not a usable RSA key") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _call(method, path, bearer=None, body=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "firm-runtime",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(
        API + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers=headers,
    )
    # Paths name only public identifiers once a manifest code is redacted.
    shown = "/app-manifests/<code>/conversions" if path.startswith("/app-manifests/") else path
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise GitHubUnavailable(
            f"GitHub answered {method} {shown} with {exc.code}", exc.code
        ) from None
    except (OSError, ValueError, http.client.HTTPException):
        raise GitHubUnavailable(f"GitHub {method} {shown} failed") from None


def _mint(organization, installation_id, repositories, permissions, now):
    body = {"permissions": permissions}
    if repositories is not None:
        body["repositories"] = [repo.split("/", 1)[1] for repo in repositories]
    result = _call(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        _app_jwt(organization, now),
        body,
    )
    try:
        token = result["token"]
        expires_at = datetime.fromisoformat(result["expires_at"]).timestamp()
        if not isinstance(token, str) or not token:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise GitHubUnavailable("GitHub returned an unusable installation token") from None
    return token, expires_at


def _revoke(token):
    try:
        _call("DELETE", "/installation/token", token)
    except GitHubUnavailable:
        LOGGER.warning("Could not revoke a GitHub installation token; it expires within an hour")


def manifest(organization, redirect_url):
    return {
        "name": f"{organization}-roles",
        "url": f"{WEB}/{organization}",
        "hook_attributes": {"url": f"{WEB}/{organization}", "active": False},
        "redirect_url": redirect_url,
        "public": False,
        "default_events": [],
        "default_permissions": {
            "administration": "write",
            "contents": "write",
            "pull_requests": "write",
            "issues": "write",
            "metadata": "read",
        },
    }


def ensure_app(config, interactive):
    """Register the organization's App from a manifest when this host has none saved."""
    settings = config.get("github")
    organization = settings.get("organization") if isinstance(settings, dict) else None
    if not isinstance(organization, str) or not LOGIN.fullmatch(organization):
        return
    if saved_app(organization) is not None:
        return
    if not interactive:
        raise FirmError(
            f"no GitHub App is saved for {organization}; run this command in a terminal to "
            "register one"
        )
    register(organization)


def register(
    organization, *, open_browser=webbrowser.open, clock=time.monotonic, sleep=time.sleep
):
    """GitHub's manifest flow: two owner clicks in the browser, nothing copied by hand."""
    state = secrets.token_urlsafe(32)
    received = {}
    arrived = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _page(self, status, text):
            raw = f"<!doctype html><meta charset=utf-8><title>Firm</title>{text}".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/":
                action = f"{WEB}/organizations/{organization}/settings/apps/new?state={state}"
                body = html.escape(json.dumps(manifest(organization, callback)), quote=True)
                return self._page(
                    200,
                    f'<body onload="document.forms[0].submit()"><form method="post" '
                    f'action="{html.escape(action, quote=True)}">'
                    f'<input type="hidden" name="manifest" value="{body}">'
                    '<button type="submit">Register the GitHub App</button></form>',
                )
            query = parse_qs(url.query)
            code = query.get("code", [""])[0]
            if (
                url.path != "/callback"
                or not secrets.compare_digest(query.get("state", [""])[0], state)
                or not re.fullmatch(r"[A-Za-z0-9]{1,100}", code)
            ):
                return self._page(400, "<p>This is not the GitHub App registration.</p>")
            received.setdefault("code", code)
            arrived.set()
            self._page(200, "<p>GitHub App created. Return to the terminal.</p>")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    callback = f"http://127.0.0.1:{server.server_address[1]}/callback"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        print(
            f"Registering a GitHub App for {organization}. In the browser, choose "
            "Create GitHub App, then install it on the organization.",
            file=sys.stderr,
        )
        open_browser(f"http://127.0.0.1:{server.server_address[1]}/")
        if not arrived.wait(REGISTRATION_TIMEOUT):
            raise FirmError("the GitHub App was not created within ten minutes")
    finally:
        server.shutdown()
        server.server_close()
    try:
        created = _call("POST", f"/app-manifests/{received['code']}/conversions")
        app_id, slug, pem = created["id"], created["slug"], created["pem"]
        if type(app_id) is not int or not isinstance(slug, str) or not isinstance(pem, str):
            raise TypeError
    except GitHubUnavailable as exc:
        raise FirmError(f"GitHub did not create the App: {exc}") from None
    except (KeyError, TypeError):
        raise FirmError("GitHub returned an unexpected App description") from None
    save_app(organization, app_id, slug, pem)
    install = f"{WEB}/apps/{slug}/installations/new"
    print(f"Install the App on {organization}: {install}", file=sys.stderr)
    open_browser(install)
    deadline = clock() + REGISTRATION_TIMEOUT
    while True:
        try:
            _call("GET", f"/orgs/{organization}/installation", _app_jwt(organization, time.time()))
            return
        except GitHubUnavailable:
            if clock() >= deadline:
                raise FirmError(
                    f"the GitHub App is not installed on {organization}; install it at "
                    f"{install} and run the command again"
                ) from None
            sleep(INSTALLATION_POLL)


def connect(github, now=None):
    """Resolve the App's installation and bot identity and prove every grant is reachable.

    Runs when the owner initializes or configures a firm, so a missing installation or an
    ungranted repository is reported then rather than to a role mid-run.
    """
    now = time.time() if now is None else now
    organization = github["organization"]
    app = saved_app(organization)
    if app is None:
        raise FirmError(
            f"no GitHub App is saved for {organization}; run firm init or firm configure in "
            "a terminal to register one"
        )
    try:
        token = _app_jwt(organization, now)
        try:
            installation = _call("GET", f"/orgs/{organization}/installation", token)["id"]
        except GitHubUnavailable:
            raise FirmError(
                f"the GitHub App is not installed on {organization}; install it at "
                f"{WEB}/apps/{app['slug']}/installations/new"
            ) from None
        bot_login = f"{app['slug']}[bot]"
        # A public endpoint; GitHub accepts an App JWT only under /app.
        bot = _call("GET", f"/users/{quote(bot_login)}")
        if type(installation) is not int or type(bot["id"]) is not int:
            raise TypeError
    except (KeyError, TypeError):
        raise FirmError("GitHub returned an unexpected App description") from None
    except GitHubUnavailable as exc:
        raise FirmError(f"GitHub App is not usable: {exc}") from None
    repositories = sorted({grant["repo"].casefold() for grant in github["grants"]})
    if repositories:
        widest = "write" if any(g["access"] == "write" for g in github["grants"]) else "read"
        try:
            probe, _ = _mint(organization, installation, repositories, PERMISSIONS[widest], now)
        except GitHubUnavailable:
            raise FirmError(
                "the GitHub App installation cannot reach every granted repository with "
                f"{widest} access; add them under the installation's repository access and "
                "check the App's contents, pull request and issue permissions"
            ) from None
        _revoke(probe)
    return {
        "app_id": app["app_id"],
        "organization": organization,
        "installation_id": installation,
        "bot_login": bot_login,
        "bot_user_id": bot["id"],
    }


def record_app(db, app):
    db.execute("DELETE FROM github_app")
    if app is not None:
        db.execute(
            "INSERT INTO github_app VALUES(1,?,?,?,?,?)",
            (
                app["app_id"],
                app["organization"],
                app["installation_id"],
                app["bot_login"],
                app["bot_user_id"],
            ),
        )


def revoked_for(db, address):
    """Repositories an owner revoked for this address; only the owner can grant them again."""
    return {
        row["repository"]
        for row in db.execute("SELECT repository,address FROM github_revocations")
        if covers(row["address"], address)
    }


def _requested(db, address):
    """This address's own grants, minus the repositories an owner has revoked."""
    revoked = revoked_for(db, address)
    return [
        row
        for row in db.execute(
            "SELECT repository,access FROM github_grants WHERE address=?", (address,)
        )
        if row["repository"] not in revoked
    ]


def _fresh():
    raise _Fresh


class GitHubAccess:
    """Issue, cache and revoke installation tokens; create and grant requested repositories.

    Also the ``github_repository`` owner request kind: a role asks for a repository of the
    organization, or for one to be created when the firm's policy would refuse it.
    """

    name = "github_repository"

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.lock = threading.Lock()
        # Serializes role requests so a retry or a second role never races the repository cap.
        self.requesting = threading.Lock()
        # activation_id -> [(repositories, access, token, expires_at)], oldest first. Replaced
        # tokens stay listed so settlement revokes them too.
        self.tokens = {}

    def _state(self, db):
        github = json.loads(self.store.firm(db)["config_json"]).get("github")
        app = db.execute("SELECT * FROM github_app").fetchone()
        return (github, dict(app)) if github and app else (None, None)

    def launch_settings(self, employee_id, token_url):
        """Environment, secrets and secret variables for a run, or None without ``[github]``."""
        with self.store.read() as db:
            github, app = self._state(db)
            address = self.store.role(db, employee_id)["address"]
        if github is None:
            return None
        from . import github_credential

        email = f"{app['bot_user_id']}+{app['bot_login']}@users.noreply.github.com"
        env = {"FIRM_GITHUB_URL": token_url}
        for kind in ("AUTHOR", "COMMITTER"):
            env[f"GIT_{kind}_NAME"], env[f"GIT_{kind}_EMAIL"] = address, email
        # The helper reads the activation token from the run's credentials
        # directory, so GitHub needs no secret of its own in the environment.
        return env, {"github-credential": Path(github_credential.__file__).read_bytes()}, {}

    def issue(self, token, repo=None):
        """A token for one granted repository, or read access to all of them without ``repo``."""
        with self.store.read() as db:
            activation = self.runtime._authenticate(db, token)
            github, app = self._state(db)
            address = self.store.role(db, activation["employee_id"])["address"]
            requested = _requested(db, address)
            revoked = revoked_for(db, address)
        activation_id, employee_id = activation["activation_id"], activation["employee_id"]
        try:
            if github is None:
                raise GitHubRefused("github_not_configured")
            granted = grants_for(github, address, requested)
            if repo is None:
                repositories, access = tuple(sorted(granted)), "read"
            else:
                owner, _, name = repo.partition("/")
                if not LOGIN.fullmatch(owner) or not REPOSITORY.fullmatch(name):
                    raise GitHubRefused("github_repo_invalid")
                repositories, access = (repo.casefold(),), granted.get(repo.casefold())
            if not repositories or access is None:
                if repo is not None and repo.casefold() in revoked:
                    raise GitHubRefused("github_owner_revoked")
                raise GitHubRefused("github_not_granted")
        except GitHubRefused as refusal:
            self._event("github.token_refused", employee_id, activation_id, error=refusal.code)
            raise
        now = self.store.clock()
        with self.lock:
            for cached in reversed(self.tokens.get(activation_id, [])):
                if cached[:2] == (repositories, access) and cached[3] - now > REFRESH_MARGIN:
                    return {"token": cached[2], "expires_at": cached[3], "access": access}
        # Minted outside the lock so a slow GitHub stalls neither other roles nor the
        # dispatcher's housekeeping. An older token for the same grant is not revoked here:
        # a push may still be using it. It expires or is revoked at settlement.
        issued, expires_at = _mint(
            github["organization"], app["installation_id"], repositories, PERMISSIONS[access], now
        )
        try:
            with self.store.read() as db:
                self.runtime._authenticate(db, token)
                current_github, current_app = self._state(db)
                current_address = self.store.role(db, employee_id)["address"]
                current_grants = (
                    grants_for(current_github, current_address, _requested(db, current_address))
                    if current_github else {}
                )
                allowed = {"read", "write"} if access == "read" else {"write"}
                if current_app != app or any(
                    current_grants.get(repository) not in allowed for repository in repositories
                ):
                    raise GitHubRefused("github_not_granted")
                # Register the token before releasing the read snapshot. A later
                # settlement sees it even if the activation or grant ends next.
                with self.lock:
                    self.tokens.setdefault(activation_id, []).append(
                        (repositories, access, issued, expires_at)
                    )
        except (FirmError, GitHubRefused) as exc:
            # An owner can revoke/downgrade a grant while GitHub is answering, too.
            _revoke(issued)
            if isinstance(exc, FirmError):
                self._forget(activation_id)
            else:
                self._event("github.token_refused", employee_id, activation_id, error=exc.code)
            raise
        self._event(
            "github.token_minted",
            employee_id,
            activation_id,
            repositories=list(repositories),
            access=access,
            expires_at=expires_at,
        )
        return {"token": issued, "expires_at": expires_at, "access": access}

    def create_repository(self, actor, request_id, name, purpose):
        """Create a private repository for the role and grant it write; retries replay.

        Policy the owner can lift (creation off, the cap reached) opens an owner request.
        """
        employee_id, activation_id = actor["employee_id"], actor["activation_id"]
        payload = {"name": name, "purpose": purpose}
        with self.requesting:
            try:
                with self.store.transaction() as db:
                    return self.store.request(
                        db, employee_id, request_id, "create_repository", payload, _fresh
                    )
            except _Fresh:
                pass
            with self.store.read() as db:
                github, app = self._state(db)
                firm_id = self.store.firm(db)["firm_id"]
            try:
                if github is None:
                    raise GitHubRefused("github_not_configured")
                repository = self._create(github, app, firm_id, name, purpose)
            except (GitHubRefused, GitHubUnavailable) as exc:
                code = exc.code if isinstance(exc, GitHubRefused) else "github_unavailable"
                if code in {"github_auto_create_off", "github_limit_reached"}:
                    # The name was validated before the policy refused it.
                    wanted = f"{github['organization']}/{firm_id}-{name}".casefold()
                    with self.store.transaction() as db:
                        return self.store.request(
                            db, employee_id, request_id, "create_repository", payload,
                            lambda: self.runtime.requests.ask_owner(
                                db, actor, self.name, wanted, purpose, code, create=True
                            ),
                        )
                self._event("github.request_refused", employee_id, activation_id, error=code)
                result = {"status": "refused", "reason": code}
                if code == "github_unavailable":
                    return result
                with self.store.transaction() as db:
                    return self.store.request(
                        db, employee_id, request_id, "create_repository", payload,
                        lambda: result,
                    )
            with self.store.transaction() as db:
                return self.store.request(
                    db, employee_id, request_id, "create_repository", payload,
                    lambda: self._grant(db, actor, repository, purpose, created=True),
                )

    def _create(self, github, app, firm_id, name, purpose):
        full = f"{firm_id}-{name}"
        if not isinstance(name, str) or not name or not CREATED_NAME.fullmatch(full):
            raise FirmError(
                "name must use lower-case letters, digits, '-', '_' and '.', and the "
                f"repository name {firm_id}-<name> at most 100 characters"
            )
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > MAX_PURPOSE:
            raise FirmError(f"purpose must contain 1–{MAX_PURPOSE} characters")
        if not github["auto_create"]:
            raise GitHubRefused("github_auto_create_off")
        organization = github["organization"]
        repository = f"{organization}/{full}".casefold()
        with self.store.read() as db:
            if db.execute(
                "SELECT 1 FROM github_repositories WHERE repository=?", (repository,)
            ).fetchone():
                raise GitHubRefused("github_name_taken")
            created = db.execute("SELECT COUNT(*) FROM github_repositories").fetchone()[0]
        if created >= github["max_repositories"]:
            raise GitHubRefused("github_limit_reached")
        return self._make(github, app, full, purpose)

    def _make(self, github, app, full, purpose):
        """Create the private repository on GitHub; no policy is checked here."""
        organization = github["organization"]
        repository = f"{organization}/{full}".casefold()
        # The only token ever minted with Administration; it cannot be narrowed to a
        # repository that does not exist yet, so it lives only for these calls.
        token, _ = _mint(
            organization, app["installation_id"], None, {"administration": "write"},
            self.store.clock(),
        )
        try:
            try:
                _call(
                    "POST",
                    f"/orgs/{organization}/repos",
                    token,
                    {
                        "name": full,
                        "description": purpose,
                        "private": True,
                        "visibility": "private",
                        "auto_init": True,
                    },
                )
            except GitHubUnavailable as exc:
                if exc.status == 422:
                    raise GitHubRefused("github_name_taken") from None
                raise
            path = f"/repos/{organization}/{full}"
            if (_call("GET", path, token) or {}).get("private") is not True:
                try:
                    _call("DELETE", path, token)
                    LOGGER.error("Deleted %s: GitHub created it public", repository)
                except GitHubUnavailable:
                    LOGGER.error("%s is public and could not be deleted; delete it", repository)
                raise GitHubUnavailable("GitHub created a public repository")
        finally:
            _revoke(token)
        return repository

    def _give(self, db, employee_id, activation_id, repository, source, purpose=None):
        """Write access for a role at its address; ``purpose`` records a repository just created."""
        now = self.store.clock()
        address = self.store.role(db, employee_id)["address"]
        if purpose is not None:
            db.execute(
                "INSERT INTO github_repositories VALUES(?,?,?,?)",
                (repository, employee_id, purpose, now),
            )
            analysis_history.record(
                db, now, "github.repository_created", employee_id, activation_id,
                repository=repository,
            )
        db.execute(
            "INSERT INTO github_grants VALUES(?,?,'write',?,?) "
            "ON CONFLICT(repository,address) DO UPDATE SET access='write'",
            (repository, address, source, now),
        )
        analysis_history.record(
            db, now, "github.access_granted", employee_id, activation_id,
            repository=repository, access="write", source=source,
        )
        return address

    def _grant(self, db, actor, repository, purpose, *, created):
        """The role's own immediate grant, which the owner reads in the list of requests."""
        employee_id = actor["employee_id"]
        address = self._give(
            db, employee_id, actor["activation_id"], repository,
            "created" if created else "requested", purpose if created else None,
        )
        if created:
            self.runtime.messaging.notice(
                db,
                [OWNER],
                render(
                    "notices",
                    "github_repository_created",
                    address=address,
                    repository=repository,
                    purpose=purpose,
                ),
                subject=render("notices", "github_repository_created_subject"),
            )
        record_github(db, self.store.clock(), actor, repository, purpose)
        return {
            "status": "granted",
            "repo": repository,
            "access": "write",
            "clone_url": f"{WEB}/{repository}.git",
        }

    # ------------------------------------------------------------------ owner requests

    def _refused(self, actor, code):
        self._event("github.request_refused", actor["employee_id"], actor["activation_id"],
                    error=code)
        return {"status": "refused", "reason": code}

    def _reachable(self, github, app, repository):
        """Whether the installation can mint write access to ``repository``; the probe is revoked.

        GitHub answers 404 or 422 for a repository the installation does not cover; any other
        failure raises ``GitHubUnavailable``.
        """
        try:
            token, _ = _mint(
                github["organization"], app["installation_id"], [repository],
                PERMISSIONS["write"], self.store.clock(),
            )
        except GitHubUnavailable as exc:
            if exc.status in {404, 422}:
                return False
            raise
        _revoke(token)
        return True

    def ask(self, actor, resource, purpose):
        """Write access to a repository of the organization: at once when the firm made it."""
        owner, _, short = resource.partition("/") if isinstance(resource, str) else ("", "", "")
        if not LOGIN.fullmatch(owner) or not REPOSITORY.fullmatch(short):
            return self._refused(actor, "access_resource_invalid")
        with self.store.read() as db:
            github, app = self._state(db)
        if github is None:
            return self._refused(actor, "github_not_configured")
        if owner.casefold() != github["organization"].casefold():
            return self._refused(actor, "access_resource_invalid")
        repository = resource.casefold()
        with self.store.read() as db:
            address = self.store.role(db, actor["employee_id"])["address"]
            if grants_for(github, address).get(repository) == "write":
                # firm.toml already gives this address write; revocations do not bind it.
                return {"status": "granted", "repo": repository, "access": "write",
                        "clone_url": f"{WEB}/{repository}.git"}
            if repository in revoked_for(db, address):
                return AskOwner(repository, "github_owner_revoked")
            # A repository the firm created, or one the owner already gave this address.
            known = db.execute(
                "SELECT 1 FROM github_repositories WHERE repository=? UNION ALL "
                "SELECT 1 FROM github_grants WHERE repository=? AND address=?",
                (repository, repository, address),
            ).fetchone()
        if known:
            return GrantNow(lambda db: self._grant(db, actor, repository, purpose, created=False))
        # The owner is only asked about a repository the App can reach.
        try:
            reachable = self._reachable(github, app, repository)
        except GitHubUnavailable:
            self._refused(actor, "github_unavailable")
            return Unrecorded({"status": "refused", "reason": "github_unavailable"})
        if not reachable:
            return self._refused(actor, "access_resource_invalid")
        return AskOwner(repository, "github_not_this_firm")

    def grant(self, row):
        """The owner grants a request: create the repository, or give the role access to it."""
        payload = json.loads(row["payload_json"])
        repository = payload["resource"]
        with self.store.read() as db:
            made = db.execute(
                "SELECT 1 FROM github_repositories WHERE repository=?", (repository,)
            ).fetchone()
            address = self.store.role(db, row["employee_id"])["address"]
        if payload.get("create") and not made:
            return self._grant_creation(row, repository, payload["purpose"])
        return self.allow(repository, address, request_id=row["request_id"])

    def _grant_creation(self, row, repository, purpose):
        # The owner decided, so neither auto_create nor the cap applies. Held so a role's own
        # create_repository in this process does not race the name.
        with self.requesting:
            with self.store.read() as db:
                github, app = self._state(db)
            if github is None:
                raise FirmError("this firm has no GitHub access")
            try:
                self._make(github, app, repository.partition("/")[2], purpose)
            except GitHubRefused:
                raise FirmError(f"GitHub already has {repository}") from None
            except GitHubUnavailable as exc:
                raise FirmError(f"GitHub could not create {repository}: {exc}") from None
            with self.store.transaction() as db:
                status = db.execute(
                    "SELECT status FROM owner_requests WHERE request_id=?", (row["request_id"],)
                ).fetchone()["status"]
                if status == "pending":
                    self._give(db, row["employee_id"], None, repository, "created", purpose)
                    answered = self.runtime.requests.answer(
                        db, [row], "granted", decided_by="owner"
                    )
                else:
                    # The role was retired meanwhile; the firm still made the repository, so
                    # it is recorded as the firm's and any role can ask for it.
                    now = self.store.clock()
                    db.execute(
                        "INSERT INTO github_repositories VALUES(?,?,?,?)",
                        (repository, row["employee_id"], purpose, now),
                    )
                    analysis_history.record(
                        db, now, "github.repository_created", row["employee_id"], None,
                        repository=repository,
                    )
            if status != "pending":
                raise FirmError(
                    f"created {repository}, but request {row['request_id']} is already "
                    f"{status}; no role was given access to it"
                )
        return {"resource": repository, "status": "created", "answered": answered}

    def deny(self, row, note):
        """The owner declines one request; the same repository is refused to later askers."""
        with self.store.transaction() as db:
            self.runtime.requests.pending(db, row["request_id"])
            answered = self.runtime.requests.answer(db, [row], "denied", note, decided_by="owner")
        resource = json.loads(row["payload_json"])["resource"]
        return {"request_id": row["request_id"], "resource": resource, "status": "denied",
                "answered": answered}

    def _in_organization(self, repo):
        """The case-folded repository and the App state; the repository must be the firm's org."""
        with self.store.read() as db:
            github, app = self._state(db)
        if github is None:
            raise FirmError("this firm has no GitHub access")
        owner, _, short = repo.partition("/")
        if owner.casefold() != github["organization"].casefold() or not REPOSITORY.fullmatch(short):
            raise FirmError(f"repo must be {github['organization']}/<repository>")
        return repo.casefold(), github, app

    def allow(self, repo, scope, *, request_id=None):
        """Lift ``scope``'s revocation, give a named address write, answer who that helps.

        Any repository of the organization the App can reach; ``scope`` is an address and its
        subtree, or ``''`` for every role, which lifts every revocation. A wider revocation
        covering a named address is refused, never lifted for everyone it covers. With
        ``request_id``, that request must still be open.
        """
        repository, github, app = self._in_organization(repo)
        try:
            if not self._reachable(github, app, repository):
                raise FirmError(
                    f"the GitHub App installation cannot reach {repository} with write access; "
                    "add it under the installation's repository access"
                )
        except GitHubUnavailable as exc:
            raise FirmError(f"GitHub could not be asked about {repository}: {exc}") from None
        now = self.store.clock()
        with self.store.transaction() as db:
            if request_id is not None:
                self.runtime.requests.pending(db, request_id)
            revoked = [
                row[0]
                for row in db.execute(
                    "SELECT address FROM github_revocations WHERE repository=? ORDER BY address",
                    (repository,),
                )
            ]
            # Lifting a wider revocation for one address would lift it for everyone it covers.
            wider = [row for row in revoked if scope and row != scope and covers(row, scope)]
            if wider:
                holders = ", ".join(row or "every role" for row in wider)
                raise FirmError(
                    f"{repository} is revoked for {holders}, "
                    f"which covers {scope}; lift that first with firm github grant {repository}, "
                    "naming its address, or none for every role"
                )
            cleared = [row for row in revoked if scope == "" or row == scope]
            db.executemany(
                "DELETE FROM github_revocations WHERE repository=? AND address=?",
                [(repository, row) for row in cleared],
            )
            if scope:
                db.execute(
                    "INSERT INTO github_grants VALUES(?,?,'write','owner',?) "
                    "ON CONFLICT(repository,address) DO UPDATE SET access='write',source='owner'",
                    (repository, scope, now),
                )
            # Every open request whose role now holds the access it asked for.
            satisfied = [
                row
                for row in self._open(db, repository)
                if db.execute(
                    "SELECT 1 FROM github_grants WHERE repository=? AND address=?",
                    (repository, row["address"]),
                ).fetchone()
                and repository not in revoked_for(db, row["address"])
            ]
            answered = self.runtime.requests.answer(db, satisfied, "granted", decided_by="owner")
        return {
            "resource": repository,
            "status": "granted",
            "address": scope or None,
            "cleared": [row or "every role" for row in cleared],
            "granted": "write" if scope else None,
            "answered": answered,
        }

    def _open(self, db, repository):
        """Open requests for ``repository``, each with its role's address."""
        return db.execute(
            "SELECT q.*,r.address FROM owner_requests q JOIN roles r USING(employee_id) "
            "WHERE q.kind=? AND q.status='pending' "
            "AND json_extract(q.payload_json,'$.resource')=? ORDER BY q.rowid",
            (self.name, repository),
        ).fetchall()

    def revoke(self, repo, scope):
        """Refuse a repository to ``scope`` until the owner grants it again.

        Requested grants stay in the table but stop applying, so nothing hands the access
        back: the running host revokes their tokens on its next tick and a role asking for
        the repository again is asked of the owner. Open requests from the scope are withdrawn.
        """
        repository = repo.casefold()
        with self.store.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM github_repositories WHERE repository=? UNION ALL "
                "SELECT 1 FROM github_grants WHERE repository=?",
                (repository, repository),
            ).fetchone():
                raise FirmError(
                    f"{repo} is not a repository this firm created or the owner granted; "
                    "change configured grants in firm.toml and run firm configure"
                )
            blocked = [
                row[0]
                for row in db.execute(
                    "SELECT address FROM github_grants WHERE repository=? ORDER BY address",
                    (repository,),
                )
                if covers(scope, row[0])
            ]
            db.execute(
                "INSERT INTO github_revocations VALUES(?,?,?) "
                "ON CONFLICT(repository,address) DO UPDATE SET revoked_at=excluded.revoked_at",
                (repository, scope, self.store.clock()),
            )
            withdrawn = self.runtime.requests.answer(
                db,
                [row for row in self._open(db, repository) if covers(scope, row["address"])],
                "withdrawn",
                decided_by="owner",
            )
        return {
            "resource": repository,
            "status": "revoked",
            "address": scope or None,
            "revoked": blocked,
            "withdrawn": withdrawn,
        }

    def settle(self):
        """Revoke tokens of ended activations and tokens their role is no longer granted."""
        with self.lock:
            if not self.tokens:
                return
            held = list(self.tokens)
        with self.store.read() as db:
            github, _ = self._state(db)
            rows = {
                row["activation_id"]: (row["address"], _requested(db, row["address"]))
                for row in db.execute(
                    "SELECT a.activation_id,r.address FROM activations a JOIN roles r "
                    "USING(employee_id) WHERE a.state='running' AND r.employment='active' "
                    f"AND a.activation_id IN ({','.join('?' * len(held))})",
                    held,
                )
            }
        now = self.store.clock()
        revoked = []
        with self.lock:
            for activation_id in held:
                address, requested = rows.get(activation_id, (None, ()))
                granted = grants_for(github, address, requested) if github and address else {}
                kept = []
                for entry in self.tokens.get(activation_id, []):
                    repositories, access, token, expires_at = entry
                    allowed = {"read", "write"} if access == "read" else {"write"}
                    if expires_at <= now:
                        continue
                    if all(granted.get(repo) in allowed for repo in repositories):
                        kept.append(entry)
                    else:
                        revoked.append(token)
                if kept:
                    self.tokens[activation_id] = kept
                else:
                    self.tokens.pop(activation_id, None)
        for token in revoked:
            _revoke(token)

    def _forget(self, activation_id):
        with self.lock:
            entries = self.tokens.pop(activation_id, [])
        self._revoke_live(entries)

    def revoke_all(self):
        """Revoke every token this host issued; its endpoint is closing."""
        with self.lock:
            entries = [entry for held in self.tokens.values() for entry in held]
            self.tokens.clear()
        self._revoke_live(entries)

    def _revoke_live(self, entries):
        now = self.store.clock()
        for _, _, token, expires_at in entries:
            if expires_at > now:
                _revoke(token)

    def _event(self, kind, employee_id, activation_id, **payload):
        with self.store.transaction() as db:
            analysis_history.record(
                db, self.store.clock(), kind, employee_id, activation_id, **payload
            )
