"""Construct execution adapters from durable session bindings."""

import json
from pathlib import Path

from .accounts import validate_accounts
from .errors import FirmError
from .prompt_records import record_inputs
from .provider_contract import ProviderSetupError
from .provider_support import activation_url
from .store import OWNER
from .summaries import SandboxSummaryRunner, SummaryLaunch, SummaryService, summary_role_id
from .task_runner import WORKER_INSTRUCTIONS, GuestTaskRunner
from .tasks import TaskExecutionError


def provider_adapter(profile, account):
    """Construct a provider adapter from already-validated local settings."""
    if profile["provider"] == "codex":
        from .codex_provider import CodexProvider as Adapter
    elif profile["provider"] == "claude":
        from .claude_provider import ClaudeProvider as Adapter
    else:
        raise FirmError("unsupported provider in retained binding")
    return Adapter(
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
                    self.runtime.accounts.pause(OWNER, scope, dependency, exc.category)
                    result.append({"scope": scope, "dependency": dependency, "code": exc.code})
        return {"checked_accounts": sorted(checked), "failures": result}

    def for_session(self, session_id):
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
                request, sandbox.layout(summary_role_id(request.run_id))
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
            from .claude_provider import ClaudeProvider
            from .codex_provider import CodexProvider

            if request is None:
                raise FirmError("summary request was not retained")
            # A later configuration edit cannot change how an already-started
            # provider's output and usage are interpreted.
            parser = {"codex": CodexProvider, "claude": ClaudeProvider}[request.provider]
            output = parser.parse_summary(logs, exit_code)
            subscription = getattr(parser, "parse_subscription", None)
            reading = subscription(logs) if subscription is not None else None
            if reading is not None and request.account_id:
                self.runtime.accounts.record_subscription(
                    request.account_id,
                    reading["windows"],
                    status=reading.get("status"),
                    warning=reading.get("warning"),
                )
            return output

        return SandboxSummaryRunner(
            sandbox,
            build,
            parse,
            start_url=summary_url,
            start_allowed=lambda: self.runtime.status()["state"] == "running",
        )

    def task_runner(self, sandbox, *, mcp_url):
        """Fresh workers on the caller's bound account and child Linux identity."""
        start_url = activation_url(mcp_url).replace("/activation/start", "/summary/start")

        def build(request, parent):
            with self.runtime.store.read() as db:
                account = self.runtime.accounts.config(db)["accounts"].get(request.account_id)
            if not account or account["provider"] != request.provider:
                raise FirmError("task account/provider binding changed")
            adapter = provider_adapter(
                {"provider": request.provider, "model": request.model, "effort": request.effort},
                account,
            )
            launch = SummaryLaunch(
                run_id=request.run_id, prompt=request.instruction, max_output_tokens=0,
                timeout=max(0, request.deadline - self.runtime.store.clock()),
                account_id=request.account_id, provider=request.provider,
                model=request.model, effort=request.effort, start_url=start_url,
                start_token=request.start_token, kind="task", instructions=WORKER_INSTRUCTIONS,
                tools_enabled=True,
            )
            spec = adapter.build_task_spec(
                launch, sandbox.child_layout(parent.role_id, request.run_id)
            )
            record_inputs(
                self.runtime.store.path.parent / "inputs",
                request.run_id,
                spec,
                kind="tasks",
                metadata={
                    "run_id": request.run_id,
                    "caller_activation_id": request.caller_activation_id,
                    "provider": request.provider,
                    "model": request.model,
                    "offered_role_tools": [],
                    "tool_policy": {
                        "provider_native_tools": "enabled",
                        "firm_tools": "disabled",
                        "delegation": "disabled",
                    },
                },
            )
            return spec

        def parse(request, logs, exit_code, *, apply_holds):
            return record_task_output(
                self.runtime, request.run_id, request.provider, request.account_id, logs, exit_code,
                apply_holds=apply_holds,
            )

        return GuestTaskRunner(self.runtime, sandbox, build_spec=build, parse_output=parse)


def record_task_output(runtime, run_id, provider, account_id, logs, exit_code, *, apply_holds):
    from . import claude_provider, codex_provider

    parser = {"codex": codex_provider, "claude": claude_provider}[provider]
    result = parser.parse_result(logs, exit_code)
    observed = parser.parse_usage(logs)
    if apply_holds:
        subscription = getattr(parser, "parse_subscription", None)
        reading = subscription(logs) if subscription is not None else None
        if reading is not None:
            runtime.accounts.record_subscription(
                account_id,
                reading["windows"],
                status=reading.get("status"),
                warning=reading.get("warning"),
            )
    counters = dict(observed.__dict__) if observed else {}
    # Every task owns a fresh session, so session totals are task totals even
    # if cancellation prevented the final turn.completed event.
    for name in ("input_tokens", "cached_input_tokens", "output_tokens",
                 "reasoning_output_tokens"):
        if counters.get(name) is None:
            counters[name] = counters.get("session_" + name)
    usage = SummaryService._safe_usage(counters) or {}
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE summary_runs SET input_tokens=COALESCE(?,input_tokens),"
            "cached_input_tokens=COALESCE(?,cached_input_tokens),"
            "output_token_usage=COALESCE(?,output_token_usage),"
            "output_tokens=COALESCE(?,output_tokens),"
            "reasoning_output_tokens=COALESCE(?,reasoning_output_tokens),"
            "provider_context_window=COALESCE(?,provider_context_window),"
            "native_compactions=COALESCE(?,native_compactions),"
            "resolved_model=COALESCE(?,resolved_model) WHERE run_id=? AND kind='task'",
            (usage.get("input_tokens"), usage.get("cached_input_tokens"),
             usage.get("output_tokens"), usage.get("output_tokens"),
             usage.get("reasoning_output_tokens"), usage.get("provider_context_window"),
             usage.get("native_compactions"), usage.get("resolved_model"), run_id),
        )
        error = result.error
        if apply_holds and error and error.category in {"auth", "config", "quota"}:
            limits = []
            if error.reset_at is not None:
                limits.append(error.reset_at)
            if error.retry_after_seconds is not None:
                limits.append(runtime.store.clock() + error.retry_after_seconds)
            until = max(limits) if limits else None
            runtime.accounts.hold(
                db, "account", account_id, error.category,
                until if error.category == "quota" else None,
            )
    if result.error:
        raise TaskExecutionError("provider_failure")
    return result.final_text
