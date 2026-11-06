"""Readable employee tool output; service records and owner diagnostics stay structured."""

from datetime import UTC, datetime

from .prompt_resources import render

# These are runtime bookkeeping, never instructions or employee work content.
_INTERNAL = {
    "activation_id",
    "session_id",
    "manager_id",
    "execution_profile",
    "execution_profiles",
    "provider",
    "model",
    "account_id",
    "provider_session_id",
    "settings_json",
    "digest",
    "public_profile_digest",
    "snapshot",
    "selection",
    "offset",
    "character_start",
    "character_end",
    "matched_terms",
    "identity_and_context",
    "other_holds",
    "used_periods",
    "requested_at",
    "measured_at",
    "observed_tokens",
    "window_tokens",
    "window_source",
    "age_seconds",
    "current_context_tokens",
    "provider_context_window",
    "soft_context_window",
    "remaining_soft_tokens",
    "handoff_at_tokens",
    "session_high_water_tokens",
    "change_since_previous_guidance",
    "consumed_fraction",
    "native_compactions",
    "mandate_revision",
    "access_on_activation",
    "related_actions",
    "seen_sequence",
}


def person(value: dict | str) -> str:
    if isinstance(value, str):
        return "Operations" if value == "runtime" else "Owner" if value == "owner" else value
    address = value.get("address") or value.get("employee_id", "")
    if address in {"runtime", "owner"}:
        return person(address)
    employee = value.get("employee_id", "")
    label = str(address)
    if employee and employee != address:
        label += f" ({employee})"
    identity = value.get("identity")
    if identity and identity not in {address, employee}:
        label = f"{identity} — {label}"
    if value.get("employment") == "retired":
        label += " [retired]"
    return label


