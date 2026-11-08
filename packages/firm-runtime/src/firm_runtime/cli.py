"""Owner commands and the local foreground execution host."""

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import tomllib
from datetime import datetime
from importlib import resources
from pathlib import Path

from firm_sandbox.host_paths import read_text_file

from . import export as exports
from . import fleet, github
from .analysis import SECTIONS, AnalysisReader
from .errors import FirmError
from .execution_activity import DEFAULT_IDLE_TIMEOUT
from .onboarding import (
    choose_directory,
    create_firm_files,
    create_missing_files,
    default_data_directory,
    list_firms,
    login_provider,
)
from .provider_contract import ProviderSetupError
from .runtime import Runtime
from .store import OWNER, Store


def _owner_config(path, runtime=None):
    """Read and validate local configuration without external setup side effects."""
    from .lifecycle import validate_config

    config = validate_config(tomllib.loads(read_text_file(path)))
    if runtime is not None:
        with runtime.store.read() as db:
            runtime.owner.validate_reconfiguration(db, config)
    return config


def _sandbox(directory, path):
    """The firm's sandbox, for owner commands that read or change its network."""
    from firm_sandbox import Config, Sandbox

    config = Config.load(path or directory / "sandbox.toml")
    if config.root.resolve() != directory.resolve():
        raise FirmError("sandbox configuration belongs to a different firm directory")
    return Sandbox(config)


def _ask(prompt: str, *, input_fn=None) -> str:
    input_fn = input if input_fn is None else input_fn
    try:
        return input_fn(prompt).strip()
    except EOFError:
        raise FirmError(
            "guided start needs an interactive terminal; use `firm start DIRECTORY` otherwise"
        ) from None


def _choose_provider(*, input_fn=None) -> str:
    print("Choose the model provider for this firm:\n  1) Codex\n  2) Claude Code"
          "\n  3) AGY (experimental subscription)", file=sys.stderr)
    while True:
        choice = _ask("Provider [1/2/3]: ", input_fn=input_fn)
        if choice in {"1", "codex", "Codex", "CODEx"}:
            return "codex"
        if choice in {"2", "claude", "Claude", "Claude Code", "claude code"}:
            return "claude"
        if choice.lower() in {"3", "agy"}:
            return "agy"
        print("Enter 1 for Codex, 2 for Claude Code or 3 for experimental AGY.", file=sys.stderr)


def _choose_task(*, input_fn=None) -> tuple[str, str]:
    print(
        "Choose what the founder should do first:\n  1) Build a weekly planner\n  2) My own task",
        file=sys.stderr,
    )
    while True:
        choice = _ask("First task [1/2]: ", input_fn=input_fn).lower()
        if choice in {"1", "starter", "weekly planner"}:
            try:
                mandate = (
                    resources.files("firm_runtime")
                    .joinpath("resources/starter_mandate.md")
                    .read_text(encoding="utf-8")
                )
            except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
                raise FirmError(
                    f"the packaged weekly planner starter is unavailable: {exc}"
                ) from None
            return "starter", mandate
        if choice in {"2", "custom", "my own task"}:
            task = _ask("Describe the task in one or two sentences: ", input_fn=input_fn)
            if task:
                return "custom", task
            print("Enter a short task, or choose the weekly planner.", file=sys.stderr)
        else:
            print("Enter 1 for the weekly planner or 2 for your own task.", file=sys.stderr)


