"""Owner-only configuration repair and explicit role stop/routing operations."""

import json

from . import analysis_history, github, guidance
from .errors import FirmError
from .lifecycle import _require_address, validate_config
from .store import OWNER, encoded


class OwnerControls:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def send_mail(self, actor, recipients, body, *, subject=None, attachments=None,
                  action_recipients=None, request_id):
        self._require_owner(actor)
        mail = self.runtime.messaging
        payload = dict(recipients=recipients, body=body, subject=subject,
                       attachments=attachments, action_recipients=action_recipients)
        with self.store.transaction() as db:
            def send():
                intended = mail._intended_recipients(db, recipients)
                actions = action_recipients
                if actions is None:
                    actions = recipients if len(intended) == 1 else []
                keys = mail._normalize_actions(db, actions, intended)
                return mail._send(db, actor, recipients, body, subject,
                                  attachments=attachments, action_keys=keys)
            return self.store.request(db, actor, request_id, "send", payload, send)

    def reply_mail(self, actor, message_id, body, *, attachments=None,
                   action_recipients=None, request_id):
        self._require_owner(actor)
        mail = self.runtime.messaging
        payload = dict(message_id=message_id, body=body, attachments=attachments,
                       action_recipients=action_recipients)
        with self.store.transaction() as db:
            def reply():
                _, _, audience = mail._thread(db, actor, message_id)
                others = [p for p in audience if p["employee_id"] != actor]
                actions = action_recipients
                if actions is None:
                    actions = [p["employee_id"] for p in others] if len(others) == 1 else []
                return mail._reply(db, actor, message_id, actions, body, attachments)
            return self.store.request(db, actor, request_id, "reply", payload, reply)

    @staticmethod
    def _require_owner(actor):
        if actor != OWNER:
            raise FirmError("only the owner may change execution routing or configuration")

    def configure(self, actor, config):
        self._require_owner(actor)
        config = validate_config(config)
        # Reject local state conflicts before an optional GitHub setup call can create
        # or update anything outside the firm database. Recheck after that call so the
        # eventual write is still based on current role state.
        with self.store.read() as db:
            self.validate_reconfiguration(db, config)
        app = github.connect(config["github"]) if "github" in config else None
        with self.store.transaction() as db:
            self.validate_reconfiguration(db, config)
            corrected = self.runtime.context.refresh_policies(db, config)
            db.execute("UPDATE firm SET config_json=?", (encoded(config),))
            github.record_app(db, app)
        return {
            "status": "applied",
            "retained_sessions": (
                "keep captured execution identity; matching context policy updated"
            ),
            "retained_context_policies_updated": corrected,
            "shared_holds": "require explicit incident clearance after repair",
        }

    def validate_reconfiguration(self, db, config):
        used = {
            row[0]
            for row in db.execute(
                "SELECT execution_profile FROM roles WHERE employment!='retired'"
            )
        }
        if used - config["profiles"].keys():
            raise FirmError("configuration must retain current roles' primary profiles")
        if self.runtime.accounts.enabled(db) and "accounts" not in config:
            raise FirmError("cannot remove named accounts from an execution host")
        for row in db.execute(
            "SELECT DISTINCT account_id,provider FROM summary_runs "
            "WHERE kind='task' AND state='running'"
        ):
            account = config.get("accounts", {}).get(row["account_id"])
            if account is None or account["provider"] != row["provider"]:
                raise FirmError("configuration must retain accounts held by tasks")

    @staticmethod
    def _scope(address):
        """A role address and its subtree, or every role of the firm."""
        if address is None:
            return ""
        _require_address(address)
        return address

    def revoke_github(self, actor, repo, address=None):
        """Refuse the repository to an address, or every role, until the owner grants it again."""
        self._require_owner(actor)
        return self.runtime.github.revoke(repo, self._scope(address))

    def grant_github(self, actor, repo, address=None):
        """Clear an address's own revocation and grant it write, or every revocation without one.

        A wider revocation covering the address is refused rather than lifted for everyone.
        """
        self._require_owner(actor)
        return self.runtime.github.allow(repo, self._scope(address))

    def stop_role(self, actor, employee_id, request_id):
        self._require_owner(actor)
        with self.store.transaction() as db:
            role = self.store.role(db, employee_id)

            def stop():
                if role["employment"] == "retired":
                    raise FirmError("retired roles cannot be stopped or reopened")
                db.execute(
                    "UPDATE roles SET employment='paused' WHERE employee_id=?", (employee_id,)
                )
                active = [
                    r[0]
                    for r in db.execute(
                        "SELECT activation_id FROM activations WHERE employee_id=? "
                        "AND state IN ('admitted','running')",
                        (employee_id,),
                    )
                ]
                for aid in active:
                    db.execute(
                        "UPDATE activations SET token_hash=NULL WHERE activation_id=?", (aid,)
                    )
                    db.execute(
                        "UPDATE sandbox_runs SET stop_cause='owner_stop' WHERE activation_id=?",
                        (aid,),
                    )
                analysis_history.role_changed(db, self.store.clock(), role, employee_id)
                return {
                    "employee_id": employee_id,
                    "status": "stopping" if active else "paused",
                    "execution_exit_confirmed": not bool(active),
                }

            return self.store.request(
                db, actor, request_id, "stop_role", {"employee_id": employee_id}, stop
            )

    def route(self, actor, employee_id, profile, request_id):
        """Explicit fresh start; callers first stop live execution and wait for confirmation."""
        self._require_owner(actor)
        with self.store.transaction() as db:
            role = self.store.role(db, employee_id)

            def change():
                if role["employment"] == "retired":
                    raise FirmError("retired roles cannot be routed")
                if db.execute(
                    "SELECT 1 FROM activations WHERE employee_id=? "
                    "AND state IN ('admitted','running')",
                    (employee_id,),
                ).fetchone():
                    raise FirmError(
                        "confirm the old execution has stopped before routing fresh work"
                    )
                if profile not in self.runtime.accounts.config(db)["profiles"]:
                    raise FirmError("unknown execution profile")
                recovery = json.loads(role["recovery_json"]) if role["recovery_json"] else None
                if recovery:
                    recovery["context_retained"] = False
                db.execute(
                    "UPDATE roles SET execution_profile=?,effort=NULL,session_id=NULL,"
                    "employment='active',recovery_json=? "
                    "WHERE employee_id=?",
                    (profile, encoded(recovery) if recovery else None, employee_id),
                )
                self.runtime.levels.mark_routed(db, employee_id)
                db.execute("DELETE FROM role_dependencies WHERE employee_id=?", (employee_id,))
                self.store.request_execution(db, employee_id, "owner_fresh_route")
                analysis_history.role_changed(db, self.store.clock(), role, employee_id)
                if role["execution_profile"] == profile:
                    analysis_history.record(
                        db,
                        self.store.clock(),
                        "role.routed",
                        employee_id,
                        profile=profile,
                        fresh=True,
                    )
                return {
                    "employee_id": employee_id,
                    "profile": profile,
                    "status": "queued",
                    "context": "fresh",
                    "attempt_budget_preserved": True,
                }

            return self.store.request(
                db,
                actor,
                request_id,
                "route_fresh",
                {"employee_id": employee_id, "profile": profile},
                change,
            )

    def set_guidance(self, actor, employee_id, scope, functions, request_id):
        """Set exactly the owner's selection, including an intentionally empty one."""
        self._require_owner(actor)
        try:
            selection = guidance.parse_selection(
                {"scope_guidance": scope, "functional_guidance": functions}, guidance.catalog()
            )
        except ValueError as exc:
            raise FirmError(str(exc)) from exc
        with self.store.transaction() as db:
            role = self.store.role(db, employee_id)

            def change():
                if role["employment"] == "retired":
                    raise FirmError("retired roles cannot be given guidance")
                db.execute(
                    "UPDATE roles SET guidance_state='owner',scope_guidance=?,"
                    "functional_guidance=?,guidance_retry_at=NULL,guidance_reason=NULL "
                    "WHERE employee_id=?",
                    (selection["scope"], encoded(selection["functions"]), employee_id),
                )
                analysis_history.record(db, self.store.clock(), "guidance.owner_set",
                                        employee_id, **selection)
                return selection

            return self.store.request(db, actor, request_id, "set_guidance",
                                      {"employee_id": employee_id, **selection}, change)

    def automatic_guidance(self, actor, employee_id, request_id):
        self._require_owner(actor)
        with self.store.transaction() as db:
            role = self.store.role(db, employee_id)

            def change():
                if role["employment"] == "retired":
                    raise FirmError("retired roles cannot be given guidance")
                config = self.runtime.accounts.config(db)
                revision = db.execute("SELECT MAX(revision) FROM mandates WHERE employee_id=?",
                                      (employee_id,)).fetchone()[0]
                db.execute(
                    "UPDATE roles SET scope_guidance=NULL,functional_guidance='[]' "
                    "WHERE employee_id=?", (employee_id,),
                )
                if config.get("staffing") is False:
                    db.execute(
                        "UPDATE roles SET guidance_state=NULL,guidance_revision=NULL,"
                        "guidance_attempts=0,guidance_retry_at=NULL,guidance_reason=NULL "
                        "WHERE employee_id=?", (employee_id,),
                    )
                else:
                    guidance.register(db, employee_id, revision)
                analysis_history.record(db, self.store.clock(), "guidance.automatic",
                                        employee_id, revision=revision)
                return guidance.effective(db, self.store.role(db, employee_id), config)

            return self.store.request(db, actor, request_id, "automatic_guidance",
                                      {"employee_id": employee_id}, change)

    def resume_role(self, actor, employee_id, request_id):
        self._require_owner(actor)
        with self.store.transaction() as db:
            role = self.store.role(db, employee_id)

            def resume():
                if role["employment"] == "retired":
                    raise FirmError("retired roles cannot be resumed")
                if role["employment"] != "paused":
                    raise FirmError("only a stopped role can be resumed")
                if db.execute(
                    "SELECT 1 FROM activations WHERE employee_id=? "
                    "AND state IN ('admitted','running')",
                    (employee_id,),
                ).fetchone():
                    raise FirmError("wait for the old execution to exit before resuming")
                db.execute(
                    "UPDATE roles SET employment='active' WHERE employee_id=?", (employee_id,)
                )
                self.store.request_execution(db, employee_id, "owner_resume")
                analysis_history.role_changed(db, self.store.clock(), role, employee_id)
                return {"employee_id": employee_id, "status": "queued", "holds_preserved": True}

            return self.store.request(
                db, actor, request_id, "resume_role", {"employee_id": employee_id}, resume
            )