def _time(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")
    return str(value)


def _controls(
    result: dict,
    *,
    include_checklist: bool = True,
    excluded_instructions: set[str] | None = None,
) -> list[str]:
    lines = []
    seen_instructions = set(excluded_instructions or ())
    for key in ("context_pressure", "operations_control"):
        control = result.get(key)
        if not isinstance(control, dict):
            continue
        if key == "context_pressure" and control.get("level") not in {
            "advisory",
            "handoff_required",
        }:
            continue
        instruction = control.get("instruction")
        if instruction and instruction not in seen_instructions:
            lines.append(render("results", "operations_instruction", instruction=instruction))
            seen_instructions.add(instruction)
        if include_checklist and control.get("handoff_checklist"):
            lines.append(str(control["handoff_checklist"]))
    return lines


def _mail_waiting(result: dict) -> list[str]:
    """One line for mail that arrived during this work, ordered by whose mail it is."""
    control = result.get("mail_waiting")
    if not isinstance(control, dict) or not control.get("senders"):
        return []
    senders = ", ".join(f"{count} from {person(sender)}" for sender, count in control["senders"])
    return [render("results", "mail_waiting", senders=senders)]


def _awareness(context: dict) -> list[str]:
    """Keep consequential self/report facts without printing measurement machinery."""
    lines = []
    for notice in context.get("notices", []):
        if isinstance(notice, str):
            lines.append(notice)
    rows = [("You", context.get("self", {}))]
    rows.extend((person(row), row) for row in context.get("direct_reports", []))
    for label, row in rows:
        if not isinstance(row, dict):
            continue
        pressure = row.get("context", {})
        level = pressure.get("level")
        if level in {"advisory", "handoff_required"}:
            if level == "handoff_required":
                key = (
                    "awareness_stale_full" if pressure.get("state") == "stale" else "awareness_full"
                )
            else:
                key = (
                    "awareness_stale_memory"
                    if pressure.get("state") == "stale"
                    else "awareness_memory"
                )
            lines.append(render("results", key, label=label))
        terms = row.get("temporary_periods")
        if terms and terms.get("periods_after_current", 2) <= 1:
            remaining = terms["periods_after_current"]
            lines.append(render("results", "awareness_temporary", label=label, remaining=remaining))
    omitted = context.get("omitted_relevant_reports", 0)
    if omitted:
        lines.append(render("results", "awareness_omitted_reports", omitted=omitted))
    return lines


def render_inbox(result: dict) -> str:
    """Render real mail and consequential controls; an empty overview is empty text."""
    lines = []
    pending = result.get("pending_count", 0)
    if pending:
        lines.append(f"Inbox: {pending} pending message{'s' if pending != 1 else ''}.")
    for conversation in result.get("conversations", []):
        others = [person(p) for p in conversation.get("participants", [])]
        with_whom = ", ".join(others) if others else render("results", "inbox_with_yourself")
        if conversation.get("participants_omitted"):
            with_whom += f" and {conversation['participants_omitted']} more"
        lines.append(
            f"- {conversation['conversation_id']} · with {with_whom}"
            f" · {conversation['pending_count']} waiting"
        )
        for header in conversation.get("waiting", []):
            lines.append(
                f"  {header['message_id']} · {person(header['sender'])}: {header['subject']}"
            )
        if conversation.get("waiting_omitted"):
            lines.append(
                "  "
                + render(
                    "results",
                    "inbox_more_waiting",
                    count=conversation["waiting_omitted"],
                    conversation_id=conversation["conversation_id"],
                )
            )
    if result.get("omitted_rows"):
        lines.append(f"{result['omitted_rows']} conversations omitted.")
    if result.get("next_cursor") is not None:
        lines.append(render("results", "inbox_continue", cursor=repr(result["next_cursor"])))
    lines.extend(_awareness(result.get("context", {})))
    lines.extend(_controls(result))
    return "\n".join(lines)


def _details(value, *, key: str = "") -> list[str]:
    """Readable fallback for operation-specific facts, excluding runtime bookkeeping."""
    if value is None or key in _INTERNAL:
        return []
    if isinstance(value, dict):
        if "employee_id" in value and set(value) <= {
            "employee_id",
            "address",
            "identity",
            "employment",
            "accepts_mail",
            "status",
        }:
            label = person(value)
            return [label + (f": {value['status']}" if value.get("status") else "")]
        lines = []
        for field, item in value.items():
            if field in _INTERNAL or field in {
                "context_pressure",
                "operations_control",
                "mail_waiting",
            }:
                continue
            rendered = _details(item, key=field)
            if not rendered:
                continue
            title = field.replace("_", " ").capitalize()
            lines.append(f"{title}: " + (rendered[0] if len(rendered) == 1 else ""))
            if len(rendered) > 1:
                lines.extend(rendered)
        return lines
    if isinstance(value, list):
        return [line for item in value for line in _details(item, key=key)]
    if key in {"due_at", "created_at"}:
        return [_time(value)]
    return [str(value)]


def _messages(messages: list[dict]) -> list[str]:
    lines = []
    for message in messages:
        lines.append(
            f"{message['message_id']} · Conversation {message['conversation_id']}"
            f" · From {person(message['author'])}"
            f" · {_time(message['created_at'])}\nSubject: {message['subject']}"
        )
        lines.append("To: " + ", ".join(person(p) for p in message.get("recipients", [])))
        if message.get("reply_to"):
            lines.append(f"In reply to {message['reply_to']}")
        lines.append(message["body"])
        attachments = message.get("attachments", [])
        if attachments:
            lines.append("Attachments (relative to your drive; prepend its path to open):")
            lines.extend(f"- {item['path']}/{item['name']}" for item in attachments)
        if message.get("summary"):
            lines.append(render("results", "summary_label") + "\n" + message["summary"])
    return lines


def _disposition(name: str, result: dict) -> list[str]:
    """Claim, dismiss and clear: what was read or set aside, and why nothing was."""
    status = result.get("status")
    if status == "claimed":
        lines = [render("results", "claim_reading"), *_messages(result["messages"])]
        if result.get("remaining"):
            lines.append(
                render(
                    "results",
                    "claim_remaining",
                    count=result["remaining"],
                    conversation_id=result["conversation_id"],
                )
            )
        return lines
    if status == "dismissed":
        return [render("results", "dismissed", count=len(result["message_ids"]))]
    if status == "cleared":
        cleared = "; ".join(
            f"{item['conversation_id']} ({', '.join(item['message_ids'])})"
            for item in result["conversations"]
        )
        return [render("results", "cleared", count=result["count"], conversations=cleared)]
    if result.get("reason") == "no_peek":
        return [render("results", "disposition_needs_peek", operation=name)]
    if result.get("reason") == "nothing_waiting":
        return [render("results", "nothing_waiting", operation=name)]
    return _details(result)


def _delivery(result: dict) -> list[str]:
    """One queued line, one line per failed recipient, and a lookup hint on any failure."""
    message_id = result["message_id"]
    queued = [_recipient(item) for item in result.get("queued", [])]
    if queued:
        names = queued[0] if len(queued) == 1 else ", ".join(queued[:-1]) + " and " + queued[-1]
        lines = [render("results", "mail_queued", message_id=message_id, recipients=names)]
    else:
        lines = [render("results", "mail_not_queued", message_id=message_id)]
    failed = result.get("failed", [])
    for failure in failed:
        lines.append(
            render(
                "results",
                "mail_not_delivered",
                recipient=_recipient(failure),
                reason=failure["reason"],
            )
        )
    if failed:
        lines.append(render("results", "mail_find_recipient"))
    if result.get("continues"):
        lines.append(
            render(
                "results",
                "conversation_continues",
                conversation_id=result["conversation_id"],
                continues=result["continues"],
            )
        )
    summary = result.get("summary")
    if isinstance(summary, dict):
        if summary.get("status") == "attached":
            lines.append(render("results", "summary_attached"))
        else:
            lines.append(render("results", "summary_unavailable", reason=summary.get("reason")))
    return ["\n".join(lines)]


def _recipient(value: dict) -> str:
    """The address as the sender wrote it, or the role's address when the audience named it."""
    return person(value.get("reference") or value.get("address") or value["employee_id"])


def _workspace(workspace: dict) -> list[str]:
    if workspace.get("path"):
        access = "read/write" if workspace.get("access") == "read_write" else "read only"
        return [f"Workspace: {workspace['path']} ({access})"]
    if workspace.get("visibility") == "reporting_line":
        return [render("results", "workspace_outside_line")]
    if workspace and "status" not in workspace:
        return [render("results", "workspace_unavailable")]
    return []


def render_tool_result(name: str, result: dict) -> str:
    if name == "peek":
        return render_inbox(result) or render("results", "no_pending_mail")
    if name == "handoff" and result.get("next") in {"continue", "wait"}:
        saved = render("controls", "saved")
        lines = _controls(
            result,
            include_checklist=False,
            excluded_instructions={saved},
        )
        lines.append(saved)
    elif name == "hire":
        lines = [
            render(
                "results",
                "hire_queued",
                address=result["address"],
                employee_id=result["employee_id"],
            )
        ]
        terms = result.get("appointment", {})
        if terms.get("kind") == "temporary":
            lines.append(
                render("results", "hire_temporary", remaining_periods=terms["remaining_periods"])
            )
        lines.append(render("results", "hire_followup"))
    elif name == "task":
        lines = [result["answer"] if result["status"] == "completed"
                 else "unavailable: " + result["reason"]]
    elif name in {"send", "reply"} and result.get("status") == "sent":
        lines = _delivery(result)
    elif name == "messages" and "messages" in result:
        lines = _messages(result["messages"])
    elif name in {"claim", "dismiss", "clear"} and result.get("status") != "blocked":
        lines = _disposition(name, result)
    elif name == "revise_mandate":
        lines = [
            render(
                "results",
                "mandate_updated",
                address=result["address"],
                employee_id=result["employee_id"],
                revision=result["mandate"]["revision"],
            )
        ]
    elif name == "fire":
        lines = [render("results", "retired", person=person(result))]
        if result.get("mail"):
            lines.extend(_details(result["mail"]))
    elif name in {"renew_temporary", "promote"}:
        if result["kind"] == "durable":
            lines = [render("results", "permanent_appointment", employee_id=result["employee_id"])]
        else:
            lines = [
                render(
                    "results",
                    "temporary_remaining",
                    employee_id=result["employee_id"],
                    remaining_periods=result["remaining_periods"],
                )
            ]
    elif name == "get_role" and "role" in result:
        role = result["role"]
        lines = [person(role)]
        if role.get("manager"):
            lines.append("Reports to " + person(role["manager"]))
        for section, document in result.get("documents", {}).items():
            title = "Profile · self-description" if section == "profile" else section.capitalize()
            if section == "mandate":
                title += f" · revision {document.get('revision')} · delegated by " + person(
                    document.get("author_id", "")
                )
            if document.get("status") != "available":
                lines.append(f"{title}: {document.get('status', 'unavailable')}")
            else:
                lines.append(f"{title}\n{document['text']}")
                if not document.get("complete", True):
                    lines.append(render("results", "document_continues"))
        lines.extend(_workspace(result.get("workspace", {})))
    elif name == "find_roles":
        lines = [f"{result['total_matches']} matching roles."]
        for role in result.get("roles", []):
            lines.append(person(role))
            lines.extend(f"  {part['source']}: {part['text']}" for part in role.get("evidence", []))
    elif name == "org_tree":
        lines = [person(result["root"])]
        if result.get("breadcrumb"):
            lines.insert(0, " / ".join(person(p) for p in result["breadcrumb"]))
        for role in result.get("nodes", []):
            lines.append("  " * role["depth"] + person(role))
        if result.get("omitted_by_depth"):
            lines.append(render("results", "tree_depth", omitted=result["omitted_by_depth"]))
    elif name == "retry":
        lines = [
            render(
                "results",
                "retry_queued",
                employee_id=result["employee_id"],
                recovery_episode=result["recovery_episode"],
            )
        ]
        if result.get("execution_dependency_blocked"):
            lines.append(render("results", "retry_dependency"))
    else:
        lines = _details(result)
    if name in {"get_role", "find_roles", "org_tree"}:
        if result.get("remaining"):
            lines.append(render("results", "more_roles", remaining=result["remaining"]))
        if result.get("continuation"):
            lines.append(render("results", "continuation", cursor=result["continuation"]))
    if not (name == "handoff" and result.get("next") in {"continue", "wait"}):
        lines.extend(_controls(result))
        lines.extend(_mail_waiting(result))
    return "\n\n".join(lines) or render("results", "done")
