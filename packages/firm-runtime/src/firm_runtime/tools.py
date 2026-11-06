"""Role operations bind the caller to one live activation, inside each transaction."""

import asyncio
import threading
from typing import Annotated, Literal, TypedDict

from pydantic import ConfigDict, Field

# Modifier keys the helper holds down while it posts an event.
MODIFIERS = Annotated[
    tuple[Literal["command", "shift", "option", "control"], ...], Field(max_length=4)
]


class Tap(TypedDict):
    """One tap of a planned passage: where it lands, and how long after the one before it."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    x: float
    y: float
    after_ms: Annotated[int, Field(strict=True, ge=0, le=5000)]


class RoleTools:
    def __init__(self, runtime, token: str):
        self.runtime = runtime
        self.token = token

    async def task(self, instruction: str, request_id: str) -> dict:
        """Await a bounded worker without blocking parallel MCP calls."""
        cancelled = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(
            self.runtime.task_service.call, self.token, instruction, request_id,
            cancelled=cancelled,
        ))
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            await worker
            raise
        # The worker may outlast mail arrivals, drain requests, or this activation.
        # Reauthorize and present current controls only after releasing the wait.
        with self.runtime.authorized(self.token) as actor:
            return self.runtime.present_controls(actor["activation_id"], result)

    def _call(
        self,
        method,
        *args,
        with_activation=False,
        with_token=False,
        gate_claim=False,
        discovery=False,
        observes_inbox=False,
        **kwargs,
    ):
        with self.runtime.authorized(self.token) as actor:
            if gate_claim and self.runtime.context.gate_claim(actor["activation_id"]):
                return self.runtime.present_controls(
                    actor["activation_id"],
                    {"status": "blocked", "reason": "context_handoff_required"},
                )
            if with_activation:
                kwargs["activation_id"] = actor["activation_id"]
            if discovery:
                kwargs["cursor"] = self.runtime.discovery.resolve_continuation(
                    actor["activation_id"], kwargs.get("cursor")
                )
            principal = self.token if with_token else actor["employee_id"]
            result = method(principal, *args, **kwargs)
            if discovery:
                result = self.runtime.discovery.present_continuation(actor["activation_id"], result)
            return self.runtime.present_controls(
                actor["activation_id"], result, observed_inbox=observes_inbox
            )

    def send(
        self, recipients: list[str], body: str, request_id: str,
        subject: str | None = None,
        attachments: Annotated[list[str], Field(max_length=10)] | None = None,
    ) -> dict:
        return self._call(
            self.runtime.messaging.send, recipients, body, subject=subject,
            attachments=attachments, request_id=request_id
        )

    def reply(
        self,
        message_id: str,
        body: str,
        request_id: str,
        add: list[str] | None = None,
        summarize: bool = False,
        attachments: Annotated[list[str], Field(max_length=10)] | None = None,
    ) -> dict:
        """Reply without holding authorization open: a summary may call a provider."""
        with self.runtime.authorized(self.token) as actor:
            pass
        result = self.runtime.messaging.reply(
            actor["employee_id"],
            message_id,
            body,
            add=add,
            summarize=summarize,
            attachments=attachments,
            request_id=request_id,
            session_id=actor["session_id"],
        )
        return self.runtime.present_controls(actor["activation_id"], result)

    def peek(self, cursor: str | None = None, limit: int = 100) -> dict:
        return self._call(
            self.runtime.messaging.peek,
            cursor=cursor,
            limit=limit,
            with_activation=True,
            observes_inbox=True,
        )

    def claim(
        self,
        request_id: str,
        message_ids: list[str] | None = None,
        conversation: str | None = None,
    ) -> dict:
        return self._call(
            self.runtime.messaging.claim,
            message_ids,
            conversation=conversation,
            request_id=request_id,
            with_activation=True,
            gate_claim=True,
        )

    def dismiss(
        self,
        request_id: str,
        message_ids: list[str] | None = None,
        conversation: str | None = None,
    ) -> dict:
        return self._call(
            self.runtime.messaging.dismiss,
            message_ids,
            conversation=conversation,
            request_id=request_id,
            with_activation=True,
        )

    def clear(self, request_id: str) -> dict:
        return self._call(
            self.runtime.messaging.clear, request_id=request_id, with_activation=True
        )

    def messages(self, message_ids: list[str]) -> dict:
        return self._call(self.runtime.messaging.messages, message_ids)

    def context(self, conversation: str) -> dict:
        """Request derived correspondence context without holding authorization open."""
        with self.runtime.authorized(self.token) as actor:
            # Record the incoming call, then release the transaction before
            # summary work that may involve a provider.
            pass
        result = self.runtime.messaging.context(
            actor["employee_id"], conversation, session_id=actor["session_id"]
        )
        return self.runtime.present_controls(actor["activation_id"], result)

    def set_reminder(self, due_at: float, note: str, request_id: str) -> dict:
        return self._call(self.runtime.reminders.set, due_at, note, request_id)

    def list_reminders(self) -> dict:
        return self._call(self.runtime.reminders.list)

    def cancel_reminder(self, reminder_id: str, request_id: str) -> dict:
        return self._call(self.runtime.reminders.cancel, reminder_id, request_id)

    def renew_temporary(self, employee: str, periods: int, request_id: str) -> dict:
        return self._call(self.runtime.appointments.renew, employee, periods, request_id)

    def promote(self, employee: str, request_id: str) -> dict:
        return self._call(self.runtime.appointments.promote, employee, request_id)

    def hire(
        self,
        slug: str,
        mandate: str,
        request_id: str,
        temporary_periods: int | None = None,
    ) -> dict:
        return self._call(
            self.runtime.lifecycle.hire,
            slug,
            mandate,
            request_id=request_id,
            temporary_periods=temporary_periods,
        )

    def get_role(
        self,
        role: str,
        section: str = "all",
        cursor: str | None = None,
    ) -> dict:
        # Resolve once inside authorization, preserving an exact retired identity.
        with self.runtime.authorized(self.token) as actor:
            with self.runtime.store.read() as db:
                selected = self.runtime.store.resolve(db, role)
            from .errors import FirmError

            if selected is None:
                raise FirmError("unknown employee or current role address")
            result = self.runtime.discovery.get_role(
                actor["employee_id"],
                employee_id=selected["employee_id"],
                section=section,
                cursor=self.runtime.discovery.resolve_continuation(actor["activation_id"], cursor),
                activation_id=actor["activation_id"],
            )
            result = self.runtime.discovery.present_continuation(actor["activation_id"], result)
            return self.runtime.present_controls(actor["activation_id"], result)

    def find_roles(self, query: str = "", limit: int = 10, cursor: str | None = None) -> dict:
        return self._call(
            self.runtime.discovery.find_roles,
            query=query,
            limit=limit,
            cursor=cursor,
            discovery=True,
        )

    def org_tree(
        self,
        root: str | None = None,
        depth: int = 2,
        max_nodes: int = 50,
        cursor: str | None = None,
    ) -> dict:
        return self._call(
            self.runtime.discovery.org_tree,
            root=root,
            depth=depth,
            max_nodes=max_nodes,
            cursor=cursor,
            discovery=True,
        )

    def revise_mandate(
        self,
        employee: str,
        expected_revision: int,
        mandate: str,
        request_id: str,
        reason: str = "",
    ) -> dict:
        return self._call(
            self.runtime.lifecycle.revise_mandate,
            employee,
            expected_revision,
            mandate,
            reason=reason,
            request_id=request_id,
        )

    def fire(self, employee: str, reason: str, request_id: str) -> dict:
        return self._call(
            self.runtime.lifecycle.fire, employee, reason=reason, request_id=request_id
        )

    def retry(self, employee: str, episode: str, request_id: str) -> dict:
        return self._call(self.runtime.retry, employee, episode, request_id)

    def handoff(self, notes: str, next: str, request_id: str) -> dict:
        return self._call(self.runtime.handoff, notes, next, request_id, with_token=True)

    def create_repository(self, name: str, purpose: str, request_id: str) -> dict:
        with self.runtime.authorized(self.token) as actor:
            # Record the call, then release the transaction before GitHub is asked.
            pass
        result = self.runtime.github.create_repository(actor, request_id, name, purpose)
        return self.runtime.present_controls(actor["activation_id"], result)

    def request_access(self, kind: str, resource: str, purpose: str, request_id: str) -> dict:
        with self.runtime.authorized(self.token) as actor:
            # Record the call, then release the transaction before the kind is asked.
            pass
        result = self.runtime.requests.request_access(actor, request_id, kind, resource, purpose)
        return self.runtime.present_controls(actor["activation_id"], result)

    def mac_acquire(self) -> dict:
        return self.runtime.mac.acquire(self.token)

    def mac_release(self) -> dict:
        return self.runtime.mac.release(self.token)

    def mac_status(self) -> dict:
        return self.runtime.mac.status(self.token)

    def desktop_status(self) -> dict:
        return self.runtime.mac.call(self.token, "status")

    def desktop_apps(self) -> dict:
        return self.runtime.mac.call(self.token, "list_apps")

    def desktop_windows(self, pid: int) -> dict:
        return self.runtime.mac.call(self.token, "list_windows", pid=pid)

    def desktop_inspect(
        self, pid: int, depth: int = 4, max_elements: int = 200,
        handle: str | None = None,
    ) -> dict:
        arguments = {"pid": pid, "depth": depth, "max_elements": max_elements}
        if handle is not None:
            arguments["handle"] = handle
        return self.runtime.mac.call(self.token, "inspect_ax", **arguments)

    def desktop_launch(
        self, path: str, launch_mode: Literal["launch_services", "direct"] = "launch_services"
    ) -> dict:
        return self.runtime.mac.call(
            self.token, "launch_app", path=path, launch_mode=launch_mode
        )

    def desktop_action(self, pid: int, handle: str, action: str) -> dict:
        return self.runtime.mac.call(
            self.token, "action", pid=pid, handle=handle, action=action
        )

    def desktop_focus(self, pid: int, handle: str) -> dict:
        return self.runtime.mac.call(self.token, "focus", pid=pid, handle=handle)

    def desktop_type(
        self,
        pid: int,
        text: Annotated[str, Field(min_length=1)],
        handle: str | None = None,
    ) -> dict:
        return self.runtime.mac.call(self.token, "type_text", pid=pid, text=text, handle=handle)

    def desktop_press_key(
        self, pid: Annotated[int, Field(strict=True, gt=0)],
        key_code: Annotated[int, Field(strict=True, ge=0, le=127)],
        modifiers: MODIFIERS = (),
    ) -> dict:
        return self.runtime.mac.call(
            self.token, "press_key", pid=pid, key_code=key_code, modifiers=list(modifiers)
        )

    def desktop_capture(self, pid: int, window_id: int) -> dict:
        return self.runtime.mac.call(self.token, "capture_window", pid=pid, window_id=window_id)

    def desktop_click(
        self, pid: int, window_id: int, x: float, y: float,
        button: Literal["left", "right"] = "left",
        modifiers: MODIFIERS = (),
        hold_ms: Annotated[int, Field(strict=True, ge=0, le=10000)] = 0,
    ) -> dict:
        return self.runtime.mac.call(
            self.token, "click", pid=pid, window_id=window_id, x=x, y=y, button=button,
            modifiers=list(modifiers), hold_ms=hold_ms,
        )

    def desktop_drag(
        self, pid: int, window_id: int,
        from_x: float, from_y: float, to_x: float, to_y: float,
        modifiers: MODIFIERS = (),
        hold_ms: Annotated[int, Field(strict=True, ge=0, le=10000)] = 0,
        duration_ms: Annotated[int, Field(strict=True, ge=50, le=10000)] | None = None,
    ) -> dict:
        return self.runtime.mac.call(
            self.token, "drag", pid=pid, window_id=window_id,
            from_x=from_x, from_y=from_y, to_x=to_x, to_y=to_y,
            modifiers=list(modifiers), hold_ms=hold_ms, duration_ms=duration_ms,
        )

    def desktop_scroll(
        self, pid: int, window_id: int, x: float, y: float,
        delta_x: int = 0, delta_y: int = 0,
        modifiers: MODIFIERS = (),
    ) -> dict:
        return self.runtime.mac.call(
            self.token, "scroll", pid=pid, window_id=window_id,
            x=x, y=y, delta_x=delta_x, delta_y=delta_y, modifiers=list(modifiers),
        )

    def desktop_tap_sequence(
        self, pid: int, window_id: int,
        taps: Annotated[list[Tap], Field(min_length=1, max_length=64)],
    ) -> dict:
        return self.runtime.mac.call(
            self.token, "tap_sequence", pid=pid, window_id=window_id, taps=list(taps)
        )

    def desktop_close_apps(self) -> dict:
        return self.runtime.mac.call(self.token, "release")