def _provider_for_directory(directory: Path) -> tuple[str, dict, str]:
    try:
        from .lifecycle import validate_config

        config = validate_config(tomllib.loads(read_text_file(directory / "firm.toml")))
        profile_name = config.get("founder_profile", config.get("default_profile"))
        profile = config["profiles"][profile_name]
        provider = profile["provider"]
        account_id = profile["accounts"][0]
    except (OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
        raise FirmError(
            f"{directory} has incomplete or invalid firm.toml; run `firm check {directory}` "
            "and repair the reported files."
        ) from None
    return provider, profile, account_id


def _show_check(report: dict) -> bool:
    for item in report["checks"]:
        print(f"{item['status']}: {item['check']} — {item['message']}", file=sys.stderr)
        if item.get("action"):
            print(f"  Next: {item['action']}", file=sys.stderr)
    return bool(report["ok"])


def _announce_first_run(provider: str, *, directory: Path | None = None) -> None:
    model = {"codex": "gpt-6-luna", "claude": "Claude Sonnet",
             "agy": "Gemini 3.8 Flash (experimental)"}[provider]
    allocation = "8 GiB of RAM and a 60 GiB maximum virtual disk"
    if directory is not None and (directory / "sandbox.toml").is_file():
        try:
            firm = tomllib.loads(read_text_file(directory / "sandbox.toml"))["firm"]
            memory, disk = firm["memory_gib"], firm["disk_gib"]
            if type(memory) is int and type(disk) is int and memory > 0 and disk > 0:
                allocation = f"{memory} GiB of RAM and a {disk} GiB maximum virtual disk"
            else:
                allocation = "the allocations configured in sandbox.toml"
        except (OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
            allocation = "the allocations configured in sandbox.toml"
    extra = (
        " If your account or workspace does not offer it, change profiles.codex.model in "
        "firm.toml and apply it with `firm configure DIRECTORY --config DIRECTORY/firm.toml`."
        if provider == "codex"
        else " Experimental AGY uses subscription login; credit overages are disabled. "
        "Quota and context are unavailable. Workspace instructions can be read; "
        "implicit native tools are not fully isolated."
        if provider == "agy"
        else " Claude Code sign-in uses its one-year setup token, saved owner-only for this firm."
    )
    print(
        "This firm will create a local Linux VM: Firm will download its guest image, "
        f"allocate {allocation}, then run the selected "
        f"provider ({model}) under your provider account. Submitting the first task starts "
        "the VM; choosing a saved setup to resume starts that task." + extra,
        file=sys.stderr,
    )


def _run_guided_start(args) -> None:
    """Create or resume one firm, then enter the same host start path."""
    if not sys.stdin.isatty():
        raise FirmError(
            "guided start needs an interactive terminal; use `firm start DIRECTORY` otherwise"
        )
    data_root = Path(args.data_dir or default_data_directory()).expanduser().absolute()
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = data_root.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
    ):
        raise FirmError("owner data directory must be controlled by the current user")
    os.chmod(data_root, 0o700)

    firms = list_firms(data_root)
    if firms:
        print(f"Firm data directory: {data_root}", file=sys.stderr)
        print("  1) Create a new firm", file=sys.stderr)
        for index, firm in enumerate(firms, start=2):
            print(f"  {index}) Resume {firm['name']} ({firm['state']})", file=sys.stderr)
        while True:
            choice = _ask("Choose [1]: ", input_fn=input) or "1"
            if choice == "1":
                selected = None
                break
            if choice.isdigit() and 2 <= int(choice) <= len(firms) + 1:
                selected = firms[int(choice) - 2]["path"]
                break
            print("Choose one of the listed numbers.", file=sys.stderr)
    else:
        selected = None

    initialized = False
    runtime = None
    auth_incidents = []
    if selected is None:
        provider = _choose_provider(input_fn=input)
        _announce_first_run(provider)
        task_kind, mandate = _choose_task(input_fn=input)
        selected = choose_directory(data_root, mandate)
        create_firm_files(selected, provider, mandate)
        task_label = "weekly planner" if task_kind == "starter" else mandate.strip().splitlines()[0]
        print(f"Prepared {selected.name} in {selected}.", file=sys.stderr)
    else:
        selected_info = next(firm for firm in firms if firm["path"] == selected)
        if selected_info["state"] == "needs attention":
            reason = selected_info["reason"] or "This Firm cannot be resumed automatically."
            raise FirmError(
                f"{reason} No data was changed; inspect {selected} with `firm check` or "
                "export it with its original runtime."
            )
        initialized = selected_info["state"] == "initialized"
        if initialized:
            provider, _profile, _account_id = _provider_for_directory(selected)
            from .compatibility import validate_firm

            validate_firm(selected)
            runtime = Runtime(Store(selected / "durable" / "firm.sqlite3"))
            auth_incidents = [
                incident
                for incident in runtime.accounts.status()["incidents"]
                if incident["scope"] == "account"
                and incident["dependency"] == _account_id
                and incident["kind"] == "auth"
            ]
            print(
                f"Resuming {selected.name}; Firm will check its provider account and local VM, "
                "then reconnect to its console.",
                file=sys.stderr,
            )
            task_label = "resume existing firm"
        else:
            if (selected / "firm.toml").is_file():
                provider, _profile, account_id = _provider_for_directory(selected)
            else:
                provider = _choose_provider(input_fn=input)
                account_id = f"{provider}-personal"
            _announce_first_run(provider, directory=selected)
            if (selected / "mandate.md").is_file():
                mandate = read_text_file(selected / "mandate.md")
            else:
                _task_kind, mandate = _choose_task(input_fn=input)
            create_missing_files(selected, provider, mandate)
            task_label = mandate.strip().splitlines()[0]
            print(f"Resuming setup for {selected.name}.", file=sys.stderr)

    if selected is None:
        raise FirmError("no firm selected")
    _provider, profile, account_id = _provider_for_directory(selected)
    config = _owner_config(selected / "firm.toml")
    account = config["accounts"][account_id]
    auth_file = Path(account["auth_file"]).expanduser()
    from .providers import provider_adapter

    credential_ok = False
    if auth_file.is_file() and not auth_incidents:
        try:
            provider_adapter(profile, account).validate_credentials()
            credential_ok = True
        except ProviderSetupError:
            print(
                f"The saved {provider.title()} credential needs sign-in again.",
                file=sys.stderr,
            )
    elif auth_incidents:
        print(
            f"The saved {provider.title()} credential was rejected by the provider. "
            "Sign in again to clear this account's authentication hold.",
            file=sys.stderr,
        )

    from .setup_check import check_setup

    preflight = check_setup(
        selected,
        mcp_port=args.mcp_port,
        console_port=args.console_port,
        check_ports=not initialized,
    )
    hard_failures = [
        item
        for item in preflight["checks"]
        if item["status"] == "failure" and not item["check"].startswith("credential:")
    ]
    if hard_failures:
        _show_check(preflight)
        raise FirmError(
            f"setup is saved at {selected}; fix the listed prerequisites and rerun `firm start`."
        )
    if not credential_ok:
        print("Authenticate with your provider before provisioning the Linux VM.", file=sys.stderr)
        login_provider(
            provider,
            account_id,
            state_directory=selected / ".setup-login" / provider,
            target_credentials_path=auth_file,
            force_login=bool(auth_incidents),
        )
        if auth_incidents:
            if runtime is None:
                raise FirmError("cannot clear the provider hold without the initialized firm")
            for incident in auth_incidents:
                runtime.accounts.clear(OWNER, incident["incident_id"])

    report = check_setup(
        selected,
        mcp_port=args.mcp_port,
        console_port=args.console_port,
        check_ports=not initialized,
    )
    if not _show_check(report):
        raise FirmError(
            f"setup is saved at {selected}; fix the listed items and rerun `firm start` to resume."
        )
    if initialized:
        _start_selected(args, selected, guided=True)
        return

    print(f"Starting the first task: {task_label}", file=sys.stderr)
    _initialize_firm(selected, firm_id=selected.name, terminal=sys.stdin.isatty())
    _start_selected(args, selected, guided=True)


def _initialize_firm(
    directory: Path,
    *,
    firm_id: str | None = None,
    mandate_path: Path | None = None,
    config_path: Path | None = None,
    terminal: bool = False,
) -> dict:
    """Create the founder account and initial firm state without choosing output format."""
    directory = Path(directory)
    mandate = read_text_file(mandate_path or directory / "mandate.md")
    config = _owner_config(config_path or directory / "firm.toml")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise FirmError("firm directory must be owner-controlled")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    database = directory / "durable" / "firm.sqlite3"
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        database_info = database.lstat()
    except FileNotFoundError:
        database_info = None
    if database_info is not None:
        if not stat.S_ISREG(database_info.st_mode):
            raise FirmError("a non-regular firm database path already exists")
        from .compatibility import validate_firm

        validate_firm(directory)
        from .setup_check import database_initialization_state

        if database_initialization_state(database) == "invalid":
            raise FirmError(
                "the database is neither a safely resumable first initialization "
                "nor a valid initialized firm; no database changes were made"
            )
    else:
        from .compatibility import initialize_firm

        initialize_firm(directory)
    runtime = Runtime(Store(database))
    github.ensure_app(config, terminal)
    return runtime.lifecycle.initialize(firm_id or directory.resolve().name, mandate, config)


def _start_selected(args, directory: Path, *, guided: bool = False) -> None:
    from .compatibility import validate_firm

    validate_firm(directory)
    database = directory / "durable" / "firm.sqlite3"
    runtime = Runtime(Store(database))
    sandbox_config = args.sandbox_config or directory / "sandbox.toml"
    if args.foreground:
        from .foreground import serve

        serve(
            runtime,
            sandbox_config=sandbox_config,
            mcp_port=args.mcp_port,
            console_port=args.console_port,
            timeout=args.timeout,
            wait_for_resume=True,
        )
    else:
        from .foreground import launch_local_host

        result = launch_local_host(
            directory,
            sandbox_config=sandbox_config,
            mcp_port=args.mcp_port,
            console_port=args.console_port,
            timeout=args.timeout,
            open_browser=not args.no_browser,
        )
        if guided:
            print("Owner console is ready; the firm is starting.")
            print(f"Firm data directory: {directory}")
            print(f"Owner console: {result['unlock_url']}")
            print("Use Stop to stop execution, then Close Firm to close the host.")
            print(f"Resume later with: firm start {shlex.quote(str(directory))}")
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a stopped firm containing only its founder")
    init.add_argument("directory", type=Path)
    init.add_argument(
        "--firm-id", help="Defaults to the directory name; use YYYY-MM-DD_useful-slug"
    )
    init.add_argument("--mandate", type=Path, help="Defaults to DIRECTORY/mandate.md")
    init.add_argument("--config", type=Path, help="Defaults to DIRECTORY/firm.toml")
    check = commands.add_parser(
        "check", help="Check local files, credentials and ports without changing the firm"
    )
    check.add_argument("directory", type=Path)
    check.add_argument("--mcp-port", type=int, default=8765)
    check.add_argument("--console-port", type=int, default=8766)
    status = commands.add_parser(
        "status", help="Report company state, roles and whether infrastructure is running"
    )
    status.add_argument("directory", type=Path)
    network = commands.add_parser(
        "network",
        help="Inspect firm traffic, switch egress off or on, or allow or revoke a host",
    )
    network.add_argument("directory", type=Path)
    network.add_argument(
        "mode", choices=("status", "on", "off", "allow", "revoke"), nargs="?", default="status"
    )
    network.add_argument("host", nargs="?", help="With allow or revoke: api.example.com")
    network.add_argument("--sandbox-config", type=Path)
    requests = commands.add_parser(
        "requests", help="List what roles asked the owner for, or grant or deny one"
    )
    requests.add_argument("directory", type=Path)
    requests.add_argument("action", choices=("list", "grant", "deny"), nargs="?", default="list")
    requests.add_argument("request_id", nargs="?")
    requests.add_argument("--reason", help="Required with deny; the role reads it")
    requests.add_argument("--sandbox-config", type=Path)
    analyze = commands.add_parser(
        "analyze", help="Read recorded firm behavior without starting execution"
    )
    analyze.add_argument("directory", type=Path, help="A firm directory or a firm export")
    analyze.add_argument("--from", dest="from_at")
    analyze.add_argument("--until")
    analyze.add_argument("--employee-id")
    analyze.add_argument("--section", choices=SECTIONS)
    analyze.add_argument(
        "--row-type", help="With --section, only rows of this type, e.g. lease in the mac section"
    )
    analyze.add_argument(
        "--conversation-id",
        help="One conversation in the conversations section, with the message bodies",
    )
    analyze.add_argument("--cursor")
    analyze.add_argument("--limit", type=int, default=100)
    analyze.add_argument("--json", action="store_true", help="Emit JSON (the default)")
    start = commands.add_parser(
        "start", help="Create, resume, or run a firm with its local console"
    )
    start.add_argument(
        "directory", type=Path, nargs="?", help="Existing firm directory; omit for guided setup"
    )
    start.add_argument("--data-dir", type=Path, help="Owner data directory used by guided setup")
    start.add_argument("--sandbox-config", type=Path, help="Defaults to DIRECTORY/sandbox.toml")
    start.add_argument("--mcp-port", type=int, default=8765)
    start.add_argument("--console-port", type=int, default=8766)
    start.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT,
        help="Seconds without role tool calls or new provider output (default: 1800)",
    )
    start.add_argument(
        "--foreground",
        action="store_true",
        help=(
            "Keep the host in this terminal for diagnosis; the default opens or reconnects "
            "to the local console"
        ),
    )
    start.add_argument(
        "--no-browser", action="store_true", help="Start or reconnect without opening a browser"
    )
    stop = commands.add_parser("stop", help="Close admission; the foreground host applies shutdown")
    stop.add_argument("directory", type=Path)
    stop.add_argument("--force", action="store_true", help="Terminate active execution")
    send = commands.add_parser("send", help="Send owner mail without starting execution")
    send.add_argument("directory", type=Path)
    send.add_argument("recipients", nargs="+")
    send.add_argument("--body", required=True)
    send.add_argument("--subject")
    send.add_argument("--request-id", required=True)
    configure = commands.add_parser(
        "configure", help="Apply execution settings; retained conversations keep their settings"
    )
    configure.add_argument("directory", type=Path)
    configure.add_argument("--config", type=Path, required=True)
    accounts = commands.add_parser(
        "accounts", help="Inspect account slots, quota headroom and shared incidents"
    )
    accounts.add_argument("directory", type=Path)
    quota = commands.add_parser(
        "quota", help="Record an explicitly sourced, expiring quota observation"
    )
    quota.add_argument("directory", type=Path)
    quota.add_argument("account")
    reading = quota.add_mutually_exclusive_group(required=True)
    reading.add_argument("--remaining-fraction", type=float)
    reading.add_argument("--unknown", action="store_true")
    quota.add_argument("--observed-at", type=float, help="UTC epoch seconds; defaults to now")
    quota.add_argument(
        "--valid-for", type=float, required=True, help="Observation lifetime in seconds"
    )
    quota.add_argument("--source", required=True)
    hold = commands.add_parser("hold", help="Pause an account or execution profile")
    hold.add_argument("directory", type=Path)
    hold.add_argument("scope", choices=["account", "profile"])
    hold.add_argument("dependency")
    hold.add_argument("--kind", choices=["auth", "config", "quota", "outage"], required=True)
    hold.add_argument("--until", type=float, help="UTC epoch seconds; omit for owner intervention")
    clear = commands.add_parser("clear-hold", help="Clear one exact shared incident after repair")
    clear.add_argument("directory", type=Path)
    clear.add_argument("incident_id")
    for name, description in (
        ("stop-role", "Pause admission and request termination of this role"),
        ("resume-role", "Resume a stopped role, retaining its conversation and holds"),
        ("route", "Reopen a stopped role with fresh context on an explicit profile"),
        ("retry", "Release one role-specific refusal/exhaustion episode after review"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("directory", type=Path)
        command.add_argument("employee_id")
        command.add_argument("--request-id", required=True)
        if name == "route":
            command.add_argument("--profile", required=True)
            command.add_argument("--fresh", action="store_true", required=True)
        if name == "retry":
            command.add_argument("--episode", required=True)
    github_command = commands.add_parser("github", help="Manage the firm's GitHub access")
    github_actions = github_command.add_subparsers(dest="action", required=True)
    for name, description in (
        ("revoke", "Refuse a repository the firm created or you granted until you grant it again"),
        ("grant", "Give write access to a repository of the organization, lifting a revocation"),
    ):
        action = github_actions.add_parser(name, help=description)
        action.add_argument("directory", type=Path)
        action.add_argument("repo", help="organization/repository")
        action.add_argument(
            "--address", help="Only this role address and the roles under it; default every role"
        )
    save = commands.add_parser(
        "save-credentials",
        help="Store credentials read from stdin as the account's default private auth_file",
    )
    save.add_argument("account")
    save.add_argument("--provider", choices=("claude", "codex", "agy"), required=True)
    login = commands.add_parser("login", help="Sign in using an isolated provider profile")
    login.add_argument("account")
    login.add_argument("--provider", choices=("claude", "codex", "agy"), required=True)
    login.add_argument("--auth-file", type=Path, help="Configured account auth_file")
    listing = commands.add_parser(
        "list", help="List firms under a firms directory with state and durable size"
    )
    listing.add_argument("firms_directory", type=Path)
    export = commands.add_parser(
        "export", help="Save the database, host records and drive to DIRECTORY/export"
    )
    export.add_argument("directory", type=Path)
    export.add_argument("destination", type=Path, nargs="?", help="Write the export here instead")
    discard = commands.add_parser(
        "discard-vms",
        help="Export a stopped, idle firm, then discard its VMs and guest disks for good; "
        "the firm directory and its export remain",
    )
    discard.add_argument("directory", type=Path)
    discard.add_argument("--confirm", required=True, metavar="FIRM_ID")
    export.add_argument(
        "--homes",
        action="store_true",
        help="Include each role's private work and provider sessions",
    )
    discard.add_argument(
        "--no-homes",
        dest="homes",
        action="store_false",
        help="Leave out each role's private work and provider sessions, lost with the disks",
    )
    delete = commands.add_parser(
        "delete", help="Permanently remove a stopped, idle firm directory, its export included"
    )
    delete.add_argument("directory", type=Path)
    delete.add_argument("--confirm", required=True, metavar="FIRM_ID")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            result = fleet.list_firms(args.firms_directory)
        elif args.command == "start" and args.directory is None:
            if args.sandbox_config is not None:
                raise FirmError("--sandbox-config requires an explicit firm directory")
            _run_guided_start(args)
            return
        elif args.command == "start" and args.data_dir is not None:
            raise FirmError("--data-dir is only used by guided `firm start` without a directory")
        elif args.command == "login":
            path = login_provider(args.provider, args.account,
                                  target_credentials_path=args.auth_file, force_login=True)
            result = {"account": args.account, "provider": args.provider, "auth_file": str(path)}
        elif args.command == "save-credentials":
            from .accounts import credentials_path
            from .credential_store import CredentialStore

            path = credentials_path(args.account, args.provider)
            if sys.stdin.isatty():
                print("Paste the credentials, then press Ctrl-D.", file=sys.stderr)
            try:
                CredentialStore(args.provider, path).save(sys.stdin.buffer.read())
            except ProviderSetupError as exc:
                raise FirmError(
                    f"stdin is not usable {args.provider} credentials ({exc.code})"
                ) from None
            result = {"account": args.account, "provider": args.provider, "auth_file": str(path)}
        elif args.command == "check":
            from .setup_check import check_setup

            result = check_setup(
                args.directory, mcp_port=args.mcp_port, console_port=args.console_port
            )
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
            for check in result["checks"]:
                if check["status"] == "warning":
                    print(f"warning: {check['action']}", file=sys.stderr)
            if not result["ok"]:
                raise SystemExit(2)
            return
        elif args.command == "init":
            result = _initialize_firm(
                args.directory,
                firm_id=args.firm_id,
                mandate_path=args.mandate,
                config_path=args.config,
                terminal=sys.stdin.isatty(),
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return
        else:
            database = args.directory / "durable" / "firm.sqlite3"
            if args.command == "analyze" and (args.directory / exports.MANIFEST).is_file():
                database = args.directory / exports.DATABASE
            if not database.is_file():
                raise FirmError("firm database does not exist")
            if args.command != "analyze":
                from .compatibility import validate_firm

                validate_firm(args.directory)
            if args.command == "analyze":

                def timestamp(value):
                    if value is None:
                        return None
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is None:
                        raise ValueError(
                            "analysis timestamps require a timezone, e.g. 2026-09-10T00:00:00Z"
                        )
                    return parsed.timestamp()

                def network_status():
                    # An export has no sandbox; the network section then says so.
                    if database != args.directory / "durable" / "firm.sqlite3":
                        raise FirmError("an export carries no live sandbox")
                    return _sandbox(args.directory, None).network_status()

                result = AnalysisReader(database, network_status=network_status).read(
                    args.section,
                    from_at=timestamp(args.from_at),
                    until=timestamp(args.until),
                    employee_id=args.employee_id,
                    row_type=args.row_type,
                    conversation_id=args.conversation_id,
                    limit=args.limit,
                    cursor=args.cursor,
                )
                print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
                return
            if args.command == "start":
                _start_selected(args, args.directory)
                return
            runtime = Runtime(Store(database))
            if args.command == "network":
                if (args.host is None) != (args.mode in {"status", "on", "off"}):
                    raise FirmError("allow and revoke take one host; status, on and off take none")
                sandbox = _sandbox(args.directory, args.sandbox_config)
                runtime.hosts.sandbox = sandbox
                try:
                    if args.mode == "status":
                        result = sandbox.network_status()
                    elif args.mode in {"on", "off"}:
                        result = sandbox.set_network_enabled(args.mode == "on")
                    elif args.mode == "allow":
                        result = runtime.hosts.allow(OWNER, args.host)
                    else:
                        result = runtime.hosts.revoke(OWNER, args.host)
                except RuntimeError as exc:
                    raise FirmError(str(exc)) from exc
            elif args.command == "requests":
                if (args.request_id is None) != (args.action == "list"):
                    raise FirmError("grant and deny take one request id; list takes none")
                if args.action == "list":
                    result = runtime.requests.listing(OWNER)
                else:
                    # A host decision rewrites the recorded grants to the sandbox.
                    runtime.hosts.sandbox = _sandbox(args.directory, args.sandbox_config)
                    try:
                        result = (
                            runtime.requests.deny(OWNER, args.request_id, args.reason)
                            if args.action == "deny"
                            else runtime.requests.grant(OWNER, args.request_id)
                        )
                    except RuntimeError as exc:
                        raise FirmError(str(exc)) from exc
            elif args.command == "send":
                result = runtime.messaging.send(
                    OWNER,
                    args.recipients,
                    args.body,
                    subject=args.subject,
                    request_id=args.request_id,
                )
            elif args.command == "stop":
                result = runtime.stop(args.force)
            elif args.command == "configure":
                config = _owner_config(args.config, runtime)
                github.ensure_app(config, sys.stdin.isatty())
                result = runtime.owner.configure(OWNER, config)
            elif args.command == "github":
                operation = (
                    runtime.owner.revoke_github
                    if args.action == "revoke"
                    else runtime.owner.grant_github
                )
                result = operation(OWNER, args.repo, args.address)
            elif args.command == "accounts":
                result = runtime.accounts.status()
                for warning in result["warnings"]:
                    print(f"warning: {warning}", file=sys.stderr)
            elif args.command == "quota":
                result = runtime.accounts.observe(
                    OWNER,
                    args.account,
                    args.remaining_fraction,
                    observed_at=args.observed_at
                    if args.observed_at is not None
                    else runtime.store.clock(),
                    valid_for=args.valid_for,
                    source=args.source,
                )
            elif args.command == "hold":
                result = runtime.accounts.pause(
                    OWNER, args.scope, args.dependency, args.kind, args.until
                )
            elif args.command == "clear-hold":
                result = runtime.accounts.clear(OWNER, args.incident_id)
            elif args.command == "stop-role":
                result = runtime.owner.stop_role(OWNER, args.employee_id, args.request_id)
            elif args.command == "resume-role":
                result = runtime.owner.resume_role(OWNER, args.employee_id, args.request_id)
            elif args.command == "route":
                result = runtime.owner.route(OWNER, args.employee_id, args.profile, args.request_id)
            elif args.command == "retry":
                result = runtime.retry(OWNER, args.employee_id, args.episode, args.request_id)
            elif args.command == "export":
                result = fleet.export(
                    args.directory, runtime, destination=args.destination, homes=args.homes
                )
            elif args.command == "discard-vms":
                result = fleet.discard_vms(args.directory, args.confirm, runtime, homes=args.homes)
            elif args.command == "delete":
                result = fleet.delete(args.directory, args.confirm, runtime)
            elif args.command == "status":
                result = runtime.status()
                sandbox_config = args.directory / "sandbox.toml"
                if sandbox_config.is_file():
                    state, error = fleet.sandbox_status(sandbox_config)
                    result["sandbox_state"] = state
                    if error is not None:
                        result["sandbox_error"] = error
            else:
                result = runtime.status()
    except (FirmError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"firm: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
