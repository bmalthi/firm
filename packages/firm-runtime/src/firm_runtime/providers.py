"""Construct execution adapters from durable session bindings."""

import json
from pathlib import Path

from .accounts import validate_accounts
from .errors import FirmError
from .prompt_records import record_inputs
from .provider_contract import ExecutionProvider, ProviderSetupError
from .provider_registry import provider_implementation
from .provider_support import activation_url
from .store import OWNER
from .summaries import SandboxSummaryRunner, summary_role_id


def provider_adapter(profile, account) -> ExecutionProvider:
    """Construct a provider adapter from already-validated local settings."""
    implementation = provider_implementation(profile["provider"])
    return implementation.constructor(
        model=profile["model"],
        effort=profile["effort"],
        auth_file=Path(account["auth_file"]).expanduser(),
    )


class Providers:
    def __init__(self, runtime):
        self.runtime = runtime
        with runtime.store.read() as db:
            config = runtime.accounts.config(db)
            validate_accounts(config)
            if "accounts" not in config:
                raise FirmError("sandbox execution requires named accounts in firm configuration")

    def preflight(self):
        """Check local provider settings/credentials before reserving role execution.

        Remote access and model availability still come from structured provider
        responses. A local repair never implicitly clears a shared incident.
        """
        with self.runtime.store.read() as db:
            config = self.runtime.accounts.config(db)
        checked = set()
        result = []
        for name, profile in config["profiles"].items():
            for account_id in profile["accounts"]:
                account = config["accounts"][account_id]
                try:
                    adapter = provider_adapter(profile, account)
                    if account_id not in checked:
                        adapter.validate_credentials()
                        checked.add(account_id)
                except ProviderSetupError as exc:
                    scope = "account" if exc.category == "auth" else "profile"
                    dependency = account_id if scope == "account" else name
                    if exc.category in {"auth", "config"}:
                        self.runtime.accounts.pause(
                            OWNER, scope, dependency, exc.category, code=exc.code
                        )
                    result.append({"scope": scope, "dependency": dependency, "code": exc.code})
        return {"checked_accounts": sorted(checked), "failures": result}

    def for_session(self, session_id) -> ExecutionProvider:
        with self.runtime.store.read() as db:
            binding = self.runtime.accounts.binding(db, session_id)
            if not binding:
                raise FirmError("execution has no durable provider/account binding")
            profile = json.loads(binding["settings_json"])
            current = (
                self.runtime.accounts.config(db).get("accounts", {}).get(binding["account_id"])
            )
            account = (
                current
                if current and current["provider"] == binding["provider"]
                else json.loads(binding["account_json"])
            )
        return provider_adapter(profile, account)

    def summary_runner(self, sandbox, *, mcp_url):
        """Return the foreground runner for no-authority summary and staffing turns."""
        summary_url = activation_url(mcp_url).replace("/activation/start", "/summary/start")

        def build(request):
            # SummaryLaunch carries the authenticated account reservation, while the
            # runner deliberately receives no role token, shares, or MCP URL.
            with self.runtime.store.read() as db:
                config = self.runtime.accounts.config(db)
                account = config.get("accounts", {}).get(request.account_id)
            if not account or account.get("provider") != request.provider:
                raise FirmError("summary account/provider binding changed")
            adapter = provider_adapter(
                {"provider": request.provider, "model": request.model, "effort": request.effort},
                account,
            )
            spec = adapter.build_summary_spec(
                request, sandbox.layout(summary_role_id(request.run_id, request.stateless_slot))
            )
            record_inputs(
                self.runtime.store.path.parent / "inputs",
                request.run_id,
                spec,
                kind="summaries" if request.kind == "summary" else request.kind,
                metadata={
                    "run_id": request.run_id,
                    "provider": request.provider,
                    "model": request.model,
                    "offered_role_tools": [],
                },
            )
            return spec

        def parse(request, logs, exit_code):
            if request is None:
                raise FirmError("summary request was not retained")
            # A later configuration edit cannot change how an already-started
            # provider's output and usage are interpreted.
            parser = provider_implementation(request.provider)
            output = parser.parse_summary(logs, exit_code)
            reading = parser.parse_subscription(logs)
            if reading is not None and request.account_id:
                self.runtime.accounts.record_subscription(
                    request.account_id,
                    list(reading.windows),
                    status=reading.status,
                    warning=reading.warning,
                )
            return output

        return SandboxSummaryRunner(
            sandbox,
            build,
            parse,
            start_url=summary_url,
            start_allowed=lambda: self.runtime.status()["state"] == "running",
        )
