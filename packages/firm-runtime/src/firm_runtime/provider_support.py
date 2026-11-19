"""Shared validation and prompt rendering for provider adapters."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from . import guidance
from .prompt_resources import prompt, render
from .runtime import Invocation
from .store import OWNER

MCP_TIMEOUT_SECONDS = 120


def role_mcp_settings(request) -> tuple[tuple[str, ...], int]:
    """Validate Firm exposure and return the MCP call timeout in seconds."""
    # Keep metadata-only provider imports independent of the MCP implementation.
    from .mcp_server import TOOL_DESCRIPTIONS

    names = request.offered_tools
    if (
        not isinstance(names, tuple)
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            for name in names
        )
        or len(set(names)) != len(names)
        or not set(names) <= TOOL_DESCRIPTIONS.keys()
    ):
        raise ValueError("offered Firm tools must be a unique subset of the Firm catalog")
    return names, MCP_TIMEOUT_SECONDS


def canonical_uuid(value: str, label: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a UUID") from None
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical UUID")
    return value


def text_setting(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 128:
        raise ValueError(f"{label} must be a non-empty string of at most 128 characters")
    return value


def activation_url(mcp_url: str) -> str:
    try:
        parsed = urlsplit(mcp_url)
    except ValueError:
        raise ValueError("mcp_url must be an HTTP URL ending in /mcp") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("mcp_url must be an HTTP URL ending in /mcp")
    path = parsed.path.rstrip("/")
    if not path.endswith("/mcp"):
        raise ValueError("mcp_url must end in /mcp")
    path = path[: -len("/mcp")] + "/activation/start"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _mandate(value: object) -> str:
    if not isinstance(value, dict):
        raise ValueError("current mandate must be an object")
    body, author = value.get("body"), value.get("author_address", value.get("author_id"))
    if not isinstance(body, str) or not isinstance(author, str) or not author:
        raise ValueError("current mandate must contain body and author")
    return render("workplace", "mandate", author=author, body=body)


def standing(context: dict) -> str:
    """Employee definition first; changing operational facts belong to the wake."""
    if not isinstance(context, dict):
        raise ValueError("invocation context must be an object")
    address = context.get("address")
    if not isinstance(address, str) or not address:
        raise ValueError("invocation context must contain a role address")
    manager = context.get("manager_address", context.get("manager_id"))
    identity = context.get("identity", address)
    title = address if identity == address else f"{identity} ({address})"
    sections = [
        render("workplace", "identity", title=title, manager=manager),
        _mandate(context.get("mandate")),
    ]
    refs = context.get("workspace_references") or {}
    if any(not isinstance(refs.get(key), str) for key in ("workspace", "private", "scratch")):
        raise ValueError("invocation context must contain workspace references")
    workplace = [
        render("workplace", "workplace", workspace=refs["workspace"], private=refs["private"],
               scratch=refs["scratch"])
    ]
    if shared := refs.get("shared"):
        workplace.append(render("workplace", "shared", shared=shared))
    if refs.get("drive"):
        workplace.append(render("workplace", "access", drive=refs["drive"]))
    if manager != OWNER:
        workplace.append(render("workplace", "founder"))
    capabilities = context.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        raise ValueError("invocation capabilities must be an object")
    llm_clis = [
        name for name in (capabilities.get("llm") or ())
        if isinstance(name, str) and name in {"claude", "codex", "agy"}
    ]
    if llm_clis:
        plural = "" if len(llm_clis) == 1 else "s"
        llm_section = [render("workplace", "llm_intro", plural=plural), ""]
        llm_section.extend(render("workplace", f"llm_{name}") for name in llm_clis)
        llm_section.append("")
        llm_section.append(
            render(
                "workplace",
                "llm_boundaries",
                max_concurrent=capabilities.get("llm_max_concurrent", 2),
            )
        )
        workplace.append("\n" + "\n".join(llm_section) + "\n")
    enabled = []
    if capabilities.get("github") is True:
        enabled.append(render("workplace", "capability_github"))
    if capabilities.get("mac") is True:
        enabled.append(render("workplace", "capability_mac"))
    if enabled:
        workplace.append("## Configured optional capabilities\n\n" + "\n\n".join(enabled))
    else:
        workplace.append(render("workplace", "capabilities_none"))
    sections += [
        "\n".join(workplace), prompt("standing-physics.md"), prompt("standing-good-worker.md")
    ]
    selection = (json.loads(context["guidance"]) if context.get("guidance")
                 else {"scope": None, "functions": []})
    ids = ([selection["scope"]] if selection["scope"] else []) + [
        e["id"] for e in guidance.catalog() if e["dimension"] == "function"
        and e["id"] in selection["functions"]
    ]
    if ids:
        sections.append(prompt("guidance-introduction.md"))
        sections.extend(guidance.body(i) for i in ids)
    sections.append(prompt("standing-limits.md"))
    return "\n\n".join(sections) + "\n"


def _orientation(context: dict) -> str:
    value = context.get("orientation") or {}
    lines = []
    for key, label in (
        ("direct_reports", "Direct reports"),
        ("top_level_roles", "Other functions"),
    ):
        if key not in value or (key == "top_level_roles" and not value.get("top_level_included")):
            continue
        people = value[key]
        labels = []
        for person in people:
            address = person["address"]
            if address in {
                context.get("address"),
                context.get("manager_address", context.get("manager_id")),
            }:
                continue
            identity = person.get("identity", address)
            labels.append(address if identity == address else f"{identity} ({address})")
        omitted = value.get("omitted", {}).get(key, 0)
        if labels:
            lines.append(f"{label}: " + "; ".join(labels) + ".")
        elif not people and not omitted:
            lines.append(f"{label}: none.")
        if omitted:
            lines.append(
                render("wake", "orientation_omitted", omitted=omitted, label=label.lower())
            )
    return "\n".join(lines)


def _recovery(value: dict) -> str:
    retained = value.get("context_retained", False)
    cause = "owner_stop" if value.get("cause") == "owner_stop" else "interrupted"
    lines = [render("wake", cause)]
    if when := value.get("time_utc"):
        label = "Detected at" if value.get("time_is_detection") else "At"
        lines.append(f"{label} {when}.")
    lines.append(render("wake", "retained" if retained else "lost"))
    lines.append(render("wake", "check_results"))
    reads = value.get("recent_reads", [])
    if reads:
        lines.append(render("wake", "recent_reads"))
        for mail in reads[:5]:
            lines.append(
                f"- {mail['message_id']}"
                + f" — {mail.get('subject') or mail.get('preview', 'No subject')}"
            )
    return "\n".join(lines)


def render_invocation(invocation: Invocation, provider_session_id: str | None) -> str:
    from .role_output import render_inbox

    context = invocation.context
    if not isinstance(context, dict):
        raise ValueError("invocation context must be an object")
    sections = []
    birth = bool(context.get("birth"))
    if birth:
        manager = context.get("manager_address", context.get("manager_id"))
        sections.append(prompt("founder_birth.md" if manager == OWNER else "birth.md"))
    elif "recovery" in context:
        sections.append(_recovery(context["recovery"]))
    elif "handoff_notes" in context:
        notes = context["handoff_notes"]
        if not isinstance(notes, str):
            raise ValueError("handoff_notes must be a string")
        sections.append(render("wake", "handoff", notes=notes))
    elif context.get("fresh_session"):
        sections.append(render("wake", "fresh"))
    else:
        sections.append(
            render("wake", "update", reason=context.get("wake_reason", render("wake", "default")))
        )
    if context.get("idle_inbox_review"):
        sections.append(render("wake", "idle_review_instructions"))
    if context.get("mandate_changed") and provider_session_id is not None:
        sections.append(render("wake", "mandate_changed"))
    if context.get("guidance_changed") and provider_session_id is not None:
        sections.append(render("wake", "guidance_changed"))
    if orientation := _orientation(context):
        sections.append(orientation)
    if terms := context.get("temporary_appointment"):
        remaining = terms.get("remaining_periods")
        if remaining is not None:
            sections.append(render("wake", "temporary", remaining=remaining))
    overview = context.get("inbox", {})
    awareness = overview.get("context", {})
    own = dict(awareness.get("self", {}))
    if context.get("temporary_appointment"):
        own.pop("temporary_periods", None)
    if overview.get("context_pressure", {}).get("level") in {"advisory", "handoff_required"}:
        own.pop("context", None)
    overview = {**overview, "context": {**awareness, "self": own}}
    inbox = render_inbox(overview)
    if inbox != render("results", "no_unread_mail"):
        sections.append(render("wake", "inbox", inbox=inbox))
    elif not birth:
        sections.append(render("wake", "empty_inbox"))
    return "\n\n".join(sections).strip() + "\n"


def summary_instructions() -> str:
    return prompt("runtime_summary.md") + "\n"


def level_instructions() -> str:
    return prompt("runtime_level-choice.md") + "\n"
